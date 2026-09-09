"""Iron-condor exit monitor.

Runs every few minutes during RTH. For each open SPY iron-condor `trades`
row, fetches fresh quotes for the four legs, computes the current exit
debit, and submits a closing order when any of these fire:

- **1.5× stop loss**: exit when current debit ≥ 2.5 × credit_received
  (running loss is 1.5× the credit collected; the validated condor stop)
- **Force close at/after 15:45 ET**: time-based; we never hold past close
  (≈ the backtest's close settlement). NO profit target — the condor's edge is
  full-credit expiries, so we hold winners to the force-close. SMART variant: at
  the deadline we only actually close when SPY is near a short strike (pin/breach
  risk); a condor comfortably inside its shorts is left to EXPIRE worthless for
  free rather than paying the 4-leg exit spread. Loss-cut exits always close.

P&L per contract = entry_credit - exit_debit (positive = profit).
On fill, the `trades` row is updated with exit_price, realized_pnl_usd,
and closed_at.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.options.condor_engine import STOP_MULT, stop_breached
from integrations import alpaca_client
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.config import get_settings
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.timeutils import to_et

log = get_logger(__name__)

STRATEGY_NAME = "spy_0dte_ic"
# Primary force-close moved 15:50 → 15:45 (2026-08-17): the 15:45 sweep force-closes
# via the clock check below, while the full position is still held and quotes are
# live — before the broker starts pre-processing 0DTE expiry (which desynced #150's
# close). The explicit 15:50 scheduler job remains as a safety-net retry.
FORCE_CLOSE_AFTER = time(15, 45)
# Smart force-close band: at the deadline, only close when SPY is within this
# fraction of a short strike (or already breached) — otherwise let the condor
# expire worthless for free instead of paying the exit spread on a clean winner.
FORCE_CLOSE_NEAR_STRIKE_PCT = 0.003  # 0.3% ≈ $2.3 on SPY at 770

# Statuses in which an order is already off the book — no point cancelling. Any
# other status (`new`, `accepted`, `pending_new`, `partially_filled`, …) means the
# order may still be live and holding quantity, so an unfilled close in one of
# those states is cancelled. Mirrors alpaca_client._TERMINAL_ORDER_STATUSES minus
# `filled` (a filled close is handled on its own branch).
_DEAD_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced", "suspended"}
)


# ----------------- helpers -----------------


def _open_iron_condor_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.strategy == STRATEGY_NAME, Trade.closed_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


def _occ_strike(occ: str | None) -> Decimal | None:
    """Strike from an OCC symbol (last 8 digits / 1000), or None if unparseable."""
    if not occ or len(occ) < 8 or not occ[-8:].isdigit():
        return None
    return Decimal(occ[-8:]) / Decimal("1000")


def _near_short_strike(
    spot: Decimal | None, short_put_occ: str | None, short_call_occ: str | None,
    pct: float = FORCE_CLOSE_NEAR_STRIKE_PCT,
) -> bool:
    """True if SPY is within `pct` of a short strike (or already through one) — the
    only regime where force-closing beats letting the condor expire. FAIL-SAFE: an
    unknown spot (feed blip) or unparseable strikes return True, so we close rather
    than gamble on a position we can't evaluate."""
    if spot is None:
        return True
    sp = _occ_strike(short_put_occ)
    sc = _occ_strike(short_call_occ)
    if sp is None or sc is None:
        return True
    spot = Decimal(str(spot))
    band = Decimal(str(pct))
    return spot <= sp * (Decimal("1") + band) or spot >= sc * (Decimal("1") - band)


async def _fetch_spot(stock_fetcher, trade_id) -> Decimal | None:
    """Best-effort SPY spot for the near-strike gates. Returns None on a feed blip;
    callers pass None to _near_short_strike, which FAIL-SAFEs to 'near' (→ close)."""
    try:
        q = await stock_fetcher("SPY")
        if q is not None:
            return q.mid if getattr(q, "mid", None) and q.mid > 0 else (q.bid or q.ask)
    except Exception as e:  # noqa: BLE001 — feed blip → caller fails safe
        log.warning("exit_monitor_spot_fetch_failed", trade_id=trade_id, error=str(e))
    return None


