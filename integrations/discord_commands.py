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

from integrations import alpaca_client
from integrations.alpaca_client import AccountSnapshot, PositionSnapshot
from trademaster import risk_manager
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

    with factory() as session:
        signals_today = _today_signals_count(session)
        pnl_today = _today_realized_pnl(session)
        open_positions = risk_manager.count_open_positions(session)

    return (
        f"**TradeMaster status** · {paused_text}\n"
        f"mode: `{settings.trading_mode}` · account_type: `{settings.account_type}`\n"
        f"{account_line}\n"
        f"today: {signals_today} signals · realized P&L: ${pnl_today}\n"
        f"open positions (db): {open_positions} / max {settings.max_concurrent_positions}"
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
) -> str:
    try:
        a = await account_fetcher()
    except Exception as e:  # noqa: BLE001
        log.warning("cash_fetch_failed", error=str(e))
        return f"⚠️ Failed to fetch account: `{type(e).__name__}: {e}`"
    return (
        f"**Cash:** ${a.cash}\n"
        f"**Buying power:** ${a.buying_power}\n"
        f"**Equity:** ${a.equity}\n"
        f"**Portfolio value:** ${a.portfolio_value}"
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
    get_state().last_kill_at = datetime.now(UTC)
    get_state().paused_until = datetime.now(UTC) + timedelta(hours=24)
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
    until = datetime.now(UTC) + timedelta(minutes=minutes)
    get_state().paused_until = until
    return f"⏸ Paused for {minutes} min (until {until.isoformat()})."


# ----------------- /resume -----------------


async def resume() -> str:
    state = get_state()
    if not state.is_paused():
        return "▶ Trading was not paused."
    state.paused_until = None
    return "▶ Trading resumed."
