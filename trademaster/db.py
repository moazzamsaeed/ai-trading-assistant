"""SQLite persistence via SQLAlchemy 2.0.

Five tables back the system:
  - trades          executed positions with entry/exit/P&L
  - signals         every agent signal (for audit + retro-analysis)
  - agent_runs      every LLM call (model, tokens, cost, duration)
  - risk_events     every rejection/halt with reason
  - pending_orders  live-mode plans awaiting /approve or /reject (D-014)

Single-writer SQLite — one TradeMaster process. No pooling concerns.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from trademaster.config import get_settings
from trademaster.timeutils import ET, today_et


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64))

    qty: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    entry_price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    realized_pnl_usd: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))

    alpaca_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task_type: Mapped[str] = mapped_column(String(32))
    agent: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)

    confidence: Mapped[float | None]
    reasoning: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)

    accepted: Mapped[bool | None]
    rejection_reason: Mapped[str | None] = mapped_column(Text)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))

    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    duration_ms: Mapped[int | None]

    error: Mapped[str | None] = mapped_column(Text)


class NearMiss(Base):
    """A ticker that was HELD but nearly qualified — logged for post-hoc analysis.

    Recorded when a HOLD ticker meets ≥3 of 4 indicator criteria using a
    relaxed 1.0× volume threshold. Lets us compare 'what would have been'
    against actual price movement to calibrate the volume filter over time.
    """

    __tablename__ = "near_misses"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    would_be_action: Mapped[str] = mapped_column(String(16))   # BUY_CALL | BUY_PUT
    criteria_met: Mapped[int]                                   # 3 or 4
    volume_ratio: Mapped[float | None]
    rsi: Mapped[float | None]
    above_vwap: Mapped[bool]
    ema_confirmed: Mapped[bool]                                 # ema20 > ema50
    spy_regime: Mapped[str | None] = mapped_column(String(16))
    llm_reasoning: Mapped[str | None] = mapped_column(Text)    # why LLM said HOLD


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)

    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class PendingOrder(Base):
    """A risk-approved plan awaiting Discord `/approve` (live mode only).

    Status flow:
      pending  → approved (user ran /approve, order submitted)
               → rejected (user ran /reject)
               → expired  (no decision within 15 min — market data stale)
    """

    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    strategy: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(100))

    alpaca_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"))
    error: Mapped[str | None] = mapped_column(Text)


def make_engine(url: str | None = None):
    url = url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


def make_session_factory(engine=None):
    engine = engine or make_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine=None) -> None:
    """Create all tables if they don't exist. Idempotent."""
    engine = engine or make_engine()
    Base.metadata.create_all(engine)


def _sum_scale_out_partials(session, *, start=None, end=None) -> Decimal:
    """Sum extra.partial_realized_pnl_usd (scale-out gains) over trades whose
    opened_at falls in [start, end).

    Scale-out partials are realized intraday when a tier fires, but they are
    NOT in realized_pnl_usd (which `_close_trade_row` sets to the FINAL leg
    only). So the governors + capital calc would otherwise ignore every locked
    scale-out gain — understating winners and tripping the loss limit early.
    Attributed by opened_at: exact for 0DTE (open/scale/close same day), and a
    fine approximation for the rare multi-day weekly. Disjoint from
    realized_pnl_usd (scaled qty vs. final qty), so no double-count.
    """
    stmt = select(Trade.extra)
    if start is not None:
        stmt = stmt.where(Trade.opened_at >= start)
    if end is not None:
        stmt = stmt.where(Trade.opened_at < end)
    total = Decimal("0")
    for extra in session.execute(stmt).scalars():
        if not extra:
            continue
        p = extra.get("partial_realized_pnl_usd")
        if p:
            with contextlib.suppress(ArithmeticError, ValueError, TypeError):
                total += Decimal(str(p))
    return total


def get_cumulative_realized_pnl(session_factory) -> Decimal:
    """Sum of realized_pnl_usd (final legs) + scale-out partials across trades.

    Used by the paper-mode capital model: effective capital tracks the
    starting base plus all realized gains/losses since the (optional)
    baseline reset point, so today's sizing reflects recent outcomes
    without being haunted by ancient bad runs.

    If `settings.baseline_reset_at` is set, only trades closed at or
    after that UTC timestamp are counted. Trades before the reset stay
    in the DB for audit but are excluded from sizing.
    """
    reset_at = get_settings().baseline_reset_at

    with session_factory() as session:
        stmt = (
            select(func.coalesce(func.sum(func.cast(Trade.realized_pnl_usd, Numeric)), 0))
            .where(Trade.realized_pnl_usd.isnot(None))
        )
        if reset_at is not None:
            stmt = stmt.where(Trade.closed_at >= reset_at)
        result = session.execute(stmt).scalar()
        partials = _sum_scale_out_partials(session, start=reset_at)
    return Decimal(str(result or 0)) + partials


