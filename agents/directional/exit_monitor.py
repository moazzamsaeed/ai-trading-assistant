"""Directional options exit monitor — indicator-driven intelligent exit.

Runs every 5 min during RTH. For each open directional Trade row:

  1. Hard floor (mode-aware): exit immediately, no LLM.
       aggressive: −50% | selective: −30%
  2. Force close on expiry day at 15:30 ET (0DTE every day, weekly on Friday).
  3. Trailing stop (ratcheting): once profit hits a tier, stop is raised to
       lock in gains. Tiers: +30%→+10%, +50%→+25%, +75%→+40%, +100%→+60%.
       Stop only ever moves UP. Persisted to DB — survives daemon restarts.
  4. Stop premium: hard stop set at entry (−50% aggressive / −30% selective).
  5. Indicator scan + LLM confirmation — two triggers:
       a. Any reversal rule fires (thesis protection, loss or gain)
       b. P&L ≥ PROFIT_LOCK_PCT (75%): always consult LLM even if no rules fired
     BUY_CALL rules: price < VWAP, RSI > 75, EMA20 < EMA50, volume fading
     BUY_PUT rules:  price > VWAP, RSI < 25, EMA20 > EMA50, volume fading
  6. LLM decides EXIT or HOLD with full context: P&L, indicators, mode, expiry.
     - No hard profit target — LLM + indicators govern when to take profit.
     - Selective mode: one fading indicator in profit zone is enough to exit.
     - Aggressive mode: require ≥2 confirming signals before exiting a winner.

On a close fill:
  - updates the Trade row
  - returns combined_text: single Discord message for #signals (manual close
    instruction + P&L summary in one)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import (
    OrderResult,
    get_recent_bars,
    get_single_option_quote,
    parse_occ_symbol,
)
from trademaster import indicators
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.router import TaskType, route_to_model
from trademaster.timeutils import to_et

log = get_logger(__name__)

FORCE_CLOSE_AFTER = time(15, 30)
DIRECTIONAL_STRATEGIES = {"directional_call", "directional_put"}
HARD_FLOOR_PCT = Decimal("0.30")   # −30%: exit unconditionally, no LLM
VOLUME_FADE_THRESHOLD = 0.7        # volume_ratio below this = momentum fading
PROFIT_LOCK_PCT = 75.0             # always consult LLM above this P&L even with no indicator triggers

# Trailing stop tiers: (profit_trigger_pct, lock_in_pct)
# Once the position reaches trigger_pct gain, the stop is ratcheted up to
# lock_in_pct gain. The stop only ever moves UP — never loosens.
# Example: position hits +50% → stop moves up to entry × 1.25 (+25%).
#          If price then falls to +24% → exits immediately, locking in gain.
TRAILING_STOP_LEVELS: list[tuple[float, float]] = [
    (100.0, 0.60),  # reached +100% → protect +60%
    (75.0,  0.40),  # reached +75%  → protect +40%
    (50.0,  0.25),  # reached +50%  → protect +25%
    (30.0,  0.10),  # reached +30%  → protect +10%
]

_EXIT_CONFIRM_PROMPT = """You manage an open options position. Decide EXIT or HOLD.

Position:
  Ticker: {ticker} | Action: {action} | Mode: {mode}
  Entry: ${entry_premium}/share | Current bid: ${current_bid}/share
  P&L: {pnl_sign}{pnl_pct}% | Held: {mins_held} min | Expiry: {expiry}
  Reference targets: PT ≥${profit_target}/share · Stop ≤${stop_ref}/share

{ticker} indicators right now:
  Price: ${price} | VWAP: {vwap}
  RSI(9): {rsi} | EMA20: {ema20} | EMA50: {ema50}
  Volume ratio: {vol_ratio}

Rules triggered: {rules}

Original entry reasoning: "{entry_reasoning}"

--- EXIT FRAMEWORK ---

LOSS SIDE — exit if thesis is broken:
  • Price broke VWAP against position direction
  • EMA cross confirmed reversal
  • RSI flipped extreme opposite direction + volume fading