async def _assignment_leg_out(
    trade, *, spot, legs, chain, closer, waiter, buf, fill_timeout_s, factory,
) -> dict:
    """Assignment config: at the deadline, buy back ONLY the short leg(s) near/ITM via
    single-leg market orders so a physically-settled short can't assign shares
    overnight. Longs + comfortably-OTM shorts are left to expire; the reconciler
    settles the residual at 16:03. Records the buybacks on the trade; does NOT mark it
    closed. FAIL-SAFE: unknown spot/strike → close the leg (never leave a blind short)."""
    short_put, _long_put, short_call, _long_call = legs
    band = Decimal(str(buf))
    spot_d = Decimal(str(spot)) if spot is not None else None
    closed: list[dict] = []
    for occ, is_put in ((short_put, True), (short_call, False)):
        strike = _occ_strike(occ)
        if strike is None or spot_d is None:
            near = True  # fail-safe: can't evaluate → close rather than risk assignment
        else:
            near = (spot_d <= strike * (Decimal("1") + band)) if is_put \
                else (spot_d >= strike * (Decimal("1") - band))
        if not near:
            continue
        q = _quote_by_occ(chain, occ)
        ref = (q.ask if q and getattr(q, "ask", None) else Decimal("0"))
        order = await closer(qty=int(Decimal(str(trade.qty))), occ_symbol=occ, limit_price=ref)
        final = await waiter(order.order_id, timeout_s=fill_timeout_s)
        closed.append({
            "occ": occ, "side": "put" if is_put else "call",
            "order_id": order.order_id, "status": final.status,
            "fill": str(final.filled_avg_price) if final.filled_avg_price else None,
        })
        log.info(
            "exit_monitor_assignment_leg_out", trade_id=trade.id, occ=occ,
            side="put" if is_put else "call", status=final.status,
            fill=str(final.filled_avg_price) if final.filled_avg_price else None,
        )
    with factory() as s:
        row = s.get(Trade, trade.id)
        if row is not None:
            ex = row.extra or {}
            ex["assignment_legs_closed"] = closed
            row.extra = ex
            s.commit()
    return {
        "trade_id": trade.id, "status": "assignment_closed",
        "legs_closed": [c["side"] for c in closed],
    }


def _quote_by_occ(chain: list[OptionQuote], occ: str) -> OptionQuote | None:
    for q in chain:
        if q.occ_symbol == occ:
            return q
    return None


def _compute_exit_debit_per_contract(
    *,
    chain: list[OptionQuote],
    short_put_occ: str,
    long_put_occ: str,
    short_call_occ: str,
    long_call_occ: str,
) -> Decimal | None:
    """Net debit per contract to close the iron condor at current quotes.

    Closing = buy back shorts (pay ask) + sell longs (receive bid).
    Returns None if any leg's quote is missing.
    """
    sp = _quote_by_occ(chain, short_put_occ)
    lp = _quote_by_occ(chain, long_put_occ)
    sc = _quote_by_occ(chain, short_call_occ)
    lc = _quote_by_occ(chain, long_call_occ)
    if not all((sp, lp, sc, lc)):
        return None
    # Use ask for buy-backs and bid for sells (conservative — assumes we pay spread).
    cost_per_share = (sp.ask + sc.ask) - (lp.bid + lc.bid)
    return (cost_per_share * Decimal("100")).quantize(Decimal("0.01"))


