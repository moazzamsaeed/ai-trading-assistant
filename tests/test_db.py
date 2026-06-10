"""DB schema smoke tests against an in-memory SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest

from trademaster.db import (
    ET,
    AgentRun,
    Base,
    RiskEvent,
    Signal,
    Trade,
    get_today_directional_streak,
    make_engine,
    make_session_factory,
    today_et,
)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


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


# ---------------------------------------------------------------------------
# get_today_directional_streak (fix B re-entry throttle)
# ---------------------------------------------------------------------------


def _open_directional(sf, action: str, *, seq: int) -> None:
    """Insert a directional trade today (ET) at a deterministic intraday time."""
    strat = "directional_call" if action == "BUY_CALL" else "directional_put"
    noon_et = datetime.combine(today_et(), time(12, 0), tzinfo=ET)
    opened = (noon_et + timedelta(minutes=seq)).astimezone(UTC)
    with sf() as s:
        s.add(Trade(
            symbol="SPY260101P00500000", asset_class="option", side="buy",
            strategy=strat, qty=Decimal("1"), entry_price=Decimal("1.00"),
            opened_at=opened, extra={"action": action},
        ))
        s.commit()


def test_directional_streak_counts_trailing_run(session_factory):
    assert get_today_directional_streak(session_factory) == (None, 0)
    for i in range(3):
        _open_directional(session_factory, "BUY_PUT", seq=i)
    # 3 consecutive puts → throttle should engage at limit 3.
    assert get_today_directional_streak(session_factory) == ("BUY_PUT", 3)


def test_directional_streak_resets_on_flip(session_factory):
    _open_directional(session_factory, "BUY_PUT", seq=0)
    _open_directional(session_factory, "BUY_PUT", seq=1)
    _open_directional(session_factory, "BUY_CALL", seq=2)  # direction flip
    # The trailing run is now a single call — streak resets.
    assert get_today_directional_streak(session_factory) == ("BUY_CALL", 1)
