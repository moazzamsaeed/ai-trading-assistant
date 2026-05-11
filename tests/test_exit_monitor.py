"""Iron-condor exit-monitor tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from agents.options import exit_monitor as em
from agents.options.exit_monitor import (
    _decide_exit,
    run_exit_monitor,
)
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.db import Base, Trade, make_engine, make_session_factory


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# ----------------- _decide_exit -----------------


def test_decide_exit_profit_target():
    exit_, why = _decide_exit(
        credit_received=Decimal("80"), exit_debit=Decimal("40"), force=False
    )
    assert exit_ and why == "profit_target_50pct"


def test_decide_exit_stop_loss_2x():
    exit_, why = _decide_exit(
        credit_received=Decimal("80"), exit_debit=Decimal("240"), force=False
    )
    assert exit_ and why == "stop_loss_2x"


def test_decide_exit_hold_in_normal_range():
    exit_, _ = _decide_exit(
        credit_received=Decimal("80"), exit_debit=Decimal("100"), force=False
    )
    assert not exit_


def test_decide_exit_force_overrides():
    exit_, why = _decide_exit(
        credit_received=Decimal("80"), exit_debit=Decimal("100"), force=True
    )
    assert exit_ and "force_close" in why


# ----------------- fixtures -----------------


def _q(occ: str, *, bid: str, ask: str, kind: str, strike: int) -> OptionQuote:
    b, a = Decimal(bid), Decimal(ask)
    return OptionQuote(
        occ_symbol=occ,
        underlying="SPY",
        strike=Decimal(strike),
        expiry=date(2026, 5, 11),
        option_type=kind,
        bid=b, ask=a, mid=(b + a) / 2,
        delta=None, gamma=None, theta=None, vega=None,
        implied_volatility=None,
    )


def _ic_trade(session_factory, *, qty: int = 1, credit: str = "80.00") -> int:
    """Persist an open iron condor and return its row id."""
    legs = {
        "short_put": "SPY260511P00495000",
        "long_put":  "SPY260511P00490000",
        "short_call": "SPY260511C00505000",
        "long_call":  "SPY260511C00510000",
    }
    with session_factory() as s:
        row = Trade(
            symbol="SPY",
            asset_class="option",
            side="sell",
            strategy="spy_0dte_ic",
            qty=Decimal(qty),
            entry_price=Decimal(credit),
            alpaca_order_id="open_1",
            opened_at=datetime(2026, 5, 11, 13, 45, tzinfo=UTC),
            extra={
                **legs,
                "structure": "iron_condor",
                "wing_width": "5",
                "credit_per_contract": credit,
                "max_loss_per_contract": "420.00",
                "expiry": "2026-05-11",
            },
        )
        s.add(row)
        s.commit()
        return int(row.id)


def _chain_at_debit(target_debit_per_share: Decimal) -> list[OptionQuote]:
    """Build a fresh chain whose exit-debit math sums to `target_debit_per_share`.

    exit per share = (sp.ask + sc.ask) - (lp.bid + lc.bid)
    Distribute symmetrically: sp.ask = sc.ask = X, lp.bid = lc.bid = Y → 2X - 2Y = D
    """
    half = target_debit_per_share / Decimal("4")
    raw_y = Decimal("0.50") - half
    if raw_y < Decimal("0.01"):
        y = Decimal("0.01")
        x = y + target_debit_per_share / Decimal("2")
    else:
        y = raw_y
        x = Decimal("0.50") + half
    return [
        _q("SPY260511P00495000", bid=str(x - Decimal("0.05")), ask=str(x),
           kind="put", strike=495),
        _q("SPY260511P00490000", bid=str(y), ask=str(y + Decimal("0.05")),
           kind="put", strike=490),
        _q("SPY260511C00505000", bid=str(x - Decimal("0.05")), ask=str(x),
           kind="call", strike=505),
        _q("SPY260511C00510000", bid=str(y), ask=str(y + Decimal("0.05")),
           kind="call", strike=510),
    ]


def _fake_fill(price_per_share: str) -> OrderResult:
    return OrderResult(
        order_id="close_1",
        status="filled",
        filled_avg_price=Decimal(price_per_share),
        filled_qty=Decimal("1"),
        submitted_at=datetime.now(UTC),
        raw_status="filled",
    )


# ----------------- monitor scenarios -----------------


async def test_monitor_holds_when_neither_threshold_hit(session_factory):
    trade_id = _ic_trade(session_factory)

    # Debit per contract ~$100 (entry credit $80 → in moderate-loss zone, < 2x stop).
    chain = _chain_at_debit(Decimal("1.00"))

    async def chain_fetcher(*_a, **_k):
        return chain

    async def submitter(**_):
        raise AssertionError("must not submit close order when holding")

    async def waiter(*_a, **_k):
        return _fake_fill("0")

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=False,
    )
    assert len(results) == 1
    r = results[0]
    assert r["trade_id"] == trade_id
    assert r["status"] == "hold"
    assert Decimal(r["credit"]) == Decimal("80")
    assert Decimal(r["exit_debit"]) == Decimal("100.00")

    with session_factory() as s:
        row = s.get(Trade, trade_id)
        assert row.closed_at is None


async def test_monitor_closes_at_profit_target(session_factory):
    trade_id = _ic_trade(session_factory)

    # Aim for $40 debit per contract → 50% PT triggers.
    chain = _chain_at_debit(Decimal("0.40"))

    submitted: dict = {}

    async def chain_fetcher(*_a, **_k):
        return chain

    async def submitter(**kwargs):
        submitted.update(kwargs)
        return OrderResult(
            order_id="close_1",
            status="new",
            filled_avg_price=None,
            filled_qty=Decimal("0"),
            submitted_at=datetime.now(UTC),
            raw_status="new",
        )

    async def waiter(_id, *, timeout_s):
        return _fake_fill("0.42")  # close cost per share

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=False,
    )
    assert len(results) == 1
    r = results[0]
    assert r["status"] == "closed"
    assert r["reason"] == "profit_target_50pct"
    assert submitted["short_put"] == "SPY260511P00495000"

    with session_factory() as s:
        row = s.get(Trade, trade_id)
        assert row.closed_at is not None
        assert row.exit_price == Decimal("42.00")  # filled debit
        # P&L = entry credit (80) - exit debit (42) = 38, qty=1
        assert row.realized_pnl_usd == Decimal("38.00")
        assert row.extra["exit_reason"] == "profit_target_50pct"


async def test_monitor_closes_at_stop_loss(session_factory):
    trade_id = _ic_trade(session_factory)

    # 2x stop fires when debit >= 3 * credit = $240. Push debit to $260.
    chain = _chain_at_debit(Decimal("2.60"))

    async def chain_fetcher(*_a, **_k):
        return chain

    async def submitter(**_):
        return OrderResult(
            order_id="close_1",
            status="new",
            filled_avg_price=None,
            filled_qty=Decimal("0"),
            submitted_at=datetime.now(UTC),
            raw_status="new",
        )

    async def waiter(_id, *, timeout_s):
        return _fake_fill("2.65")  # actual fill slightly worse

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=False,
    )
    assert results[0]["reason"] == "stop_loss_2x"
    with session_factory() as s:
        row = s.get(Trade, trade_id)
        assert row.realized_pnl_usd == Decimal("80.00") - Decimal("265.00")  # -185


async def test_monitor_force_closes_regardless(session_factory):
    _ic_trade(session_factory)

    # Modest debit that wouldn't normally trigger.
    chain = _chain_at_debit(Decimal("1.00"))

    async def chain_fetcher(*_a, **_k):
        return chain

    async def submitter(**_):
        return OrderResult(
            order_id="close_1",
            status="new",
            filled_avg_price=None,
            filled_qty=Decimal("0"),
            submitted_at=datetime.now(UTC),
            raw_status="new",
        )

    async def waiter(_id, *, timeout_s):
        return _fake_fill("1.00")

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=True,
    )
    assert "force_close" in results[0]["reason"]


async def test_monitor_skips_trade_with_missing_legs(session_factory):
    with session_factory() as s:
        row = Trade(
            symbol="SPY",
            asset_class="option",
            side="sell",
            strategy="spy_0dte_ic",
            qty=Decimal(1),
            entry_price=Decimal("80"),
            opened_at=datetime.now(UTC),
            extra={"structure": "iron_condor"},  # legs missing
        )
        s.add(row)
        s.commit()
        trade_id = int(row.id)

    async def chain_fetcher(*_a, **_k):
        raise AssertionError("should not fetch chain when legs missing")

    async def submitter(**_):
        raise AssertionError("should not submit when legs missing")

    async def waiter(*_a, **_k):
        raise AssertionError("should not wait when legs missing")

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=False,
    )
    assert results == [{"trade_id": trade_id, "status": "missing_legs"}]


async def test_monitor_skips_trade_when_quote_missing(session_factory):
    _ic_trade(session_factory)

    # Return a chain without any of the expected OCC symbols.
    async def chain_fetcher(*_a, **_k):
        return [_q("SPY260511P00400000", bid="1", ask="1.1", kind="put", strike=400)]

    async def submitter(**_):
        raise AssertionError("should not submit when quote missing")

    async def waiter(*_a, **_k):
        raise AssertionError("should not wait when quote missing")

    results = await run_exit_monitor(
        session_factory=session_factory,
        chain_fetcher=chain_fetcher,
        submitter=submitter,
        waiter=waiter,
        force_close=False,
    )
    assert results[0]["status"] == "no_quotes"


async def test_force_close_auto_after_1550_et():
    """Internal: now after 15:50 ET sets force=True without explicit arg."""
    # 15:55 ET → 19:55 UTC (EDT) / 20:55 (EST). We assert the time-only check.
    assert time(15, 55) >= em.FORCE_CLOSE_AFTER
    assert time(15, 49) < em.FORCE_CLOSE_AFTER