def _decide_exit(
    *,
    credit_received: Decimal,
    exit_debit: Decimal,
    force: bool,
    daily_loss_cap_per_contract: Decimal | None = None,
) -> tuple[bool, str]:
    """Return (should_exit, reason) — matches the validated condor backtest:
    a 1.5×-credit intraday stop + force-close, and NO profit target (the edge
    comes from full-credit expiries; a 50% PT would cap winners and degrade it).
    Stop fires when buy-back debit ≥ credit × (1 + STOP_MULT).

    `daily_loss_cap_per_contract` (option B) is a hard per-contract loss ceiling
    derived from condor_daily_loss_limit_pct × pool ÷ qty. It's a HIGHER threshold
    than the 1.5× stop, so it only fires when a fast gap jumped the loss past the
    stop between sweeps — the backstop that guarantees no single day exceeds the
    daily % cap. All three exits close marketably (see _process_one_condor_exit)."""
    if force:
        return True, "force_close_15:50"
    loss_per_ct = exit_debit - credit_received
    if daily_loss_cap_per_contract is not None and loss_per_ct >= daily_loss_cap_per_contract:
        return True, "daily_loss_cap"
    if stop_breached(float(credit_received), float(exit_debit)):
        return True, f"stop_loss_{STOP_MULT:g}x"
    return False, ""


def _format_exit_signal(trade: Trade, exit_debit: Decimal, reason: str) -> str:
    """Broker-ready exit instructions for #signals."""
    extra = trade.extra or {}
    qty = trade.qty
    legs = {
        "short_put": extra.get("short_put", "?"),
        "long_put": extra.get("long_put", "?"),
        "short_call": extra.get("short_call", "?"),
        "long_call": extra.get("long_call", "?"),
    }

    def _strike(occ: str) -> str:
        # Last 8 chars / 1000 = strike. e.g. 00495000 → 495
        if len(occ) < 8 or not occ[-8:].isdigit():
            return "?"
        return str(Decimal(occ[-8:]) / Decimal("1000"))

    credit = trade.entry_price
    realized_per_contract = (Decimal(str(credit)) - exit_debit).quantize(Decimal("0.01"))
    qty_text = f"{qty}× " if qty != 1 else ""

    # Plain-language reason mapping.
    reason_text = {
        "profit_target_50pct": "✅ profit target hit",
        "stop_loss_1.5x": "🛑 stop loss (1.5× credit) — cap the loss now",
        "daily_loss_cap": "🛑 daily loss cap hit — hard floor, close now",
        "force_close_15:50": "⏰ closing before market close",
        "force_close": "⏰ closing before market close",
    }.get(reason, f"closing ({reason})")

    pnl_word = "profit" if realized_per_contract >= 0 else "loss"
    pnl_amount = abs(realized_per_contract)

    return (
        f"🚨 **SPY EXIT now — {reason_text}** (trade #{trade.id})\n"
        f"\n"
        f"1. **Buy back** {qty_text}**SPY ${_strike(legs['short_put'])} PUT**\n"
        f"2. **Sell** {qty_text}**SPY ${_strike(legs['long_put'])} PUT**\n"
        f"3. **Buy back** {qty_text}**SPY ${_strike(legs['short_call'])} CALL**\n"
        f"4. **Sell** {qty_text}**SPY ${_strike(legs['long_call'])} CALL**\n"
        f"\n"
        f"You'll pay about **${exit_debit}** to close. "
        f"Expected {pnl_word}: **${pnl_amount}**."
    )


def _format_exit_telemetry(trade: Trade, *, exit_debit: Decimal, reason: str) -> str:
    """Automated-exit telemetry for #trades."""
    credit = Decimal(str(trade.entry_price))
    qty = Decimal(str(trade.qty))
    pnl_per_contract = (credit - exit_debit).quantize(Decimal("0.01"))
    pnl_total = (pnl_per_contract * qty).quantize(Decimal("0.01"))
    return (
        f"🤖 **Iron-condor closed** — trade #{trade.id}\n"
        f"Reason: `{reason}` · entry credit: ${credit}/contract · "
        f"exit debit: ${exit_debit}/contract\n"
        f"Realized P&L: ${pnl_per_contract}/contract · qty {qty} · total ${pnl_total}"
    )


def _close_trade_row(
    session: Session,
    trade: Trade,
    *,
    exit_debit_per_contract: Decimal,
    order: OrderResult,
    reason: str,
) -> None:
    qty = Decimal(trade.qty)
    credit_per_contract = Decimal(str(trade.entry_price))
    pnl_per_contract = credit_per_contract - exit_debit_per_contract
    trade.exit_price = exit_debit_per_contract
    trade.realized_pnl_usd = pnl_per_contract * qty
    trade.closed_at = datetime.now(UTC)
    extra = dict(trade.extra or {})
    extra["exit_reason"] = reason
    extra["close_order_id"] = order.order_id
    extra["close_status"] = order.status
    extra["close_filled_avg_price_per_share"] = (
        str(order.filled_avg_price) if order.filled_avg_price else None
    )
    trade.extra = extra
    session.commit()