PROFIT SIDE — no hard target, use indicator confluence:
  • P&L 30–74%: exit if ≥2 indicators show fading momentum
  • P&L ≥75%: exit if ANY single indicator shows fading momentum
  • P&L ≥150%: EXIT unless ALL indicators unanimously confirm continuation (rare — capture the gain)
  • Volume fade alone while in profit = smart money distributing into your position → EXIT
  • RSI exhaustion + volume fade = momentum spent → EXIT

MODE CONTEXT — {mode_guidance}

TIME CONTEXT — factor in expiry. Theta accelerates in final 90 min.
  If expiry is today and P&L is positive, lean EXIT over HOLD.

HOLD only if: momentum is intact across multiple indicators, thesis unchanged, and remaining upside clearly justifies the risk of giving back current gains.

Respond with JSON only: {{"decision": "EXIT"|"HOLD", "reason": "one brief sentence"}}"""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _open_directional_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.strategy.in_(DIRECTIONAL_STRATEGIES),
        Trade.closed_at.is_(None),
    )
    return list(session.execute(stmt).scalars())


def _close_trade_row(
    session: Session,
    trade: Trade,
    *,
    exit_premium: Decimal,
    order: OrderResult | None,
    reason: str,
    llm_reasoning: str = "",
) -> None:
    qty = Decimal(str(trade.qty))
    entry_p = Decimal(str(trade.entry_price))
    trade.exit_price = exit_premium
    trade.realized_pnl_usd = (exit_premium - entry_p) * 100 * qty
    trade.closed_at = datetime.now(UTC)
    extra = dict(trade.extra or {})
    extra["exit_reason"] = reason
    extra["exit_reasoning"] = llm_reasoning
    extra["close_order_id"] = order.order_id if order else None
    extra["close_status"] = order.status if order else "broker_error"
    trade.extra = extra
    session.commit()


# ---------------------------------------------------------------------------
# Indicator-based exit rules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------


def _trailing_stop_premium(
    entry_premium: Decimal,
    peak_pnl_pct: float,
) -> Decimal | None:
    """Return the stop price that locks in gains at the current peak level.

    Walks TRAILING_STOP_LEVELS from highest to lowest and returns the first
    tier whose trigger has been reached. Returns None if peak is below the
    lowest trigger (+30%).
    """
    for trigger_pct, lock_pct in TRAILING_STOP_LEVELS:
        if peak_pnl_pct >= trigger_pct:
            return (entry_premium * (Decimal("1") + Decimal(str(lock_pct)))).quantize(
                Decimal("0.0001")
            )
    return None


def _maybe_ratchet_trailing_stop(
    session_factory,
    trade: Trade,
    current_pnl_pct: float,
    entry_premium: Decimal,
) -> Decimal | None:
    """Update peak P&L and ratchet the trailing stop if a new high-water mark
    has been set. Returns the new stop_premium if the stop was ratcheted, else
    None. The stop only ever moves UP — it never loosens.

    Persists both peak_pnl_pct and stop_premium to the DB so the ratcheted
    stop survives a daemon restart.
    """
    extra = trade.extra or {}
    old_peak = float(extra.get("peak_pnl_pct", 0.0))
    new_peak = max(old_peak, current_pnl_pct)

    new_trail = _trailing_stop_premium(entry_premium, new_peak)

    current_stop_raw = extra.get("stop_premium")
    current_stop = Decimal(str(current_stop_raw)) if current_stop_raw else Decimal("-1")

    peak_moved = new_peak > old_peak
    stop_ratcheted = new_trail is not None and new_trail > current_stop

    if not peak_moved and not stop_ratcheted:
        return None

    with session_factory() as session:
        row = session.get(Trade, trade.id)
        if row is None:
            return None
        new_extra = dict(row.extra or {})
        new_extra["peak_pnl_pct"] = round(new_peak, 2)
        if stop_ratcheted:
            new_extra["stop_premium"] = str(new_trail)
            new_extra["trailing_stop_active"] = True
            log.info(
                "trailing_stop_ratcheted",
                trade_id=trade.id,
                peak_pnl_pct=f"{new_peak:.1f}%",
                old_stop=str(current_stop),
                new_stop=str(new_trail),
                entry_premium=str(entry_premium),
            )
        row.extra = new_extra
        session.commit()

    return new_trail if stop_ratcheted else None


# ---------------------------------------------------------------------------
# Indicator-based exit rules
# ---------------------------------------------------------------------------


def _check_exit_rules(action: str, snap: dict) -> list[str]:
    """Return names of triggered exit rules. Empty list = no trigger."""
    triggered: list[str] = []

    price_s = snap.get("last_close")
    if not price_s:
        return triggered  # no bar data yet

    price = float(price_s)
    vwap = float(snap["vwap"]) if snap.get("vwap") else None
    rsi = float(snap["rsi9"]) if snap.get("rsi9") else None
    ema20 = float(snap["ema20"]) if snap.get("ema20") else None
    ema50 = float(snap["ema50"]) if snap.get("ema50") else None
    vol = float(snap["volume_ratio_20"]) if snap.get("volume_ratio_20") else None

    if action == "BUY_CALL":
        if vwap is not None and price < vwap:
            triggered.append("price_below_vwap")
        if rsi is not None and rsi > 75:   # RSI-9: overbought = 75 (wider than RSI-14's 70)
            triggered.append("rsi_overbought")
        if ema20 is not None and ema50 is not None and ema20 < ema50:
            triggered.append("ema_bearish_cross")
        if vol is not None and vol < VOLUME_FADE_THRESHOLD:
            triggered.append("volume_fading")
    else:  # BUY_PUT
        if vwap is not None and price > vwap:
            triggered.append("price_above_vwap")
        if rsi is not None and rsi < 25:   # RSI-9: oversold = 25 (wider than RSI-14's 30)
            triggered.append("rsi_oversold")
        if ema20 is not None and ema50 is not None and ema20 > ema50:
            triggered.append("ema_bullish_cross")
        if vol is not None and vol < VOLUME_FADE_THRESHOLD:
            triggered.append("volume_fading")

    return triggered


# ---------------------------------------------------------------------------
# LLM exit confirmation
# ---------------------------------------------------------------------------


async def _llm_exit_confirm(
    *,
    trade: Trade,
    snap: dict,
    triggered_rules: list[str],
    current_bid: Decimal,
    pnl_pct: float,
    session_factory,
    llm_caller=route_to_model,
) -> tuple[bool, str]:
    """Ask DeepSeek V4-Flash whether to exit. Returns (should_exit, reason)."""
    extra = trade.extra or {}
    ticker = extra.get("ticker", "?")
    action = extra.get("action", "BUY_CALL")
    entry_premium = Decimal(str(trade.entry_price))
    mode = extra.get("mode", "selective")
    entry_reasoning = extra.get("entry_reasoning", "momentum setup")

    occ = extra.get("occ_symbol", trade.symbol)
    try:
        _, expiry_date, _, _ = parse_occ_symbol(occ)
        expiry = expiry_date.isoformat()
    except ValueError:
        expiry = "unknown"

    opened_at = trade.opened_at
    if opened_at and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    mins_held = (
        int((datetime.now(UTC) - opened_at).total_seconds() / 60)
        if opened_at else "?"
    )

    profit_target = extra.get("profit_target_premium", "N/A")
    stop_ref = extra.get("stop_premium", "N/A")
    mode_guidance = (
        "AGGRESSIVE — let winners run. Require confluence of ≥2 fading indicators "
        "before exiting a profitable position. Single signals are noise."
        if mode == "aggressive" else
        "SELECTIVE — protect gains. A single strong reversal signal while in profit "
        "is sufficient to EXIT. Don't give back gains chasing more."
    )

    pnl_sign = "+" if pnl_pct >= 0 else "-"
    prompt = _EXIT_CONFIRM_PROMPT.format(
        ticker=ticker,
        action=action,
        mode=mode.upper(),
        entry_premium=str(entry_premium),
        current_bid=str(current_bid),
        pnl_sign=pnl_sign,
        pnl_pct=f"{abs(pnl_pct):.1f}",
        mins_held=mins_held,
        expiry=expiry,
        profit_target=profit_target,
        stop_ref=stop_ref,
        price=snap.get("last_close", "?"),
        vwap=snap.get("vwap", "N/A"),
        rsi=snap.get("rsi9", "N/A"),
        ema20=snap.get("ema20", "N/A"),
        ema50=snap.get("ema50", "N/A"),
        vol_ratio=snap.get("volume_ratio_20", "N/A"),
        rules=", ".join(triggered_rules) if triggered_rules else "none — profit zone check",
        entry_reasoning=entry_reasoning,
        mode_guidance=mode_guidance,
    )

    try:
        response = await llm_caller(
            TaskType.EXIT_DECISION, prompt, session_factory=session_factory
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.startswith("```")
            )
        data = json.loads(text)
        decision = str(data.get("decision", "HOLD")).upper()
        reason = str(data.get("reason", ""))[:200]
        log.info(
            "exit_confirm_llm",
            trade_id=trade.id,
            decision=decision,
            reason=reason,
            rules=triggered_rules,
        )
        return decision == "EXIT", reason
    except Exception as e:  # noqa: BLE001
        log.warning("exit_confirm_llm_failed", trade_id=trade.id, error=str(e))
        return False, ""  # on failure, HOLD


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _format_exit_combined(
    trade: Trade,
    exit_premium: Decimal,
    reason: str,
    llm_reasoning: str = "",
) -> str:
    """Single #signals message combining manual close instruction + P&L."""
    extra = trade.extra or {}
    ticker = extra.get("ticker", trade.symbol[:3])
    action = extra.get("action", "BUY_CALL")
    occ = extra.get("occ_symbol", trade.symbol)
    qty = int(trade.qty)
    mode = extra.get("mode", "selective")

    entry_p = Decimal(str(trade.entry_price))
    pnl_per_share = (exit_premium - entry_p).quantize(Decimal("0.01"))
    pnl_total = (pnl_per_share * 100 * qty).quantize(Decimal("0.01"))
    is_profit = pnl_per_share >= 0
    pnl_icon = "✅" if is_profit else "❌"
    pnl_pct = abs(int(pnl_per_share / entry_p * 100)) if entry_p else 0

    reason_text = {
        "hard_floor_stop": "🛑 hard floor stop",
        "stop_premium": "🛑 stop hit — limiting loss",
        "trailing_stop": "🔒 trailing stop — locking in gains",
        "smart_exit": "🧠 smart exit — thesis reversed",
        "smart_profit_exit": "💰 smart profit exit — indicators say take it",
        "force_close": "⏰ closing before market close",
    }.get(reason, f"closing ({reason})")

    option_word = "CALL" if action == "BUY_CALL" else "PUT"
    icon = "📈" if action == "BUY_CALL" else "📉"

    lines = [
        f"{icon} **{ticker} {option_word} — bot closed** [{mode.upper()}]",
        "",
        f"Reason: {reason_text}",
    ]
    if llm_reasoning:
        lines.append(f"_{llm_reasoning}_")
    lines += [
        "",
        f"Bot sold: {qty}× `{occ}` @ **${exit_premium}**/share",
        f"**Manual close: Sell {qty}× {ticker} {option_word} at market**",
        "",
        f"P&L: **{pnl_icon} ${abs(pnl_total)}** "
        f"({pnl_pct}% {'gain' if is_profit else 'loss'} · "
        f"${abs(pnl_per_share)}/share × {qty} contracts × 100)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_directional_exit_monitor(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    quote_fetcher: Callable[..., object] = get_single_option_quote,
    bars_fetcher: Callable[..., object] = get_recent_bars,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_sell,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    llm_caller: Callable[..., object] = route_to_model,
    force_close: bool | None = None,
    fill_timeout_s: float = 60.0,
) -> list[dict]:
    """Sweep all open directional trades. Returns one dict per trade processed."""
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()

    et_now = to_et(now)
    past_force_close_time = et_now.time() >= FORCE_CLOSE_AFTER
    today = et_now.date()
    global_force = force_close  # None → decide per-trade

    with factory() as session:
        trades = _open_directional_trades(session)

    results: list[dict] = []
    if not trades:
        return results

    for trade in trades:
        extra = trade.extra or {}
        occ = extra.get("occ_symbol", trade.symbol)
        action = extra.get("action", "BUY_CALL")
        ticker = extra.get("ticker", occ[:3])
        entry_p = Decimal(str(trade.entry_price))

        # ---- determine per-trade force flag ----
        if global_force is not None:
            trade_force = global_force
        elif past_force_close_time:
            try:
                _, expiry, _, _ = parse_occ_symbol(occ)
                trade_force = expiry == today
            except ValueError:
                trade_force = True
        else:
            trade_force = False

        # ---- get current option price ----
        quote = await quote_fetcher(occ)
        if quote is None or quote.bid <= 0:
            log.warning("directional_exit_no_quote", trade_id=trade.id, occ=occ)
            results.append({"trade_id": trade.id, "status": "no_quote"})
            continue

        # Broken quote guard: if ask > 5× bid the data feed is corrupted
        # (e.g. stale last-trade price, crossed market, or Alpaca data gap).
        # Trading on corrupt quotes would compute a wildly wrong P&L and
        # potentially trigger a false hard-floor exit.
        if quote.ask > quote.bid * 5 and quote.bid > 0:
            log.warning(
                "directional_exit_quote_sanity_fail",
                trade_id=trade.id, occ=occ,
                bid=str(quote.bid), ask=str(quote.ask),
            )
            results.append({"trade_id": trade.id, "status": "stale_quote"})
            continue

        current_bid = quote.bid
        pnl_pct = float((current_bid - entry_p) / entry_p * 100)

        # ---- trailing stop ratchet ----
        # Update the high-water mark and tighten stop_premium if a new profit
        # tier has been reached. Runs every cycle — cheap (no LLM, one DB write
        # only when the stop actually moves). Must happen before exit checks so
        # the ratcheted stop is evaluated in the same cycle it was set.
        ratcheted = _maybe_ratchet_trailing_stop(factory, trade, pnl_pct, entry_p)

        # ---- exit decision ----
        should_exit = False
        reason = ""
        llm_reasoning = ""

        # Reload stop_p from DB if it was just ratcheted, otherwise use extra.
        if ratcheted is not None:
            stop_p = ratcheted
        else:
            stop_p_raw = extra.get("stop_premium")
            stop_p = Decimal(str(stop_p_raw)) if stop_p_raw else None
        trade_mode = extra.get("mode", "selective")
        # Hard floor is mode-aware: selective uses −30% (same as its stop), aggressive
        # uses −50% to match its wider stop. Both act as unconditional floors with no LLM.
        hard_floor = Decimal("0.50") if trade_mode == "aggressive" else HARD_FLOOR_PCT

        if trade_force:
            should_exit, reason = True, "force_close"

        elif current_bid <= entry_p * (Decimal("1") - hard_floor):
            should_exit, reason = True, "hard_floor_stop"

        elif stop_p is not None and current_bid <= stop_p:
            # Distinguish trailing stop (protecting gains) from original hard stop (limiting losses)
            trailing_active = (extra.get("trailing_stop_active") or ratcheted is not None)
            reason = "trailing_stop" if trailing_active else "stop_premium"
            should_exit = True
            log.info(
                "directional_exit_stop_hit",
                trade_id=trade.id,
                occ=occ,
                reason=reason,
                current_bid=str(current_bid),
                stop_premium=str(stop_p),
            )

        else:
            # Fetch bars + indicators once — used for both reversal and profit checks.
            snap: dict = {}
            try:
                bars = await bars_fetcher(ticker, timeframe_minutes=5, limit=60)
                snap = indicators.snapshot(bars)
            except Exception as e:  # noqa: BLE001
                log.warning("exit_monitor_bars_failed", trade_id=trade.id, error=str(e))

            triggered = _check_exit_rules(action, snap)
            in_strong_profit = pnl_pct >= PROFIT_LOCK_PCT

            # Consult LLM if: (a) any indicator rule fired, OR (b) P&L is high enough
            # that we should proactively check whether to take the gain.
            if triggered or in_strong_profit:
                should_exit, llm_reasoning = await _llm_exit_confirm(
                    trade=trade,
                    snap=snap,
                    triggered_rules=triggered,
                    current_bid=current_bid,
                    pnl_pct=pnl_pct,
                    session_factory=factory,
                    llm_caller=llm_caller,
                )
                if should_exit:
                    reason = "smart_profit_exit" if pnl_pct > 0 else "smart_exit"
                    log.info(
                        "directional_exit_smart",
                        trade_id=trade.id,
                        reason=reason,
                        pnl_pct=f"{pnl_pct:+.1f}%",
                        triggered_rules=triggered,
                        in_strong_profit=in_strong_profit,
                    )

        if not should_exit:
            results.append({
                "trade_id": trade.id,
                "status": "hold",
                "current_bid": str(current_bid),
                "pnl_pct": f"{pnl_pct:+.1f}%",
            })
            continue

        # ---- submit close order ----
        try:
            order = await submitter(
                qty=int(trade.qty),
                occ_symbol=occ,
                limit_price=current_bid,
            )
            final = await waiter(order.order_id, timeout_s=fill_timeout_s)
        except Exception as e:  # noqa: BLE001
            err_str = str(e)
            # Auto-close the DB row only when Alpaca confirms the position is genuinely
            # gone from the broker's book. Match on message text, not just error code —
            # 42210000 is also returned for "IOC not supported" which does NOT mean the
            # position is absent (it's a TIF rejection, position still exists).
            if (
                "position intent" in err_str
                or "uncovered" in err_str
                or "40310000" in err_str
                or ("42210000" in err_str and "time_in_force" not in err_str)
            ):
                # Verify the position is genuinely absent before auto-closing.
                # Alpaca paper sometimes rejects SELL_TO_CLOSE with "position intent
                # mismatch" even when the position IS in the book (timing/tracking
                # inconsistency). Only auto-close if get_positions() confirms it's gone.
                try:
                    live_positions = await alpaca_client.get_positions()
                    still_live = any(
                        getattr(p, "symbol", "") == occ for p in live_positions
                    )
                except Exception:  # noqa: BLE001
                    still_live = False  # can't verify — be conservative and close

                if still_live:
                    # Position IS in Alpaca — the error was a transient rejection.
                    # Log the warning but leave the DB row open to retry next cycle.
                    log.warning(
                        "directional_exit_sell_rejected_position_exists",
                        trade_id=trade.id, occ=occ, error=err_str,
                    )
                    results.append({
                        "trade_id": trade.id,
                        "status": "sell_rejected_retry_next_cycle",
                        "error_text": f"⚠️ Exit rejected for trade #{trade.id} ({occ}) but position exists — will retry.",
                    })
                else:
                    log.warning(
                        "directional_exit_position_not_in_broker",
                        trade_id=trade.id, occ=occ, error=err_str,
                    )
                    with factory() as session:
                        row = session.get(Trade, trade.id)
                        if row is not None:
                            _close_trade_row(
                                session, row,
                                exit_premium=current_bid,
                                order=None,
                                reason="position_not_in_broker",
                                llm_reasoning="Position not found in Alpaca — auto-closed to stop retry loop",
                            )
                    results.append({
                        "trade_id": trade.id,
                        "status": "closed_position_not_in_broker",
                        "error_text": (
                            f"⚠️ Trade #{trade.id} ({occ}) not found in Alpaca broker — "
                            f"marked closed to stop retry loop. Check paper account."
                        ),
                    })
            else:
                log.error("directional_exit_submit_failed", trade_id=trade.id, error=err_str)
                results.append({
                    "trade_id": trade.id,
                    "status": "submit_error",
                    "error_text": f"⚠️ Exit order failed for trade #{trade.id}: `{err_str}`",
                })
            continue

        log.info(
            "directional_exit_terminal",
            trade_id=trade.id,
            reason=reason,
            order_id=final.order_id,
            status=final.status,
        )

        exit_premium = (
            final.filled_avg_price
            if final.filled_avg_price is not None
            else current_bid
        )

        if final.status == "filled":
            with factory() as session:
                row = session.get(Trade, trade.id)
                if row is not None:
                    _close_trade_row(
                        session, row,
                        exit_premium=exit_premium,
                        order=final,
                        reason=reason,
                        llm_reasoning=llm_reasoning,
                    )
            combined_text = _format_exit_combined(
                trade, exit_premium, reason, llm_reasoning
            )
            results.append({
                "trade_id": trade.id,
                "status": "closed",
                "reason": reason,
                "exit_premium": str(exit_premium),
                "pnl_per_share": str(
                    (exit_premium - entry_p).quantize(Decimal("0.01"))
                ),
                "combined_text": combined_text,
            })
        else:
            results.append({
                "trade_id": trade.id,
                "status": f"close_order_{final.status}",
                "reason": reason,
                "error_text": (
                    f"⚠️ Directional close failed — trade #{trade.id} · "
                    f"reason `{reason}` · order status `{final.status}`"
                ),
            })

    return results
