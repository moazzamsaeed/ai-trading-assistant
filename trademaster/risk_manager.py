"""Risk manager — pure Python, no LLM in the loop.

Enforces the hard constraints documented in `docs/DECISIONS.md` D-001 and
D-007. The LLM proposes; the risk manager disposes.

Every rejection is logged to `risk_events`. Every approval also leaves a
`risk_events` row with severity=info, so the audit trail is complete.

The check order in `validate_signal` matters: cheaper / earlier-failing
checks run first to keep API call volume down on the common rejection paths.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import AccountSnapshot
from trademaster.config import get_settings
from trademaster.db import RiskEvent, Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.models import AssetClass, Side, Signal, SignalAction, TradeOrder

log = get_logger(__name__)


class RiskRejectionError(Exception):
    """Raised when an agent signal violates a hard risk constraint."""


CASH_MULTIPLIER = "1"


# ----------------------- helpers -----------------------


def _record(
    session: Session,
    *,
    event_type: str,
    severity: str,
    reason: str,
    signal_id: int | None = None,
    details: dict | None = None,
) -> None:
    session.add(
        RiskEvent(
            event_type=event_type,
            severity=severity,
            reason=reason,
            signal_id=signal_id,
            details=details or {},
        )
    )
    session.commit()


def _start_of_today_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def realized_pnl_today_usd(session: Session, now: datetime | None = None) -> Decimal:
    """Sum of `realized_pnl_usd` for trades closed today (UTC).

    Negative values represent losses; the daily loss check compares
    `-pnl >= limit` to halt.
    """
    start = _start_of_today_utc(now)
    stmt = select(func.coalesce(func.sum(Trade.realized_pnl_usd), 0)).where(
        Trade.closed_at.isnot(None), Trade.closed_at >= start
    )
    total = session.execute(stmt).scalar_one()
    return Decimal(str(total))


def count_open_positions(session: Session) -> int:
    """Count of currently open positions (closed_at IS NULL)."""
    stmt = select(func.count(Trade.id)).where(Trade.closed_at.is_(None))
    return int(session.execute(stmt).scalar_one())


# ----------------------- defined-risk check -----------------------


def _is_defined_risk(order: TradeOrder) -> tuple[bool, str]:
    """Reject any options order that isn't a defined-risk structure (D-001).

    Defined risk means every short option leg is covered by a long option leg
    of the same type (call/put) further OTM. Iron condors and vertical spreads
    pass; naked calls, naked puts, and undefined butterflies do not.

    Returns (ok, reason).
    """
    if order.asset_class is not AssetClass.OPTION:
        return True, ""

    legs = order.legs or []
    if not legs:
        return False, "option order has no legs"

    shorts = [leg for leg in legs if leg.side is Side.SELL]
    longs = [leg for leg in legs if leg.side is Side.BUY]

    if not shorts:
        return True, "no short legs (long-only options spread)"
    if len(longs) < len(shorts):
        return False, f"naked option exposure: {len(shorts)} shorts, {len(longs)} longs"

    # Each short must be matched by a long of the same option_type and same expiry.
    for short_leg in shorts:
        matched = any(
            (
                long_leg.option_type == short_leg.option_type
                and long_leg.expiry == short_leg.expiry
                and long_leg.qty >= short_leg.qty
            )
            for long_leg in longs
        )
        if not matched:
            return False, (
                f"short {short_leg.option_type} @ {short_leg.strike} "
                f"({short_leg.expiry}) has no covering long leg"
            )
    return True, "defined-risk structure verified"


# ----------------------- public API -----------------------


async def validate_account_is_cash(
    *,
    account_fetcher: Callable[[], Awaitable[AccountSnapshot]] = alpaca_client.get_account,
    session_factory: Callable[[], Session] | None = None,
) -> AccountSnapshot:
    """Refuse to start if the live Alpaca account is anything other than cash.

    Called once at TradeMaster startup. Raises `RiskRejectionError` on any
    of: non-cash multiplier, trading_blocked, account_blocked, status not
    ACTIVE. Records a risk_event for the rejection.
    """
    account = await account_fetcher()
    factory = session_factory or make_session_factory()

    problems: list[str] = []
    if account.multiplier != CASH_MULTIPLIER:
        problems.append(
            f"account.multiplier={account.multiplier!r} (expected '1' for cash, D-001)"
        )
    if account.account_blocked:
        problems.append("account_blocked=True")
    if account.trading_blocked:
        problems.append("trading_blocked=True")
    if account.status.upper() != "ACTIVE":
        problems.append(f"account.status={account.status!r} (expected ACTIVE)")

    if problems:
        reason = "; ".join(problems)
        with factory() as session:
            _record(
                session,
                event_type="account_check_failed",
                severity="critical",
                reason=reason,
                details={
                    "multiplier": account.multiplier,
                    "status": account.status,
                    "account_blocked": account.account_blocked,
                    "trading_blocked": account.trading_blocked,
                },
            )
        log.critical("account_check_failed", reason=reason)
        raise RiskRejectionError(f"Account refused at startup: {reason}")

    log.info(
        "account_check_ok",
        multiplier=account.multiplier,
        status=account.status,
        cash=str(account.cash),
        equity=str(account.equity),
    )
    return account


async def validate_signal(
    signal: Signal,
    *,
    signal_id: int | None = None,
    account_fetcher: Callable[[], Awaitable[AccountSnapshot]] = alpaca_client.get_account,
    session_factory: Callable[[], Session] | None = None,
    now: datetime | None = None,
) -> None:
    """Run a proposed trade signal through every hard check.

    Checks, in this order (fail-fast):
      1. signal has an order (open/close only — hold and alert_only skip)
      2. account is cash (re-checked, not trusted from startup cache)
      3. account not blocked / not flagged
      4. defined-risk options structure (no naked options)
      5. max position size USD
      6. max concurrent positions
      7. max options contracts per trade
      8. daily realized-loss limit not breached
      9. cash available ≥ order notional

    Raises RiskRejectionError on any failure. Records a risk_event for both
    rejections and approvals.
    """
    # 1. Action gate — hold and alert_only do not propose an order.
    if signal.action in (SignalAction.HOLD, SignalAction.ALERT_ONLY):
        return
    if signal.order is None:
        raise RiskRejectionError(f"signal action={signal.action} but no order provided")

    order = signal.order
    settings = get_settings()
    factory = session_factory or make_session_factory()

    def _reject(reason: str, details: dict | None = None) -> None:
        with factory() as session:
            _record(
                session,
                event_type="rejection",
                severity="warning",
                reason=reason,
                signal_id=signal_id,
                details=details,
            )
        log.warning("signal_rejected", reason=reason, signal_id=signal_id)
        raise RiskRejectionError(reason)

    # 4. Defined-risk options check (cheap, runs before any Alpaca call).
    ok, why = _is_defined_risk(order)
    if not ok:
        _reject(f"defined-risk failed: {why}", {"order_strategy": order.strategy})

    # 5. Max position size.
    if order.notional_usd > settings.max_position_size_usd:
        _reject(
            f"notional ${order.notional_usd} exceeds MAX_POSITION_SIZE_USD "
            f"${settings.max_position_size_usd}",
            {"notional_usd": str(order.notional_usd)},
        )

    # 7. Max options contracts per trade.
    if order.asset_class is AssetClass.OPTION and order.legs:
        max_leg_qty = max(leg.qty for leg in order.legs)
        if max_leg_qty > settings.max_options_contracts_per_trade:
            _reject(
                f"options contracts {max_leg_qty} exceeds "
                f"MAX_OPTIONS_CONTRACTS_PER_TRADE {settings.max_options_contracts_per_trade}",
                {"max_leg_qty": max_leg_qty},
            )

    # DB-backed checks (open positions, daily P&L).
    with factory() as session:
        # 6. Max concurrent positions.
        open_count = count_open_positions(session)
        if open_count >= settings.max_concurrent_positions:
            _reject(
                f"already at MAX_CONCURRENT_POSITIONS={settings.max_concurrent_positions} "
                f"(open={open_count})",
                {"open_positions": open_count},
            )

        # 8. Daily realized loss.
        realized = realized_pnl_today_usd(session, now=now)
        if realized < 0 and (-realized) >= settings.daily_loss_limit_usd:
            _reject(
                f"daily loss ${(-realized):.2f} ≥ DAILY_LOSS_LIMIT_USD "
                f"${settings.daily_loss_limit_usd}",
                {"realized_pnl_usd": str(realized)},
            )

    # 2-3 + 9: Alpaca account state and cash availability.
    account = await account_fetcher()
    if account.multiplier != CASH_MULTIPLIER:
        _reject(
            f"runtime account.multiplier={account.multiplier!r} (D-001 cash-only)",
            {"multiplier": account.multiplier},
        )
    if account.account_blocked or account.trading_blocked:
        _reject(
            f"account blocked (account_blocked={account.account_blocked}, "
            f"trading_blocked={account.trading_blocked})",
            {
                "account_blocked": account.account_blocked,
                "trading_blocked": account.trading_blocked,
            },
        )
    if account.cash < order.notional_usd:
        _reject(
            f"cash ${account.cash} < notional ${order.notional_usd}",
            {"cash": str(account.cash), "notional_usd": str(order.notional_usd)},
        )

    # All checks passed — record approval for audit.
    with factory() as session:
        _record(
            session,
            event_type="approval",
            severity="info",
            reason="all hard checks passed",
            signal_id=signal_id,
            details={
                "symbol": order.symbol,
                "asset_class": order.asset_class.value,
                "notional_usd": str(order.notional_usd),
                "strategy": order.strategy,
            },
        )
    log.info(
        "signal_approved",
        signal_id=signal_id,
        symbol=order.symbol,
        notional_usd=str(order.notional_usd),
    )


async def kill_all_positions(
    *,
    cancel=alpaca_client.cancel_all_orders,
    close=alpaca_client.close_all_positions,
    session_factory: Callable[[], Session] | None = None,
    reason: str = "manual /kill command",
) -> dict:
    """Emergency flatten — cancel all orders, close all positions.

    Triggered by Discord `/kill` command or by daily loss limit breach.
    Returns a dict with counts. Records a critical-severity risk_event.
    """
    factory = session_factory or make_session_factory()
    cancelled = await cancel()
    closed = await close(True)

    with factory() as session:
        _record(
            session,
            event_type="kill_switch",
            severity="critical",
            reason=reason,
            details={"orders_cancelled": cancelled, "positions_closed": closed},
        )
    log.critical(
        "kill_switch_activated",
        reason=reason,
        orders_cancelled=cancelled,
        positions_closed=closed,
    )
    return {"orders_cancelled": cancelled, "positions_closed": closed}
