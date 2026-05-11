"""Tests for the directional exit monitor."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.directional.exit_monitor import (
    _decide_exit,
    _format_exit_signal,
    _format_exit_telemetry,
    run_directional_exit_monitor,
)
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.db import Base, Trade, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _open_trade(
    session_factory,
    *,
    entry_premium: float = 2.00,
    pt: float = 4.00,
    stop: float = 1.00,
    action: str = "BUY_CALL",
    strategy: str = "directional_call",
) -> Trade:
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000",
            asset_class="option",
            side="buy",
            strategy=strategy,
            qty=Decimal("3"),
            entry_price=Decimal(str(entry_premium)),
            alpaca_order_id="ord-entry-1",
            opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY",
                "action": action,
                "occ_symbol": "SPY260101C00500000",
                "mode": "aggressive",
                "profit_target_premium": str(pt),
                "stop_premium": str(stop),
            },
        )
        session.add(trade)
        session.commit()
        return trade


def _quote(bid: float) -> OptionQuote:
    return OptionQuote(
        occ_symbol="SPY260101C00500000",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=date(2026, 1, 1),
        option_type="call",
        bid=Decimal(str(bid)),
        ask=Decimal(str(bid + 0.10)),
        mid=Decimal(str(bid + 0.05)),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )


def _filled(price: float = 4.00) -> OrderResult:
    return OrderResult(
        order_id="ord-close-1",
        status="filled",
        filled_avg_price=Decimal(str(price)),
        filled_qty=Decimal("3"),
        submitted_at=datetime.now(UTC),
        raw_status="filled",
    )


def _rejected() -> OrderResult:
    return OrderResult(
        order_id="ord-close-2",
        status="rejected",
        filled_avg_price=None,
        filled_qty=Decimal("0"),
        submitted_at=datetime.now(UTC),
        raw_status="rejected",
    )


# ---------------------------------------------------------------------------
# _decide_exit
# ---------------------------------------------------------------------------


def test_decide_exit_profit_target():
    hit, reason = _decide_exit(
        current_bid=Decimal("4.10"),
        profit_target_premium=Decimal("4.00"),
        stop_premium=Decimal("1.00"),
        force=False,
    )
    assert hit and reason == "profit_target"


def test_decide_exit_stop_loss():
    hit, reason = _decide_exit(
        current_bid=Decimal("0.90"),
        profit_target_premium=Decimal("4.00"),
        stop_premium=Decimal("1.00"),
        force=False,
    )
    assert hit and reason == "stop_loss"


def test_decide_exit_hold():
    hit, reason = _decide_exit(
        current_bid=Decimal("2.50"),
        profit_target_premium=Decimal("4.00"),
        stop_premium=Decimal("1.00"),
        force=False,
    )
    assert not hit and reason == ""


def test_decide_exit_force_overrides_hold():
    hit, reason = _decide_exit(
        current_bid=Decimal("2.50"),
        profit_target_premium=Decimal("4.00"),
        stop_premium=Decimal("1.00"),
        force=True,
    )
    assert hit and reason == "force_close"


# ---------------------------------------------------------------------------
# format helpers
# ---------------------------------------------------------------------------


def _fake_trade() -> Trade:
    t = Trade(
        symbol="SPY260101C00500000",
        asset_class="option",
        side="buy",
        strategy="directional_call",
        qty=Decimal("3"),
        entry_price=Decimal("2.00"),
        extra={
            "ticker": "SPY",
            "action": "BUY_CALL",
            "occ_symbol": "SPY260101C00500000",
            "mode": "aggressive",
        },
    )
    t.id = 42
    return t


def test_format_exit_signal_profit_target():
    t = _fake_trade()
    msg = _format_exit_signal(t, Decimal("4.10"), "profit_target")
    assert "EXIT" in msg
    assert "✅" in msg
    assert "Sell to close" in msg
    assert "SPY" in msg
    assert "profit" in msg.lower()


def test_format_exit_signal_stop_loss():
    t = _fake_trade()
    msg = _format_exit_signal(t, Decimal("0.90"), "stop_loss")
    assert "🛑" in msg
    assert "loss" in msg.lower()


def test_format_exit_telemetry():
    t = _fake_trade()
    msg = _format_exit_telemetry(t, exit_premium=Decimal("4.00"), reason="profit_target")
    assert "trade #42" in msg
    assert "AGGRESSIVE" in msg
    assert "$2.00" in msg
    assert "$4.00" in msg


# ---------------------------------------------------------------------------
# run_directional_exit_monitor
# ---------------------------------------------------------------------------


async def test_monitor_empty_returns_empty(session_factory):
    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        force_close=False,
    )
    assert results == []


async def test_monitor_no_quote_returns_no_quote_status(session_factory):
    _open_trade(session_factory)

    async def no_quote(_occ):
        return None

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=no_quote,
        force_close=False,
    )
    assert len(results) == 1
    assert results[0]["status"] == "no_quote"


async def test_monitor_hold_when_between_pt_and_stop(session_factory):
    _open_trade(session_factory, entry_premium=2.00, pt=4.00, stop=1.00)

    async def mid_quote(_occ):
        return _quote(bid=2.50)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        force_close=False,
    )
    assert results[0]["status"] == "hold"


async def test_monitor_profit_target_closes_trade(session_factory):
    trade = _open_trade(session_factory, entry_premium=2.00, pt=4.00, stop=1.00)

    async def high_quote(_occ):
        return _quote(bid=4.10)

    async def fake_sell(**_kwargs):
        return _filled(price=4.10)

    async def fake_wait(order_id, **_kw):
        return _filled(price=4.10)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=high_quote,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "profit_target"
    assert "signal_text" in results[0]
    assert "trade_text" in results[0]

    with session_factory() as session:
        row = session.get(Trade, trade.id)
        assert row.closed_at is not None
        assert row.exit_price == Decimal("4.10")
        assert row.realized_pnl_usd > 0  # profit


async def test_monitor_stop_loss_closes_trade(session_factory):
    trade = _open_trade(session_factory, entry_premium=2.00, pt=4.00, stop=1.00)

    async def low_quote(_occ):
        return _quote(bid=0.80)

    async def fake_sell(**_kwargs):
        return _filled(price=0.80)

    async def fake_wait(order_id, **_kw):
        return _filled(price=0.80)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=low_quote,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert results[0]["reason"] == "stop_loss"
    with session_factory() as session:
        row = session.get(Trade, trade.id)
        assert row.realized_pnl_usd < 0  # loss


async def test_monitor_force_close_ignores_pt_stop(session_factory):
    _open_trade(session_factory, entry_premium=2.00, pt=4.00, stop=1.00)

    async def mid_quote(_occ):
        return _quote(bid=2.50)  # between PT and stop — normally HOLD

    async def fake_sell(**_kwargs):
        return _filled(price=2.50)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.50)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=True,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "force_close"


async def test_monitor_failed_order_reports_status(session_factory):
    _open_trade(session_factory, entry_premium=2.00, pt=4.00, stop=1.00)

    async def high_quote(_occ):
        return _quote(bid=4.10)

    async def fake_sell(**_kwargs):
        return _rejected()

    async def fake_wait(order_id, **_kw):
        return _rejected()

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=high_quote,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert "close_order_rejected" in results[0]["status"]
    assert "trade_text" in results[0]
