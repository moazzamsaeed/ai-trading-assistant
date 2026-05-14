"""Slash-command business logic.

Each function returns a formatted text string. Discord coupling (slash
command registration, interaction objects, embeds) lives in `discord_bot.py`.
This split keeps the logic unit-testable without mocking discord.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.options.executor import execute_approved_pending
from integrations import alpaca_client
from integrations.alpaca_client import AccountSnapshot, PositionSnapshot
from trademaster import pending_orders as po
from trademaster import risk_manager, watchlist
from trademaster.config import get_settings
from trademaster.db import Signal, make_session_factory
from trademaster.logging import get_logger
from trademaster.state import get_state

log = get_logger(__name__)


# Injectable for tests.
AccountFetcher = Callable[[], Awaitable[AccountSnapshot]]
PositionsFetcher = Callable[[], Awaitable[list[PositionSnapshot]]]


def _today_signals_count(session: Session) -> int:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count(Signal.id)).where(Signal.created_at >= start)
    return int(session.execute(stmt).scalar_one())


def _today_realized_pnl(session: Session) -> Decimal:
    return risk_manager.realized_pnl_today_usd(session)


# ----------------- /status -----------------


async def status(
    *,
    account_fetcher: AccountFetcher = alpaca_client.get_account,
    session_factory: Callable[[], Session] | None = None,
) -> str:
    factory = session_factory or make_session_factory()
    settings = get_settings()
    state = get_state()
    now = datetime.now(UTC)

    paused = state.is_paused(now)
    paused_text = (
        f"⏸ paused until {state.paused_until.isoformat()}" if paused else "▶ running"
    )

    try:
        account = await account_fetcher()
        account_line = (
            f"cash=${account.cash} · buying_power=${account.buying_power} · "
            f"equity=${account.equity} · multiplier={account.multiplier}"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("status_account_fetch_failed", error=str(e))
        account_line = f"(account fetch failed: {type(e).__name__})"

    from trademaster.capital import directional_deployed_usd, get_effective_capital
    capital = (await get_effective_capital(factory)).quantize(Decimal("0.01"))
    exposure_cap = (capital * Decimal(str(settings.max_total_exposure_pct))).quantize(Decimal("0.01"))

    with factory() as session:
        signals_today = _today_signals_count(session)
        pnl_today = _today_realized_pnl(session).quantize(Decimal("0.01"))
        open_positions = risk_manager.count_open_positions(session)
        # Use directional-only deployed since exposure_cap governs directional.
        deployed = directional_deployed_usd(session).quantize(Decimal("0.01"))

    available = (exposure_cap - deployed).quantize(Decimal("0.01"))

    return (
        f"**TradeMaster status** · {paused_text}\n"
        f"mode: `{settings.trading_mode}` · account_type: `{settings.account_type}`\n"
        f"{account_line}\n"
        f"**capital:** ${capital} (base ${settings.trading_capital_usd} ± realized P&L)\n"
        f"**exposure cap:** ${exposure_cap} ({int(settings.max_total_exposure_pct*100)}%) · "
        f"deployed=${deployed} · available=${available}\n"
        f"today: {signals_today} signals · realized P&L: ${pnl_today}\n"
        f"open positions (db): {open_positions}"
    )


# ----------------- /positions -----------------


async def positions(
    *,
    positions_fetcher: PositionsFetcher = alpaca_client.get_positions,
) -> str:
    try:
        items = await positions_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("positions_fetch_failed", error=str(e))
        return f"⚠️ Failed to fetch positions: `{type(e).__name__}: {e}`"

    if not items:
        return "No open positions."

    lines = ["**Open positions:**"]
    for p in items:
        unreal = f"${p.unrealized_pl:+}"
        lines.append(
            f"`{p.symbol}` · {p.side} {p.qty} @ ${p.avg_entry_price} · "
            f"now ${p.current_price} · unrealized {unreal}"
        )
    return "\n".join(lines)


# ----------------- /cash -----------------


async def cash(
    *,
    account_fetcher: AccountFetcher = alpaca_client.get_account,
    session_factory: Callable[[], Session] | None = None,
) -> str:
    settings = get_settings()
    try:
        a = await account_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("cash_fetch_failed", error=str(e))
        return f"⚠️ Failed to fetch account: `{type(e).__name__}: {e}`"

    factory = session_factory or make_session_factory()
    from trademaster.capital import directional_deployed_usd, get_effective_capital
    with factory() as session:
        deployed = directional_deployed_usd(session).quantize(Decimal("0.01"))

    capital = (await get_effective_capital(factory)).quantize(Decimal("0.01"))
    exposure_cap = (capital * Decimal(str(settings.max_total_exposure_pct))).quantize(Decimal("0.01"))
    available = (exposure_cap - deployed).quantize(Decimal("0.01"))

    return (
        f"**Account (Alpaca):**\n"
        f"• Cash: ${a.cash}\n"
        f"• Buying power: ${a.buying_power}\n"
        f"• Equity: ${a.equity}\n"
        f"• Portfolio value: ${a.portfolio_value}\n"
        f"\n"
        f"**Effective capital ({settings.trading_mode}):**\n"
        f"• Capital: ${capital} (base ${settings.trading_capital_usd} ± realized P&L)\n"
        f"• Exposure cap: ${exposure_cap} ({int(settings.max_total_exposure_pct*100)}%)\n"
        f"• Deployed: ${deployed}\n"
        f"• Available for new trades: ${available}"
    )


# ----------------- /kill -----------------


async def kill(
    *,
    kill_fn: Callable[..., Awaitable[dict]] = risk_manager.kill_all_positions,
    reason: str = "manual /kill command",
) -> str:
    try:
        result = await kill_fn(reason=reason)
    except Exception as e:  # noqa: BLE001
        log.critical("kill_failed", error=str(e))
        return f"🛑 KILL FAILED: `{type(e).__name__}: {e}` — check Alpaca dashboard manually."
    state = get_state()
    state.last_kill_at = datetime.now(UTC)
    state.pause(hours=24)
    return (
        f"🛑 **KILL switch activated.**\n"
        f"Orders cancelled: {result['orders_cancelled']}\n"
        f"Positions closed: {result['positions_closed']}\n"
        f"Trading paused for 24h. Use `/resume` to re-enable earlier."
    )


# ----------------- /pause -----------------


async def pause(minutes: int) -> str:
    if minutes <= 0:
        return "❌ Pause minutes must be positive."
    state = get_state()
    state.pause(minutes=minutes)
    return f"⏸ Paused for {minutes} min (until {state.paused_until.isoformat()})."


# ----------------- /resume -----------------


async def resume() -> str:
    state = get_state()
    if not state.is_paused():
        return "▶ Trading was not paused."
    state.paused_until = None
    return "▶ Trading resumed."


# ----------------- /pending /approve /reject -----------------


def _format_pending_summary(row) -> str:
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    minutes_left = int(
        max(0, (expires_at - datetime.now(UTC)).total_seconds() // 60)
    )
    return (
        f"**Pending #{row.id}** · `{row.strategy}` · expires in {minutes_left} min\n"
        f"{row.summary}"
    )


async def pending(
    *,
    session_factory: Callable[[], Session] | None = None,
) -> str:
    """List all pending live-mode trade approvals."""
    factory = session_factory or make_session_factory()
    with factory() as s:
        rows = po.list_pending(s)
    if not rows:
        return "No pending trades."
    blocks = [_format_pending_summary(r) for r in rows]
    return "**Pending approvals:**\n\n" + "\n\n---\n\n".join(blocks)


async def reject(
    pending_id: int,
    *,
    user_label: str,
    session_factory: Callable[[], Session] | None = None,
) -> str:
    factory = session_factory or make_session_factory()
    with factory() as s:
        ok = po.mark_rejected(s, pending_id, decided_by=user_label)
    if not ok:
        return f"❌ Pending #{pending_id} not found or not in `pending` state."
    log.info("pending_rejected", pending_id=pending_id, decided_by=user_label)
    return f"🛑 Rejected pending #{pending_id}."


async def approve(
    pending_id: int,
    *,
    user_label: str,
    session_factory: Callable[[], Session] | None = None,
    executor=execute_approved_pending,
) -> str:
    """Approve a pending order: reconstruct the plan and submit to Alpaca.

    Returns a human-readable status string ready to post to #trades.
    """
    factory = session_factory or make_session_factory()
    try:
        result = await executor(
            pending_id, decided_by=user_label, session_factory=factory
        )
    except Exception as e:  # noqa: BLE001
        log.error(
            "pending_approve_failed",
            pending_id=pending_id,
            error=str(e),
            error_type=type(e).__name__,
        )
        return f"⚠️ /approve {pending_id} failed: `{type(e).__name__}: {e}`"

    if result.executed:
        return (
            f"✅ Approved pending #{pending_id} · submitted · "
            f"{result.reason} · trade #{result.trade_id}"
        )
    return f"⚠️ Approved pending #{pending_id} · NOT executed · {result.reason}"


# ----------------- /watchlist /watchlist_add /watchlist_remove -----------------


def _format_watchlist(tickers: list[str]) -> str:
    if not tickers:
        return "**Watchlist:** _(empty)_"
    return f"**Watchlist ({len(tickers)}):** " + " · ".join(f"`{t}`" for t in tickers)


async def watchlist_show() -> str:
    """`/watchlist` — list current tickers."""
    return _format_watchlist(watchlist.list_tickers())


async def watchlist_add(ticker: str) -> tuple[str, list[str], bool]:
    """`/watchlist_add SYM` — returns (reply_text, current_list, was_added)."""
    try:
        listing, added = watchlist.add_ticker(ticker)
    except ValueError as e:
        return f"❌ {e}", watchlist.list_tickers(), False
    if added:
        return f"✅ Added `{ticker.upper()}`.\n" + _format_watchlist(listing), listing, True
    return f"ℹ️ `{ticker.upper()}` was already in the watchlist.", listing, False


async def watchlist_remove(ticker: str) -> tuple[str, list[str], bool]:
    """`/watchlist_remove SYM` — returns (reply_text, current_list, was_removed)."""
    try:
        listing, removed = watchlist.remove_ticker(ticker)
    except ValueError as e:
        return f"❌ {e}", watchlist.list_tickers(), False
    if removed:
        return f"✅ Removed `{ticker.upper()}`.\n" + _format_watchlist(listing), listing, True
    return f"ℹ️ `{ticker.upper()}` was not in the watchlist.", listing, False