# ----------------- public API -----------------


async def run_exit_monitor(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    chain_fetcher: Callable[..., object] = alpaca_client.get_options_chain,
    submitter: Callable[..., object] = alpaca_client.submit_iron_condor_close,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    canceller: Callable[..., object] = alpaca_client.cancel_order,
    stock_fetcher: Callable[..., object] = alpaca_client.get_latest_stock_quote,
    position_fetcher: Callable[..., object] = alpaca_client.get_positions,
    position_closer: Callable[..., object] = alpaca_client.close_position,
    single_leg_closer: Callable[..., object] = alpaca_client.submit_single_option_buy_to_close,
    force_close: bool | None = None,
    fill_timeout_s: float = 60.0,
) -> list[dict]:
    """Sweep every open iron-condor trade. Returns one dict per trade processed.

    `force_close` defaults to True if ET clock-time is ≥ FORCE_CLOSE_AFTER,
    overriding PT/stop logic so we never hold past 15:50 ET.
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()

    if force_close is None:
        force_close = to_et(now).time() >= FORCE_CLOSE_AFTER

    results: list[dict] = []

    with factory() as session:
        trades = _open_iron_condor_trades(session)

    if not trades:
        return results

    for trade in trades:
        try:
            result = await _process_one_condor_exit(
                trade,
                now=now,
                factory=factory,
                chain_fetcher=chain_fetcher,
                submitter=submitter,
                waiter=waiter,
                canceller=canceller,
                stock_fetcher=stock_fetcher,
                single_leg_closer=single_leg_closer,
                force_close=force_close,
                fill_timeout_s=fill_timeout_s,
            )
        except Exception as e:  # noqa: BLE001 — one stuck trade must not abort the sweep
            result = await _handle_condor_exit_error(
                trade, e, canceller=canceller,
                position_fetcher=position_fetcher, position_closer=position_closer,
            )
        results.append(result)

    return results


def _parse_broker_error(err: object) -> tuple[str | None, list[str], dict]:
    """Best-effort extraction of (code, related_order_ids, full_data) from an Alpaca APIError.

    Alpaca embeds a JSON object in the exception text, e.g.
    `{"available":"0","code":40310000,"held_for_orders":"0","existing_qty":"1",
    ...,"related_orders":["<id>"]}`. Returns (None, [], {}) when nothing
    parseable is found. The full `data` dict lets callers distinguish a
    genuinely held quantity (`held_for_orders` > 0) from a position shortfall
    (`held_for_orders` == 0 and `available` < requested) — two situations
    Alpaca reports under the same 40310000 code but which need opposite handling."""
    s = str(err)
    brace = s.find("{")
    if brace == -1:
        return None, [], {}
    try:
        data = json.loads(s[brace:])
    except (ValueError, json.JSONDecodeError):
        return None, [], {}
    if not isinstance(data, dict):
        return None, [], {}
    code = data.get("code")
    related = data.get("related_orders")
    related_ids = [str(o) for o in related] if isinstance(related, list) else []
    return (str(code) if code is not None else None), related_ids, data


async def _flatten_available_legs(
    trade, position_fetcher: Callable[..., object] | None,
    position_closer: Callable[..., object] | None,
) -> list[str]:
    """Close each condor leg the broker STILL holds after a combo-order shortfall
    rejection (the worthless side + wings), so nothing lingers into assignment.
    Best-effort and P&L-neutral (closeable legs are near-worthless). Returns the OCC
    symbols actually flattened."""
    if position_fetcher is None or position_closer is None:
        return []
    extra = trade.extra or {}
    legs = [extra.get(k) for k in ("short_put", "long_put", "short_call", "long_call")]
    legs = [occ for occ in legs if occ]
    try:
        positions = await position_fetcher()
    except Exception as e:  # noqa: BLE001 — a fetch blip must not abort error handling
        log.warning("exit_monitor_leg_flatten_fetch_failed", trade_id=trade.id, error=str(e))
        return []
    held: dict[str, int] = {}
    for p in positions:
        sym = getattr(p, "symbol", None)
        try:
            qty = int(Decimal(str(getattr(p, "qty", 0) or 0)))
        except (TypeError, ValueError):
            qty = 0
        if sym and qty != 0:
            held[sym] = qty
    flattened: list[str] = []
    for occ in legs:
        if occ in held:
            try:
                await position_closer(occ)
                flattened.append(occ)
                log.info(
                    "exit_monitor_leg_flattened",
                    trade_id=trade.id, symbol=occ, qty=held[occ],
                )
            except Exception as e:  # noqa: BLE001 — one leg failing must not block the rest
                log.warning(
                    "exit_monitor_leg_flatten_failed",
                    trade_id=trade.id, symbol=occ, error=str(e),
                )
    return flattened


async def _handle_condor_exit_error(
    trade, err: Exception, *, canceller: Callable[..., object],
    position_fetcher: Callable[..., object] | None = None,
    position_closer: Callable[..., object] | None = None,
) -> dict:
    """Turn a broker exception into an isolated, retry-safe result dict.

    Alpaca reports several distinct 0DTE-close failures, and the journal on live
    trades #124/#125/#126 (2026-07-20..22) showed they need different handling —
    the previous code lumped them together and misreported all of them:

    * **Held qty** (40310000, `held_for_orders` > 0 / `related_orders` present):
      a stale resting close order holds the legs. Cancel it so the next sweep can
      resubmit (cancel-replace). This is the only case where cancelling helps.
    * **Position shortfall** (40310000, `held_for_orders` == 0, `available` <
      requested): the broker position has fewer contracts than the trade row's
      qty (partial fill / leg desync upstream). Nothing is held — cancelling does
      nothing. #124 hit this and was mislabeled "held by a resting order
      (cancelled: none found)". Report it accurately; the reconciler settles the
      remainder at expiry.
    * **Intent mismatch** (42210000): the broker won't accept `buy_to_close`
      because it no longer sees the short leg as open at expiry (#125). Expected
      0DTE endgame — the reconciler settles it. Surface calmly, don't cancel.

    All three fall through to reconciler settlement, so realized P&L is unaffected;
    the goal here is accurate, low-noise reporting and cancelling only when it
    actually unblocks a resubmit."""
    err_str = str(err)
    code, related, data = _parse_broker_error(err)

    def _as_int(key: str) -> int | None:
        try:
            return int(str(data.get(key)))
        except (TypeError, ValueError):
            return None

    held_for_orders = _as_int("held_for_orders")
    available = _as_int("available")
    requested = _as_int("qty") or (int(trade.qty) if trade.qty is not None else None)

    is_qty_err = (
        code == "40310000"
        or "insufficient qty" in err_str
        or "held_for_orders" in err_str
    )
    # A quantity IS actually held only when the broker says so (held_for_orders>0)
    # or hands us related order IDs to cancel. held_for_orders==0 with a short
    # `available` is a position shortfall, not a held order — cancelling is a no-op.
    qty_held = is_qty_err and (bool(related) or (held_for_orders or 0) > 0)

    if qty_held:
        for oid in related:
            try:
                await canceller(oid)
                log.warning(
                    "exit_monitor_cancelled_stale_order",
                    trade_id=trade.id, order_id=oid,
                )
            except Exception as ce:  # noqa: BLE001 — cancel is best-effort
                log.warning(
                    "exit_monitor_cancel_failed",
                    trade_id=trade.id, order_id=oid, error=str(ce),
                )
        log.error(
            "exit_monitor_trade_failed",
            trade_id=trade.id, error=err_str,
            error_type=type(err).__name__, cancelled=list(related),
        )
        cancelled = ", ".join(related) if related else "none found"
        return {
            "trade_id": trade.id,
            "status": "submit_error_qty_held",
            "error_sig": f"{trade.id}:qty_held",
            "error_text": (
                f"⚠️ Iron-condor close for trade #{trade.id} blocked — its quantity is "
                f"held by a resting order (cancelled: {cancelled}). Will retry next sweep."
            ),
        }

    # Position shortfall — the broker has fewer contracts than we expect (a leg was
    # pre-processed/exercised at expiry, so the combo order was rejected as a whole).
    # Nothing to cancel. LEG-LEVEL FALLBACK: the OTHER legs (the worthless side +
    # wings) are usually still fully held — flatten whatever IS still open so nothing
    # lingers into assignment/residue. The reconciler settles the exercised remainder;
    # the flattened legs are near-worthless so realized P&L is unaffected.
    if is_qty_err:
        log.warning(
            "exit_monitor_position_shortfall",
            trade_id=trade.id, available=available, requested=requested,
            held_for_orders=held_for_orders, error=err_str,
        )
        flattened = await _flatten_available_legs(trade, position_fetcher, position_closer)
        flat_note = f" Flattened still-open legs: {', '.join(flattened)}." if flattened else ""
        return {
            "trade_id": trade.id,
            "status": "submit_error_qty_short",
            "error_sig": f"{trade.id}:qty_short",
            "flattened_legs": flattened,
            "error_text": (
                f"ℹ️ Iron-condor #{trade.id} — broker position "
                f"({available if available is not None else '?'}) is short of expected "
                f"({requested if requested is not None else '?'}); nothing held to cancel."
                f"{flat_note} Reconciler will settle the remainder at expiry."
            ),
        }

    # Intent mismatch at expiry (42210000) — the broker no longer treats the short
    # leg as closeable. Expected 0DTE endgame; reconciler settles. Calm message.
    if code == "42210000" or "position intent mismatch" in err_str:
        log.warning(
            "exit_monitor_intent_mismatch",
            trade_id=trade.id, error=err_str,
        )
        return {
            "trade_id": trade.id,
            "status": "submit_error_intent_mismatch",
            "error_sig": f"{trade.id}:intent_mismatch",
            "error_text": (
                f"ℹ️ Iron-condor #{trade.id} — broker rejected intraday close at expiry "
                "(position intent mismatch). Reconciler will settle at expiry."
            ),
        }

    log.error(
        "exit_monitor_trade_failed",
        trade_id=trade.id, error=err_str, error_type=type(err).__name__,
    )
    return {
        "trade_id": trade.id,
        "status": "submit_error",
        "error_sig": f"{trade.id}:{code or type(err).__name__}",
        "error_text": (
            f"⚠️ Iron-condor close failed for trade #{trade.id}: "
            f"`{type(err).__name__}: {err}`"
        ),
    }


async def _process_one_condor_exit(
    trade,
    *,
    now: datetime,
    factory: Callable[[], Session],
    chain_fetcher: Callable[..., object],
    submitter: Callable[..., object],
    waiter: Callable[..., object],
    canceller: Callable[..., object],
    stock_fetcher: Callable[..., object],
    single_leg_closer: Callable[..., object] = alpaca_client.submit_single_option_buy_to_close,
    force_close: bool,
    fill_timeout_s: float,
) -> dict:
    """Evaluate one open iron-condor trade and close it if a threshold fired.

    Returns exactly one result dict. Raises only on unexpected broker/IO errors;
    the caller isolates those (see `_handle_condor_exit_error`) so a single stuck
    trade cannot abort the whole sweep."""
    extra = trade.extra or {}
    legs = (
        extra.get("short_put"),
        extra.get("long_put"),
        extra.get("short_call"),
        extra.get("long_call"),
    )
    if not all(legs):
        log.warning("exit_monitor_missing_legs", trade_id=trade.id, extra=extra)
        return {"trade_id": trade.id, "status": "missing_legs"}

    chain = await chain_fetcher(
        "SPY", expiry=trade.opened_at.date()
        if trade.opened_at is not None
        else now.date(),
    )
    exit_debit = _compute_exit_debit_per_contract(
        chain=chain,
        short_put_occ=legs[0],
        long_put_occ=legs[1],
        short_call_occ=legs[2],
        long_call_occ=legs[3],
    )
    if exit_debit is None:
        log.warning("exit_monitor_no_quotes", trade_id=trade.id)
        return {"trade_id": trade.id, "status": "no_quotes"}

    credit = Decimal(str(trade.entry_price))
    # Option B — hard daily loss cap: a per-contract loss ceiling from
    # condor_daily_loss_limit_pct × pool ÷ qty. When enabled, no single day can
    # exceed that % of the pool (the 1.5× stop normally cuts first at a smaller
    # loss; this catches a fast gap that jumped past the stop between sweeps).
    settings = get_settings()
    daily_cap_per_ct: Decimal | None = None
    if settings.condor_daily_loss_limit_pct > 0 and trade.qty:
        cap_total = settings.trading_capital_usd * settings.condor_daily_loss_limit_pct
        daily_cap_per_ct = (cap_total / Decimal(str(trade.qty))).quantize(Decimal("0.01"))
    should_exit, reason = _decide_exit(
        credit_received=credit,
        exit_debit=exit_debit,
        force=force_close,
        daily_loss_cap_per_contract=daily_cap_per_ct,
    )
    if not should_exit:
        return {
            "trade_id": trade.id,
            "status": "hold",
            "credit": str(credit),
            "exit_debit": str(exit_debit),
        }

    # Distance-aware stop (opt-in): the 1.5× stop triggers on the mark-to-market loss
    # alone, so on thin-credit days it fires on the OTM APPROACH — while SPY is still
    # short of a short strike — then SPY reverts and it would have expired for full
    # credit (a whipsaw). When enabled, SUPPRESS the stop (hold) unless SPY is within
    # condor_stop_arm_band_pct of a short strike (or through it) — only cut on a real
    # breach. Only stop_loss reasons are gated: daily_loss_cap is the fast-gap backstop
    # and force_close has its own near-strike logic below. FAIL-SAFE: unknown spot →
    # _near_short_strike True → the stop still fires (we don't gamble on a blind quote).
    if settings.condor_distance_aware_stop and reason.startswith("stop_loss"):
        spot = await _fetch_spot(stock_fetcher, trade.id)
        if not _near_short_strike(spot, legs[0], legs[2], settings.condor_stop_arm_band_pct):
            log.info(
                "exit_monitor_stop_suppressed_inside", trade_id=trade.id,
                spot=str(spot), short_put=legs[0], short_call=legs[2],
                reason=reason, exit_debit=str(exit_debit),
            )
            return {
                "trade_id": trade.id,
                "status": "stop_suppressed_inside",
                "reason": reason,
                "credit": str(credit),
                "exit_debit": str(exit_debit),
            }

    # Smart force-close: the time-based deadline pays the 4-leg exit spread on EVERY
    # open condor. On a position comfortably inside its short strikes — which would
    # expire worthless for free — that spread is pure drag. So when the ONLY reason
    # to exit is the deadline (not a loss-cut), skip the close and let it expire
    # unless SPY is near a short strike (pin/breach risk). Stop / daily-cap exits
    # always close (they mean the position is already losing).
    if reason.startswith("force_close"):
        spot = await _fetch_spot(stock_fetcher, trade.id)
        if not _near_short_strike(spot, legs[0], legs[2]):
            log.info(
                "exit_monitor_expire_inside", trade_id=trade.id,
                spot=str(spot), short_put=legs[0], short_call=legs[2],
            )
            return {
                "trade_id": trade.id,
                "status": "expire_inside",
                "reason": reason,
                "exit_debit": str(exit_debit),
            }
        # Near a short strike at the deadline. ASSIGNMENT CONFIG (opt-in, SPY-only):
        # instead of the blunt 4-leg close, buy back ONLY the at-risk short leg(s)
        # (single-leg, marketable) and let the longs + OTM short expire — cheaper, and
        # it's the short that carries assignment risk. NOTE (pre-deploy): the residual
        # (longs + untouched OTM short) is left to the 16:03 reconciler to settle;
        # verify the reconciler prices a partially-closed condor correctly before
        # enabling. XSP condors never reach here with the flag on (cash-settled).
        if settings.condor_assignment_close:
            return await _assignment_leg_out(
                trade, spot=spot, legs=legs, chain=chain,
                closer=single_leg_closer, waiter=waiter,
                buf=settings.condor_assign_close_buffer_pct,
                fill_timeout_s=fill_timeout_s, factory=factory,
            )

    # Option A — EVERY condor exit closes marketably. All condor exits are
    # loss-cuts or the time-based force-close (there's no profit target), so a
    # fair-value limit that rests unfilled just lets the loss ride to full defined
    # risk (the recurring MLEG close-failure). Submit a cap-marketable limit at the
    # wing width — the intrinsic max cost to close a defined-risk spread — so the
    # order always crosses while never paying more than the max loss we already
    # accepted. The actual fill (final.filled_avg_price) still drives realized P&L.
    # (Previously only force_close_15:50 was marketable, which is why the 1.5× stop
    # so often failed to actually cap the loss.)
    wing = Decimal(str(extra.get("wing_width") or "5"))
    submit_debit = max(exit_debit, (wing * Decimal("100")).quantize(Decimal("0.01")))

    order = await submitter(
        qty=int(Decimal(str(trade.qty))),
        limit_debit_per_contract=submit_debit,
        short_put=legs[0],
        long_put=legs[1],
        short_call=legs[2],
        long_call=legs[3],
    )
    final = await waiter(order.order_id, timeout_s=fill_timeout_s)
    log.info(
        "exit_monitor_close_terminal",
        trade_id=trade.id,
        reason=reason,
        order_id=final.order_id,
        status=final.status,
    )

    actual_debit = exit_debit
    if final.filled_avg_price is not None:
        # Closing an IC is a net DEBIT (buying back the spread). Alpaca returns a
        # positive per-share price for debit fills; take abs as a safety net in
        # case the sign convention varies.
        actual_debit = abs(final.filled_avg_price * Decimal("100")).quantize(
            Decimal("0.01")
        )

    if final.status == "filled":
        with factory() as session:
            row = session.get(Trade, trade.id)
            if row is not None:
                _close_trade_row(
                    session,
                    row,
                    exit_debit_per_contract=actual_debit,
                    order=final,
                    reason=reason,
                )
        signal_text = _format_exit_signal(trade, exit_debit, reason)
        trade_text = _format_exit_telemetry(
            trade, exit_debit=actual_debit, reason=reason
        )
        return {
            "trade_id": trade.id,
            "status": "closed",
            "reason": reason,
            "exit_debit": str(actual_debit),
            "realized_pnl_per_contract": str(credit - actual_debit),
            "signal_text": signal_text,
            "trade_text": trade_text,
        }

    # Submitted but did NOT fill — either a terminal non-filled status
    # (canceled/rejected) or a still-live `new`/`accepted` after the wait timed out.
    # A live unfilled order keeps holding the legs' quantity, so the NEXT sweep's
    # resubmit hits `insufficient qty` (this is exactly how #126, 2026-07-22, left a
    # `new` order resting until the DAY TIF expired it at 16:00). Cancel it here so
    # the position is clean for the reconciler / next attempt. Best-effort: a cancel
    # that fails (already terminal) is harmless.
    if final.status not in _DEAD_ORDER_STATUSES:
        try:
            await canceller(final.order_id)
            log.warning(
                "exit_monitor_cancelled_unfilled_close",
                trade_id=trade.id, order_id=final.order_id, status=final.status,
            )
        except Exception as ce:  # noqa: BLE001 — cancel is best-effort
            log.warning(
                "exit_monitor_cancel_unfilled_failed",
                trade_id=trade.id, order_id=final.order_id, error=str(ce),
            )
    # Route to #logs (throttled) so a repeatedly unfilled close doesn't spam every
    # sweep. The reconciler settles the position at expiry regardless.
    return {
        "trade_id": trade.id,
        "status": f"close_order_{final.status}",
        "reason": reason,
        "error_sig": f"{trade.id}:close_{final.status}",
        "error_text": (
            f"⚠️ Iron-condor close failed — trade #{trade.id} · "
            f"reason `{reason}` · order status `{final.status}`"
        ),
    }
