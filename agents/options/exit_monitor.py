"""Iron-condor exit monitor.

Runs every few minutes during RTH. For each open SPY iron-condor `trades`
row, fetches fresh quotes for the four legs, computes the current exit
debit, and submits a closing order when any of these fire:

- **1.5× stop loss**: exit when current debit ≥ 2.5 × credit_received
  (running loss is 1.5× the credit collected; the validated condor stop)
- **Force close at/after 15:50 ET**: time-based; we never hold past close
  (≈ the backtest's close settlement). NO profit target — the condor's edge is
  full-credit expiries, so we hold winners to the force-close.

P&L per contract = entry_credit - exit_debit (positive = profit).
On fill, the `trades` row is updated with exit_price, realized_pnl_usd,
and closed_at.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.options.condor_engine import STOP_MULT, stop_breached
from integrations import alpaca_client
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.timeutils import to_et

log = get_logger(__name__)

STRATEGY_NAME = "spy_0dte_ic"
FORCE_CLOSE_AFTER = time(15, 50)


# ----------------- helpers -----------------


def _open_iron_condor_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.strategy == STRATEGY_NAME, Trade.closed_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


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
) -> tuple[bool, str]:
    """Return (should_exit, reason) — matches the validated condor backtest:
    a 1.5×-credit intraday stop + force-close, and NO profit target (the edge
    comes from full-credit expiries; a 50% PT would cap winners and degrade it).
    Stop fires when buy-back debit ≥ credit × (1 + STOP_MULT)."""
    if force:
        return True, "force_close_15:50"
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
        "stop_loss_2x": "🛑 stop loss — cap the loss now",
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
        extra = trade.extra or {}
        legs = (
            extra.get("short_put"),
            extra.get("long_put"),
            extra.get("short_call"),
            extra.get("long_call"),
        )
        if not all(legs):
            log.warning("exit_monitor_missing_legs", trade_id=trade.id, extra=extra)
            results.append({"trade_id": trade.id, "status": "missing_legs"})
            continue

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
            results.append({"trade_id": trade.id, "status": "no_quotes"})
            continue

        credit = Decimal(str(trade.entry_price))
        should_exit, reason = _decide_exit(
            credit_received=credit,
            exit_debit=exit_debit,
            force=force_close,
        )
        if not should_exit:
            results.append(
                {
                    "trade_id": trade.id,
                    "status": "hold",
                    "credit": str(credit),
                    "exit_debit": str(exit_debit),
                }
            )
            continue

        order = await submitter(
            qty=int(Decimal(str(trade.qty))),
            limit_debit_per_contract=exit_debit,
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
            # Closing an IC is a net DEBIT (buying back the spread). Alpaca
            # returns a positive per-share price for debit fills; take abs as
            # a safety net in case the sign convention varies.
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
            results.append(
                {
                    "trade_id": trade.id,
                    "status": "closed",
                    "reason": reason,
                    "exit_debit": str(actual_debit),
                    "realized_pnl_per_contract": str(credit - actual_debit),
                    "signal_text": signal_text,
                    "trade_text": trade_text,
                }
            )
        else:
            results.append(
                {
                    "trade_id": trade.id,
                    "status": f"close_order_{final.status}",
                    "reason": reason,
                    "trade_text": (
                        f"⚠️ Iron-condor close failed — trade #{trade.id} · "
                        f"reason `{reason}` · order status `{final.status}`"
                    ),
                }
            )

    return results
