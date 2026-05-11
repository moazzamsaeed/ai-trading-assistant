"""Iron-condor executor tests.

Mocks the Alpaca submit/wait helpers. Verifies paper-mode auto-execute,
live-mode short-circuit, terminal-status handling, and Trade-row persistence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.options import executor as ex
from agents.options.executor import execute_iron_condor
from integrations.alpaca_client import OptionQuote, OrderResult
from strategies.spy_0dte_iron_condor import IronCondorPlan
from trademaster.db import Base, Trade, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _quote(*, kind: str, strike: int) -> OptionQuote:
    pad = f"{int(strike * 1000):08d}"
    letter = "C" if kind == "call" else "P"
    return OptionQuote(
        occ_symbol=f"SPY260511{letter}{pad}",
        underlying="SPY",
        strike=Decimal(strike),
        expiry=date(2026, 5, 11),
        option_type=kind,
        bid=Decimal("0.60"),
        ask=Decimal("0.65"),
        mid=Decimal("0.625"),
        delta=Decimal("0.16"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.1"),
        implied_volatility=Decimal("0.2"),
    )


def _plan(qty: int = 1) -> IronCondorPlan:
    return IronCondorPlan(
        short_put=_quote(kind="put", strike=495),
        long_put=_quote(kind="put", strike=490),
        short_call=_quote(kind="call", strike=505),
        long_call=_quote(kind="call", strike=510),
        qty=qty,
        credit_per_contract=Decimal("80.00"),
        max_loss_per_contract=Decimal("420.00"),
        wing_width=Decimal("5"),
    )


def _order(status: str = "filled", filled_avg: str = "0.80") -> OrderResult:
    return OrderResult(
        order_id="ord_123",
        status=status,
        filled_avg_price=Decimal(filled_avg) if filled_avg else None,
        filled_qty=Decimal("1"),
        submitted_at=datetime.now(UTC),
        raw_status=status,
    )


async def test_live_mode_does_not_execute(monkeypatch, session_factory):
    class FakeSettings:
        trading_mode = "live"

    monkeypatch.setattr(ex, "get_settings", lambda: FakeSettings())

    submit_called = False

    async def boom_submit(**_):
        nonlocal submit_called
        submit_called = True
        return _order()

    result = await execute_iron_condor(
        _plan(),
        session_factory=session_factory,
        submitter=boom_submit,
        waiter=lambda *a, **k: _order(),
    )
    assert result.executed is False
    assert "live mode" in result.reason.lower()
    assert submit_called is False


async def test_paper_mode_fills_and_persists_trade(monkeypatch, session_factory):
    class FakeSettings:
        trading_mode = "paper"

    monkeypatch.setattr(ex, "get_settings", lambda: FakeSettings())

    async def submitter(**kwargs):
        # Verify all four OCC symbols passed through.
        assert "short_put" in kwargs and kwargs["short_put"].endswith("00495000")
        assert "long_call" in kwargs and kwargs["long_call"].endswith("00510000")
        return _order("new", filled_avg=None)

    async def waiter(order_id, *, timeout_s):
        assert order_id == "ord_123"
        return _order("filled", filled_avg="0.85")  # filled at $85 / contract

    result = await execute_iron_condor(
        _plan(qty=2),
        session_factory=session_factory,
        submitter=submitter,
        waiter=waiter,
    )
    assert result.executed is True
    assert result.trade_id is not None
    assert "85.00" in result.reason  # actual fill credit recorded

    with session_factory() as s:
        row = s.query(Trade).one()
        assert row.symbol == "SPY"
        assert row.strategy == "spy_0dte_ic"
        assert row.qty == Decimal("2")
        assert row.entry_price == Decimal("85.00")
        assert row.alpaca_order_id == "ord_123"
        assert row.extra["structure"] == "iron_condor"
        assert row.extra["wing_width"] == "5"


async def test_rejected_status_does_not_persist_trade(monkeypatch, session_factory):
    class FakeSettings:
        trading_mode = "paper"

    monkeypatch.setattr(ex, "get_settings", lambda: FakeSettings())

    async def submitter(**_):
        return _order("new")

    async def waiter(_id, *, timeout_s):
        return _order("rejected", filled_avg=None)

    result = await execute_iron_condor(
        _plan(),
        session_factory=session_factory,
        submitter=submitter,
        waiter=waiter,
    )
    assert result.executed is False
    assert "rejected" in result.reason.lower()
    with session_factory() as s:
        assert s.query(Trade).count() == 0


async def test_timeout_with_non_terminal_status_does_not_persist(
    monkeypatch, session_factory
):
    class FakeSettings:
        trading_mode = "paper"

    monkeypatch.setattr(ex, "get_settings", lambda: FakeSettings())

    async def submitter(**_):
        return _order("new")

    async def waiter(_id, *, timeout_s):
        return _order("new", filled_avg=None)  # never filled

    result = await execute_iron_condor(
        _plan(),
        session_factory=session_factory,
        submitter=submitter,
        waiter=waiter,
    )
    assert result.executed is False
    with session_factory() as s:
        assert s.query(Trade).count() == 0
