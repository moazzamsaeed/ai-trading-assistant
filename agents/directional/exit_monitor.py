"""Directional options exit monitor.

Runs every 5 min during RTH. For each open directional Trade row:
  - fetch current bid via Alpaca options chain
  - bid >= profit_target_premium → close (profit_target)
  - bid <= stop_premium          → close (stop_loss)
  - ET time >= 15:30             → close (force_close)

On a close fill:
  - updates the Trade row (exit_price, realized_pnl_usd, closed_at)
  - returns signal_text for #signals  (manual-mirror exit instructions)
  - returns trade_text for #trades    (bot execution telemetry)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import OrderResult, get_single_option_quote
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
FORCE_CLOSE_AFTER = time(15, 30)
DIRECTIONAL_STRATEGIES = {"directional_call", "directional_put"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_directional_trades(session: Session) -> list[Trade]:
    stmt = select(Trade).where(
        Trade.strategy.in_(DIRECTIONAL_STRATEGIES),
        Trade.closed_at.is_(None),
    )
    return list(session.execute(stmt).scalars())


def _decide_exit(
    *,
    current_bid: Decimal,
    profit_target_premium: Decimal,
    stop_premium: Decimal,
    force: bool,
) -> tuple[bool, str]:
    if force:
        return True, "force_close"
    if current_bid >= profit_target_premium:
        return True, "profit_target"
    if current_bid <= stop_premium:
        return True, "stop_loss"
    return False, ""


def _format_exit_signal(trade: Trade, current_bid: Decimal, reason: str) -> str:
    """Plain-language close instruction for #signals — lets the user mirror manually."""
    extra = trade.extra or {}
    ticker = extra.get("ticker", trade.symbol[:3])
    action = extra.get("action", "BUY_CALL")
    occ = extra.get("occ_symbol", trade.symbol)
    qty = int(trade.qty)

    entry_p = Decimal(str(trade.entry_price))
    pnl_per_share = (current_bid - entry_p).quantize(Decimal("0.01"))
    pnl_total = (pnl_per_share * 100 * qty).quantize(Decimal("0.01"))
    pnl_word = "profit" if pnl_per_share >= 0 else "loss"

    reason_text = {
        "profit_target": "✅ profit target hit",
        "stop_loss": "🛑 stop loss triggered",
        "force_close": "⏰ closing before market close",
    }.get(reason, f"closing ({reason})")

    option_word = "CALL" if action == "BUY_CALL" else "PUT"
    icon = "📈" if action == "BUY_CALL" else "📉"

    return (
        f"{icon} **{ticker} EXIT — {reason_text}** (trade #{trade.id})\n"
        f"\n"
        f"**Sell to close** {qty}× {ticker} {option_word} · contract: `{occ}`\n"
        f"\n"
        f"Current bid: **${current_bid}**/share · Entry was: ${entry_p}/share\n"
        f"Expected {pnl_word}: **${abs(pnl_total)}** "
        f"(${abs(pnl_per_share)}/share × {qty} contracts × 100 shares)"
    )


def _format_exit_telemetry(
    trade: Trade, *, exit_premium: Decimal, reason: str
) -> str:
    extra = trade.extra or {}
    entry_p = Decimal(str(trade.entry_price))
    qty = Decimal(str(trade.qty))
    pnl_per_share = (exit_premium - entry_p).quantize(Decimal("0.01"))
    pnl_total = (pnl_per_share * 100 * qty).quantize(Decimal("0.01"))
    mode = extra.get("mode", "?")
    return (
        f"🤖 **Directional closed** — trade #{trade.id} [{mode.upper()}]\n"
        f"Reason: `{reason}` · entry: ${entry_p}/share · exit: ${exit_premium}/share\n"
        f"P&L: ${pnl_per_share}/share · {qty} contracts · total **${pnl_total}**"
    )


def _close_trade_row(
    session: Session,
    trade: Trade,
    *,
    exit_premium: Decimal,
    order: OrderResult,
    reason: str,
) -> None:
    qty = Decimal(str(trade.qty))
    entry_p = Decimal(str(trade.entry_price))
    trade.exit_price = exit_premium
    trade.realized_pnl_usd = (exit_premium - entry_p) * 100 * qty
    trade.closed_at = datetime.now(UTC)
    extra = dict(trade.extra or {})
    extra["exit_reason"] = reason
    extra["close_order_id"] = order.order_id
    extra["close_status"] = order.status
    trade.extra = extra
    session.commit()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_directional_exit_monitor(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    quote_fetcher: Callable[..., object] = get_single_option_quote,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_sell,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    force_close: bool | None = None,
    fill_timeout_s: float = 60.0,
) -> list[dict]:
    """Sweep all open directional trades. Returns one dict per trade processed."""
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()

    if force_close is None:
        force_close = now.astimezone(ET).time() >= FORCE_CLOSE_AFTER

    with factory() as session:
        trades = _open_directional_trades(session)

    results: list[dict] = []
    if not trades:
        return results

    for trade in trades:
        extra = trade.extra or {}
        occ = extra.get("occ_symbol", trade.symbol)

        quote = await quote_fetcher(occ)
        if quote is None or quote.bid <= 0:
            log.warning("directional_exit_no_quote", trade_id=trade.id, occ=occ)
            results.append({"trade_id": trade.id, "status": "no_quote"})
            continue

        current_bid = quote.bid
        pt_premium = Decimal(str(extra.get("profit_target_premium", "99999")))
        stop_premium = Decimal(str(extra.get("stop_premium", "0")))

        should_exit, reason = _decide_exit(
            current_bid=current_bid,
            profit_target_premium=pt_premium,
            stop_premium=stop_premium,
            force=force_close,
        )
        if not should_exit:
            results.append({
                "trade_id": trade.id,
                "status": "hold",
                "current_bid": str(current_bid),
                "pt": str(pt_premium),
                "stop": str(stop_premium),
            })
            continue

        order = await submitter(
            qty=int(trade.qty),
            occ_symbol=occ,
            limit_price=current_bid,
        )
        final = await waiter(order.order_id, timeout_s=fill_timeout_s)
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
                    )
            signal_text = _format_exit_signal(trade, current_bid, reason)
            trade_text = _format_exit_telemetry(
                trade, exit_premium=exit_premium, reason=reason
            )
            results.append({
                "trade_id": trade.id,
                "status": "closed",
                "reason": reason,
                "exit_premium": str(exit_premium),
                "pnl_per_share": str(
                    (exit_premium - Decimal(str(trade.entry_price))).quantize(Decimal("0.01"))
                ),
                "signal_text": signal_text,
                "trade_text": trade_text,
            })
        else:
            results.append({
                "trade_id": trade.id,
                "status": f"close_order_{final.status}",
                "reason": reason,
                "trade_text": (
                    f"⚠️ Directional close failed — trade #{trade.id} · "
                    f"reason `{reason}` · order status `{final.status}`"
                ),
            })

    return results
