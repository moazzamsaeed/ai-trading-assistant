"""Directional options exit monitor — hybrid intelligent exit.

Runs every 5 min during RTH. For each open directional Trade row:

  1. Hard floor (−30%): exit immediately, no LLM needed.
  2. Force close on expiry day at 15:30 ET (0DTE every day, weekly on Friday).
  3. Rule-based trigger: fetch bars, compute indicators, check exit signals.
     BUY_CALL rules: price < VWAP, RSI > 70, EMA20 < EMA50, volume fading
     BUY_PUT rules:  price > VWAP, RSI < 30, EMA20 > EMA50, volume fading
  4. If any rule fires → DeepSeek V4-Flash confirms EXIT or HOLD with reasoning.

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

_EXIT_CONFIRM_PROMPT = """You manage an open options position. Decide EXIT or HOLD.

Position:
  Ticker: {ticker} | Action: {action} | Mode: {mode}
  Entry: ${entry_premium}/share | Current bid: ${current_bid}/share
  P&L: {pnl_sign}{pnl_pct}% | Held: {mins_held} min | Expiry: {expiry}

{ticker} indicators right now:
  Price: ${price} | VWAP: {vwap}
  RSI(14): {rsi} | EMA20: {ema20} | EMA50: {ema50}
  Volume ratio: {vol_ratio}

Rules that triggered: {rules}

Original entry reasoning: "{entry_reasoning}"

EXIT if thesis is broken, momentum reversed, or remaining upside is minimal.
HOLD if momentum is intact and further gain is reasonably likely.

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


def _check_exit_rules(action: str, snap: dict) -> list[str]:
    """Return names of triggered exit rules. Empty list = no trigger."""
    triggered: list[str] = []

    price_s = snap.get("last_close")
    if not price_s:
        return triggered  # no bar data yet

    price = float(price_s)
    vwap = float(snap["vwap"]) if snap.get("vwap") else None
    rsi = float(snap["rsi14"]) if snap.get("rsi14") else None
    ema20 = float(snap["ema20"]) if snap.get("ema20") else None
    ema50 = float(snap["ema50"]) if snap.get("ema50") else None
    vol = float(snap["volume_ratio_20"]) if snap.get("volume_ratio_20") else None

    if action == "BUY_CALL":
        if vwap is not None and price < vwap:
            triggered.append("price_below_vwap")
        if rsi is not None and rsi > 70:
            triggered.append("rsi_overbought")
        if ema20 is not None and ema50 is not None and ema20 < ema50:
            triggered.append("ema_bearish_cross")
        if vol is not None and vol < VOLUME_FADE_THRESHOLD:
            triggered.append("volume_fading")
    else:  # BUY_PUT
        if vwap is not None and price > vwap:
            triggered.append("price_above_vwap")
        if rsi is not None and rsi < 30:
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

    pnl_sign = "+" if pnl_pct >= 0 else "-"
    prompt = _EXIT_CONFIRM_PROMPT.format(
        ticker=ticker,
        action=action,
        mode=mode,
        entry_premium=str(entry_premium),
        current_bid=str(current_bid),
        pnl_sign=pnl_sign,
        pnl_pct=f"{abs(pnl_pct):.1f}",
        mins_held=mins_held,
        expiry=expiry,
        price=snap.get("last_close", "?"),
        vwap=snap.get("vwap", "N/A"),
        rsi=snap.get("rsi14", "N/A"),
        ema20=snap.get("ema20", "N/A"),
        ema50=snap.get("ema50", "N/A"),
        vol_ratio=snap.get("volume_ratio_20", "N/A"),
        rules=", ".join(triggered_rules),
        entry_reasoning=entry_reasoning,
    )

    try:
        response = await llm_caller(
            TaskType.INTRADAY_SCAN, prompt, session_factory=session_factory
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
        "hard_floor_stop": "🛑 hard floor −30%",
        "smart_exit": "🧠 smart exit",
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

        current_bid = quote.bid
        pnl_pct = float((current_bid - entry_p) / entry_p * 100)

        # ---- exit decision ----
        should_exit = False
        reason = ""
        llm_reasoning = ""

        if trade_force:
            should_exit, reason = True, "force_close"

        elif current_bid <= entry_p * (Decimal("1") - HARD_FLOOR_PCT):
            should_exit, reason = True, "hard_floor_stop"

        else:
            # Rule-based trigger: fetch bars + indicators for underlying
            snap: dict = {}
            try:
                bars = await bars_fetcher(ticker, timeframe_minutes=5, limit=60)
                snap = indicators.snapshot(bars)
            except Exception as e:  # noqa: BLE001
                log.warning("exit_monitor_bars_failed", trade_id=trade.id, error=str(e))

            triggered = _check_exit_rules(action, snap)
            if triggered:
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
                    reason = "smart_exit"

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
            # Alpaca 42210000: position intent mismatch (position not in broker book)
            # Alpaca 40310000: not eligible for uncovered options (same root cause)
            # Both mean the position is gone from Alpaca — mark closed so we stop retrying.
            if "42210000" in err_str or "40310000" in err_str or "position intent" in err_str or "uncovered" in err_str:
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
