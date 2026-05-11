"""DB schema smoke tests against an in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from traderouter.db import (
    AgentRun,
    Base,
    RiskEvent,
    Signal,
    Trade,
    make_engine,
    make_session_factory,
)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


def test_insert_and_read_trade(session):
    t = Trade(
        symbol="SPY",
        asset_class="option",
        side="buy",
        strategy="spy_0dte_ic",
        qty=Decimal("1"),
        entry_price=Decimal("1.25"),
    )
    session.add(t)
    session.commit()
    row = session.query(Trade).one()
    assert row.symbol == "SPY"
    assert row.qty == Decimal("1")
    assert row.opened_at is not None


def test_insert_signal_with_payload(session):
    sig = Signal(
        task_type="intraday_scan",
        agent="options",
        action="open",
        symbol="SPY",
        confidence=0.72,
        reasoning="IV rank 65, range-bound morning.",
        payload={"iv_rank": 65, "delta": 0.16},
    )
    session.add(sig)
    session.commit()
    row = session.query(Signal).one()
    assert row.payload["iv_rank"] == 65


def test_insert_agent_run_with_cost(session):
    run = AgentRun(
        task_type="pre_market_research",
        provider="google",
        model="gemini-3.1-pro-preview",
        input_tokens=1200,
        output_tokens=400,
        cost_usd=Decimal("0.012345"),
        duration_ms=1840,
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    row = session.query(AgentRun).one()
    assert row.cost_usd == Decimal("0.012345")


def test_insert_risk_event(session):
    ev = RiskEvent(
        event_type="rejection",
        severity="warning",
        reason="cash insufficient for order notional",
        details={"required": 2000, "available": 1500},
    )
    session.add(ev)
    session.commit()
    row = session.query(RiskEvent).one()
    assert row.event_type == "rejection"
    assert row.details["required"] == 2000
