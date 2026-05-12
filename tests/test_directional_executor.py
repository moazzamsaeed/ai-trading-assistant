"""Tests for the directional options executor."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.directional.executor import (
    STRATEGY_CALL,
    STRATEGY_PUT,
    DirectionalExecutionResult,
    _format_trade_text,
    _resolve_expiry,
    execute_directional_signal,
)
from agents.directional.intraday import TickerDecision
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.db import Base, Trade, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _decision(action="BUY_CALL", strike=500.0, expiry="0DTE", conviction="HIGH"):
    return TickerDecision(
        ticker="SPY",
        action=action,
        strike=strike,
        expiry=expiry,
        conviction=conviction,
        reasoning="test setup",
    )


def _quote(ask: float = 2.00, bid: float = 1.90) -> OptionQuote:
    return OptionQuote(
        occ_symbol="SPY260101C00500000",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=date(2026, 1, 1),
        option_type="call",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        mid=Decimal(str((ask + bid) / 2)),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )


def _filled_order(price: float = 2.00) -> OrderResult:
    return OrderResult(
        order_id="ord-123",
        status="filled",
        filled_avg_price=Decimal(str(price)),
        filled_qty=Decimal("3"),
        submitted_at=datetime.now(UTC),
        raw_status="filled",
    )


def _rejected_order() -> OrderResult:
    return OrderResult(
        order_id="ord-456",
        status="rejected",
        filled_avg_price=None,
        filled_qty=Decimal("0"),
        submitted_at=datetime.now(UTC),
        raw_status="rejected",
    )


# ---------------------------------------------------------------------------
# _resolve_expiry
# ---------------------------------------------------------------------------


def test_resolve_expiry_0dte():
    today = date(2026, 5, 11)  # Monday
    assert _resolve_expiry("0DTE", today) == today


def test_resolve_expiry_weekly_from_monday():
    today = date(2026, 5, 11)  # Monday
    assert _resolve_expiry("WEEKLY", today) == date(2026, 5, 15)  # Friday


def test_resolve_expiry_weekly_from_friday_returns_next():
    today = date(2026, 5, 15)  # Friday
    assert _resolve_expiry("WEEKLY", today) == date(2026, 5, 22)  # following Friday


# ---------------------------------------------------------------------------
# _format_trade_text
# ---------------------------------------------------------------------------


def test_format_trade_text_contains_key_info():
    d = _decision()
    text = _format_trade_text(
        d,
        trade_id=7,
        qty=3,
        occ="SPY260101C00500000",
        entry_premium=Decimal("2.00"),
        profit_target_premium=Decimal("4.00"),
        stop_premium=Decimal("1.00"),
        mode="aggressive",
    )
    assert "trade #7" in text
    assert "AGGRESSIVE" in text
    assert "3×" in text
    assert "$2.00" in text
    assert "$4.00" in text
    assert "$1.00" in text


# ---------------------------------------------------------------------------
# execute_directional_signal
# ---------------------------------------------------------------------------


async def test_execute_hold_returns_no_execute():
    d = _decision(action="HOLD")
    result = await execute_directional_signal(d)
    assert not result.executed
    assert "HOLD" in result.reason


async def test_execute_missing_strike_returns_no_execute():
    d = TickerDecision("SPY", "BUY_CALL", None, "0DTE", "HIGH", "test")
    result = await execute_directional_signal(d)
    assert not result.executed


async def test_execute_max_concurrent_blocks(session_factory, monkeypatch):
    """When directional_max_concurrent positions are open, execution is skipped."""
    # Pre-populate 3 open directional trades
    with session_factory() as session:
        for _ in range(3):
            session.add(Trade(
                symbol="SPY260101C00500000",
                asset_class="option",
                side="buy",
                strategy=STRATEGY_CALL,
                qty=Decimal("1"),
                entry_price=Decimal("2.00"),
            ))
        session.commit()

    monkeypatch.setenv("DIRECTIONAL_MAX_CONCURRENT", "3")
    import trademaster.config as cfg
    cfg.get_settings.cache_clear()

    result = await execute_directional_signal(
        _decision(),
        session_factory=session_factory,
    )
    assert not result.executed
    assert "max_concurrent" in result.reason

    cfg.get_settings.cache_clear()


async def test_execute_too_expensive_skips(session_factory):
    """If 1 contract costs more than position_usd, skip rather than overspend."""
    async def expensive_quote(_occ):
        # $8/share × 100 = $800/contract > $750 aggressive position cap
        return _quote(ask=8.00)

    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        quote_fetcher=expensive_quote,
    )
    assert not result.executed
    assert "exceeds" in result.reason
    assert "$800" in result.reason


async def test_execute_no_quote_skips(session_factory):
    async def fake_quote(_occ):
        return None

    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        quote_fetcher=fake_quote,
    )
    assert not result.executed
    assert "no live quote" in result.reason


async def test_execute_order_rejected_no_trade_row(session_factory):
    async def fake_quote(_occ):
        return _quote(ask=2.00)

    async def fake_submit(**_kwargs):
        return _rejected_order()

    async def fake_wait(order_id, **_kw):
        return _rejected_order()

    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        quote_fetcher=fake_quote,
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert not result.executed
    assert "rejected" in result.reason

    with session_factory() as session:
        assert session.query(Trade).count() == 0


async def test_execute_success_persists_trade(session_factory):
    async def fake_quote(_occ):
        return _quote(ask=2.00)

    async def fake_submit(**_kwargs):
        return _filled_order(price=2.00)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=2.00)

    result = await execute_directional_signal(
        _decision(action="BUY_CALL", expiry="0DTE"),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        quote_fetcher=fake_quote,
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed
    assert result.trade_id is not None
    assert result.qty is not None
    assert result.occ is not None
    assert result.entry_premium is not None

    with session_factory() as session:
        trade = session.get(Trade, result.trade_id)
        assert trade is not None
        assert trade.strategy == STRATEGY_CALL
        assert trade.side == "buy"
        extra = trade.extra or {}
        assert "profit_target_premium" in extra
        assert "stop_premium" in extra
        assert "entry_reasoning" in extra
        assert extra["mode"] == "aggressive"


async def test_execute_put_persists_correct_strategy(session_factory):
    async def fake_quote(_occ):
        return _quote(ask=1.50)

    async def fake_submit(**_kwargs):
        return _filled_order(price=1.50)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=1.50)

    result = await execute_directional_signal(
        _decision(action="BUY_PUT", expiry="0DTE"),
        today=date(2026, 1, 2),
        mode="selective",
        session_factory=session_factory,
        quote_fetcher=fake_quote,
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed
    with session_factory() as session:
        trade = session.get(Trade, result.trade_id)
        assert trade.strategy == STRATEGY_PUT


async def test_execute_aggressive_sizing(session_factory):
    """Aggressive mode allocates 15% of $5000 = $750; at $2/share = 3 contracts."""
    submitted_kwargs = {}

    async def fake_quote(_occ):
        return _quote(ask=2.00)

    async def fake_submit(**kwargs):
        submitted_kwargs.update(kwargs)
        return _filled_order(price=2.00)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=2.00)

    await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        quote_fetcher=fake_quote,
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert submitted_kwargs["qty"] == 3  # floor(750 / 200) = 3