def get_today_realized_pnl(session_factory) -> Decimal:
    """Realized P&L today (ET day): final-leg closes + scale-out partials.

    Uses ET-aware day boundaries so trades near midnight ET are counted
    correctly — SQLite's DATE('now') is UTC and would miss them after ~8pm ET.

    Binds Python `datetime` objects (not isoformat strings) so SQLAlchemy
    handles the storage format conversion; raw string comparison against
    SQLite TEXT timestamps is fragile (space vs. 'T', tz suffix, etc.).
    """
    today = today_et()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    # If a baseline reset is configured, also exclude pre-reset trades so
    # the loss-limit gate doesn't trip on history we deliberately wiped.
    reset_at = get_settings().baseline_reset_at
    effective_start = max(day_start, reset_at) if reset_at is not None else day_start

    with session_factory() as session:
        result = session.execute(
            select(func.coalesce(func.sum(func.cast(Trade.realized_pnl_usd, Numeric)), 0))
            .where(Trade.closed_at >= effective_start)
            .where(Trade.closed_at < day_end)
        ).scalar()
        partials = _sum_scale_out_partials(session, start=effective_start, end=day_end)
    return Decimal(str(result or 0)) + partials


def get_this_week_realized_pnl(session_factory) -> Decimal:
    """Realized P&L this week (Mon–Sun ET): final-leg closes + scale-out partials.

    Resets Monday 00:00 ET. Used for the weekly loss limit gate.
    """
    today = today_et()
    week_start_date = today - timedelta(days=today.weekday())  # Monday
    week_start = datetime.combine(week_start_date, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    week_end = week_start + timedelta(days=7)
    reset_at = get_settings().baseline_reset_at
    effective_start = max(week_start, reset_at) if reset_at is not None else week_start

    with session_factory() as session:
        result = session.execute(
            select(func.coalesce(func.sum(func.cast(Trade.realized_pnl_usd, Numeric)), 0))
            .where(Trade.closed_at >= effective_start)
            .where(Trade.closed_at < week_end)
        ).scalar()
        partials = _sum_scale_out_partials(session, start=effective_start, end=week_end)
    return Decimal(str(result or 0)) + partials


def get_today_trade_count(session_factory) -> int:
    """Count of directional trades opened today (ET calendar day)."""
    today = today_et()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    reset_at = get_settings().baseline_reset_at
    effective_start = max(day_start, reset_at) if reset_at is not None else day_start

    with session_factory() as session:
        result = session.execute(
            select(func.count(Trade.id))
            .where(Trade.strategy.in_(["directional_call", "directional_put"]))
            .where(Trade.opened_at >= effective_start)
            .where(Trade.opened_at < day_end)
        ).scalar()
    return int(result or 0)


def get_today_trade_count_by_conviction(session_factory) -> dict[str, int]:
    """Count of today's directional trades split by conviction level.

    Returns {"HIGH": n, "MEDIUM": n, "LOW": n}. Used for the tiered cap:
    MEDIUM trades are capped at 2/day; HIGH trades are capped at 4/day total.
    """
    today = today_et()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    reset_at = get_settings().baseline_reset_at
    effective_start = max(day_start, reset_at) if reset_at is not None else day_start

    counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    with session_factory() as session:
        rows = session.execute(
            select(Trade)
            .where(Trade.strategy.in_(["directional_call", "directional_put"]))
            .where(Trade.opened_at >= effective_start)
            .where(Trade.opened_at < day_end)
        ).scalars().all()
        for row in rows:
            conviction = (row.extra or {}).get("conviction", "HIGH")
            counts[conviction] = counts.get(conviction, 0) + 1
    return counts


_DIRECTIONAL_STRATEGIES = ["directional_call", "directional_put"]


def get_directional_trade_context(session_factory) -> dict:
    """Open directional positions + today's closed directional outcomes.

    Feeds the intraday scan prompt so the LLM reasons WITH its own state
    (am I already long? did my earlier same-direction trades work?) instead
    of being stateless and overridden after the fact. Returns plain dicts so
    the session closes cleanly. ET-day boundary, consistent with the other
    helpers here. Honors baseline_reset_at for the today-closed slice.
    """
    today = today_et()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    day_end = day_start + timedelta(days=1)
    reset_at = get_settings().baseline_reset_at
    effective_start = max(day_start, reset_at) if reset_at is not None else day_start

    open_positions: list[dict] = []
    today_closed: list[dict] = []
    with session_factory() as session:
        open_rows = session.execute(
            select(Trade)
            .where(Trade.strategy.in_(_DIRECTIONAL_STRATEGIES))
            .where(Trade.closed_at.is_(None))
            .order_by(Trade.opened_at)
        ).scalars().all()
        for r in open_rows:
            e = r.extra or {}
            open_positions.append({
                "ticker": e.get("ticker") or r.symbol,
                "action": e.get("action"),
                "conviction": e.get("conviction"),
                "qty": int(r.qty) if r.qty is not None else None,
                "entry_price": float(r.entry_price) if r.entry_price is not None else None,
                "peak_pnl_pct": e.get("peak_pnl_pct"),
                "opened_at": r.opened_at,
            })

        closed_rows = session.execute(
            select(Trade)
            .where(Trade.strategy.in_(_DIRECTIONAL_STRATEGIES))
            .where(Trade.closed_at >= effective_start)
            .where(Trade.closed_at < day_end)
            .order_by(Trade.closed_at)
        ).scalars().all()
        for r in closed_rows:
            e = r.extra or {}
            today_closed.append({
                "ticker": e.get("ticker") or r.symbol,
                "action": e.get("action"),
                "realized_pnl": (
                    float(r.realized_pnl_usd) if r.realized_pnl_usd is not None else None
                ),
                "exit_reason": e.get("exit_reason"),
            })

    return {"open": open_positions, "today_closed": today_closed}


def _trade_detail(r: Trade) -> dict:
    """Full closed-trade detail for #trades reports. total_pnl folds in the
    scale-out partials (realized_pnl_usd is only the final leg)."""
    e = r.extra or {}
    final = float(r.realized_pnl_usd) if r.realized_pnl_usd is not None else 0.0
    try:
        partial = float(e.get("partial_realized_pnl_usd", 0) or 0)
    except (TypeError, ValueError):
        partial = 0.0
    return {
        "id": r.id,
        "ticker": e.get("ticker") or r.symbol,
        "action": e.get("action"),
        "conviction": e.get("conviction"),
        "occ": e.get("occ_symbol") or r.symbol,
        "original_qty": e.get("original_qty"),
        "final_qty": int(r.qty) if r.qty is not None else None,
        "entry_price": float(r.entry_price) if r.entry_price is not None else None,
        "exit_price": float(r.exit_price) if r.exit_price is not None else None,
        "final_pnl": final,
        "partial_pnl": partial,
        "total_pnl": final + partial,
        "peak_pnl_pct": e.get("peak_pnl_pct"),
        "scale_out_tiers_fired": e.get("scale_out_tiers_fired"),
        "exit_reason": e.get("exit_reason"),
        "mode": e.get("mode"),
        "opened_at": r.opened_at,
        "closed_at": r.closed_at,
    }


def get_trade_detail(session_factory, trade_id: int) -> dict | None:
    """Full detail for one closed trade — for the per-trade #trades report."""
    with session_factory() as session:
        r = session.get(Trade, trade_id)
        return _trade_detail(r) if r is not None else None


def get_closed_directional_trades(session_factory, *, start, end) -> list[dict]:
    """Full detail for directional trades CLOSED in [start, end) (UTC), oldest
    first — feeds the daily/weekly #trades summaries."""
    with session_factory() as session:
        rows = session.execute(
            select(Trade)
            .where(Trade.strategy.in_(_DIRECTIONAL_STRATEGIES))
            .where(Trade.closed_at >= start)
            .where(Trade.closed_at < end)
            .order_by(Trade.closed_at)
        ).scalars().all()
        return [_trade_detail(r) for r in rows]


def day_bounds_utc(today=None) -> tuple[datetime, datetime]:
    """ET calendar-day [start, end) as UTC datetimes."""
    today = today or today_et()
    start = datetime.combine(today, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    return start, start + timedelta(days=1)


def week_bounds_utc(today=None) -> tuple[datetime, datetime]:
    """ET week (Mon 00:00 → next Mon) as UTC datetimes."""
    today = today or today_et()
    monday = today - timedelta(days=today.weekday())
    start = datetime.combine(monday, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    return start, start + timedelta(days=7)
