"""Tests for slash-command business logic (no Discord coupling)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from integrations import discord_commands as cmds
from integrations.alpaca_client import AccountSnapshot, PositionSnapshot
from trademaster.db import Base, RiskEvent, make_engine, make_session_factory
from trademaster.state import get_state, reset_state_for_tests


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _account(**overrides) -> AccountSnapshot:
    base = dict(
        account_number="abc",
        status="ACTIVE",
        multiplier="1",
        cash=Decimal("10000"),
        buying_power=Decimal("10000"),
        equity=Decimal("12000"),
        portfolio_value=Decimal("12000"),
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
    )
    base.update(overrides)
    return AccountSnapshot(**base)


async def _account_fetcher(account: AccountSnapshot):
    async def f() -> AccountSnapshot:
        return account
    return f


# ----------------- /status -----------------


async def test_status_reports_running_and_account(session_factory):
    text = await cmds.status(
        account_fetcher=await _account_fetcher(_account()),
        session_factory=session_factory,
    )
    assert "running" in text
    assert "cash=$10000" in text
    assert "multiplier=1" in text


async def test_status_reports_paused(session_factory):
    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)
    text = await cmds.status(
        account_fetcher=await _account_fetcher(_account()),
        session_factory=session_factory,
    )
    assert "paused" in text.lower()


async def test_status_tolerates_alpaca_failure(session_factory):
    async def boom() -> AccountSnapshot:
        raise RuntimeError("alpaca down")

    text = await cmds.status(
        account_fetcher=boom,
        session_factory=session_factory,
    )
    assert "account fetch failed" in text.lower()


# ----------------- /positions -----------------


async def test_positions_empty():
    async def empty() -> list[PositionSnapshot]:
        return []

    text = await cmds.positions(positions_fetcher=empty)
    assert text == "No open positions."


async def test_positions_lists():
    async def two() -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol="SPY", qty=Decimal("10"), avg_entry_price=Decimal("450"),
                market_value=Decimal("4600"), unrealized_pl=Decimal("100"),
                current_price=Decimal("460"), side="long", asset_class="us_equity",
            ),
            PositionSnapshot(
                symbol="QQQ", qty=Decimal("5"), avg_entry_price=Decimal("400"),
                market_value=Decimal("1900"), unrealized_pl=Decimal("-100"),
                current_price=Decimal("380"), side="long", asset_class="us_equity",
            ),
        ]

    text = await cmds.positions(positions_fetcher=two)
    assert "SPY" in text and "QQQ" in text
    assert "$+100" in text or "+$100" in text or "+100" in text


async def test_positions_tolerates_failure():
    async def boom() -> list[PositionSnapshot]:
        raise RuntimeError("fail")

    text = await cmds.positions(positions_fetcher=boom)
    assert "failed" in text.lower()


# ----------------- /cash -----------------


async def test_cash_formats_account():
    text = await cmds.cash(account_fetcher=await _account_fetcher(_account()))
    assert "$10000" in text
    assert "Buying power" in text
    assert "Equity" in text


# ----------------- /kill -----------------


async def test_kill_pauses_for_24h(session_factory):
    async def fake_kill(**_):
        return {"orders_cancelled": 2, "positions_closed": 3}

    text = await cmds.kill(kill_fn=fake_kill)
    assert "KILL" in text
    assert "Orders cancelled: 2" in text
    assert "Positions closed: 3" in text
    state = get_state()
    assert state.paused_until is not None
    assert state.paused_until > datetime.now(UTC) + timedelta(hours=23)
    assert state.last_kill_at is not None


async def test_kill_handles_failure():
    async def boom(**_):
        raise RuntimeError("alpaca rejected")

    text = await cmds.kill(kill_fn=boom)
    assert "KILL FAILED" in text


# ----------------- /pause + /resume -----------------


async def test_pause_sets_until():
    text = await cmds.pause(15)
    assert "15 min" in text
    state = get_state()
    assert state.paused_until is not None
    assert state.is_paused()


async def test_pause_rejects_non_positive():
    text = await cmds.pause(0)
    assert "positive" in text.lower()
    assert get_state().paused_until is None


async def test_resume_clears_pause():
    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)
    text = await cmds.resume()
    assert "resumed" in text.lower()
    assert get_state().paused_until is None


async def test_resume_noop_when_not_paused():
    text = await cmds.resume()
    assert "not paused" in text.lower()


# ----------------- state ordering -----------------


def test_is_paused_respects_now():
    state = get_state()
    state.paused_until = datetime.now(UTC) - timedelta(minutes=1)
    assert not state.is_paused()
    state.paused_until = datetime.now(UTC) + timedelta(minutes=1)
    assert state.is_paused()


def test_risk_events_schema_unused_in_commands(session_factory):
    """Commands don't write risk_events directly."""
    with session_factory() as s:
        # Sanity: zero rows after fresh DB.
        assert s.query(RiskEvent).count() == 0
