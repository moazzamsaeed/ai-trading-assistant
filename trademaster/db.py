"""SQLite persistence via SQLAlchemy 2.0.

Four tables back the system:
  - trades        executed positions with entry/exit/P&L
  - signals       every agent signal (for audit + retro-analysis)
  - agent_runs    every LLM call (model, tokens, cost, duration)
  - risk_events   every rejection/halt with reason

Single-writer SQLite — one Hermes process. No pooling concerns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from trademaster.config import get_settings


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


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    event_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)

    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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
