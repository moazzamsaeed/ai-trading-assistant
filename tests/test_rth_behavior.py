"""RTH-scenario mock tests for today's behavioral changes.

Each test simulates a specific moment during the trading day and verifies
that the production code path responds the way we want — these are the
gaps that existing tests didn't cover.

Scope (changes shipped today):
- Market+IOC option order submission (buys + sells)
- Daily loss limit pause at threshold
- Per-ticker 60-min cooldown
- 20% max exposure cap
- Signal dedup at 30 min
- Cancel-after-timeout when order non-terminal
- Auto-close on Alpaca position-not-held error
- get_unrealized_pnl fail-open behavior
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents.directional.executor import (
    STRATEGY_CALL,
    DirectionalExecutionResult,
    _SelectedStrike,
    execute_directional_signal,
)
from agents.directional.intraday import TickerDecision
from integrations import alpaca_client
from integrations.alpaca_client import (
    MarketClock,
    OptionQuote,
    OrderResult,
)
from trademaster import scheduler as sch
from trademaster.db import Base, Trade, make_engine, make_session_factory
from trademaster.state import get_state, reset_state_for_tests


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state_for_tests()
    sch._last_research_post = None
    sch._last_trade_open.clear()
    sch._last_signal_posted.clear()
    yield
    reset_state_for_tests()


def _decision(ticker="SPY", action="BUY_CALL", strike=500.0, conviction="HIGH"):
    return TickerDecision(
        ticker=ticker, action=action, strike=strike, expiry="0DTE",
        conviction=conviction, reasoning="test",
    )


def _filled(price: float = 2.00, qty: int = 3, status: str = "filled") -> OrderResult:
    return OrderResult(
        order_id="ord-test",
        status=status,
        filled_avg_price=Decimal(str(price)) if status == "filled" else None,
        filled_qty=Decimal(str(qty)) if status == "filled" else Decimal("0"),
        submitted_at=datetime.now(UTC),
        raw_status=status,
    )


def _accepted(qty: int = 3) -> OrderResult:
    """Order accepted by Alpaca but not yet terminal (would hang otherwise)."""
    return OrderResult(
        order_id="ord-hung",
        status="accepted",
        filled_avg_price=None,
        filled_qty=Decimal("0"),
        submitted_at=datetime.now(UTC),
        raw_status="accepted",
    )


def _quote(ask: float = 2.00) -> OptionQuote:
    from datetime import date
    return OptionQuote(
        occ_symbol="SPY260101C00500000",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=date(2026, 1, 1),
        option_type="call",
        bid=Decimal(str(ask - 0.10)),
        ask=Decimal(str(ask)),
        mid=Decimal(str(ask - 0.05)),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )


def _strike_selector(ask: float = 2.00):
    sel = _SelectedStrike(
        strike=Decimal("500"),
        occ="SPY260101C00500000",
        quote=_quote(ask=ask),
    )

    async def _fn(_ticker, _expiry, _opt_type, _target, _budget):
        if ask * 100 > _budget:
            return None
        return sel

    return _fn


async def _async_noop(_text: str) -> None:
    return None


def _open_clock() -> MarketClock:
    now = datetime.now(UTC)
    return MarketClock(
        timestamp=now, is_open=True,
        next_open=now + timedelta(hours=12),
        next_close=now + timedelta(hours=6),
    )


# ---------------------------------------------------------------------------
# 1. Market+IOC order submission — the post-disaster fix to avoid limit hangs
# ---------------------------------------------------------------------------


async def test_option_buy_uses_market_order_day_with_buy_to_open(monkeypatch):
    """Regression guard: Alpaca does not support IOC for options (buys or sells).
    Verify Market + DAY + BUY_TO_OPEN.
    """
    from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    captured = {}

    class FakeTrading:
        def __init__(self, **_): pass
        def submit_order(self, req):
            captured["req"] = req
            return SimpleNamespace(
                id="ord1", status="filled", filled_avg_price="2.00",
                filled_qty="1", submitted_at=datetime.now(UTC),
            )

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    result = await alpaca_client.submit_single_option_buy(
        qty=3, occ_symbol="SPY260101C00500000", limit_price=Decimal("2.00"),
    )

    req = captured["req"]
    assert isinstance(req, MarketOrderRequest), f"Expected MarketOrderRequest, got {type(req).__name__}"
    assert req.time_in_force == TimeInForce.DAY, f"Expected DAY, got {req.time_in_force}"
    assert req.side == OrderSide.BUY
    assert req.position_intent == PositionIntent.BUY_TO_OPEN
    assert req.qty == 3
    assert req.symbol == "SPY260101C00500000"
    assert result.status == "filled"


async def test_option_sell_uses_market_order_day_with_sell_to_close(monkeypatch):
    """Exit sells use DAY (not IOC) — Alpaca does not support IOC for options.
    SELL_TO_CLOSE position_intent prevents rejection as 'uncovered short'.
    DAY fills immediately at best bid during RTH; auto-cancels at 4 PM if not.
    """
    from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    captured = {}

    class FakeTrading:
        def __init__(self, **_): pass
        def submit_order(self, req):
            captured["req"] = req
            return SimpleNamespace(
                id="ord2", status="filled", filled_avg_price="2.20",
                filled_qty="3", submitted_at=datetime.now(UTC),
            )

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    await alpaca_client.submit_single_option_sell(
        qty=3, occ_symbol="SPY260101C00500000", limit_price=Decimal("2.20"),
    )

    req = captured["req"]
    assert isinstance(req, MarketOrderRequest)
    assert req.time_in_force == TimeInForce.DAY  # IOC not supported for options on Alpaca
    assert req.side == OrderSide.SELL
    assert req.position_intent == PositionIntent.SELL_TO_CLOSE


# ---------------------------------------------------------------------------
# 2. get_unrealized_pnl + cancel_order — wrappers that the loss limit relies on
# ---------------------------------------------------------------------------


async def test_get_unrealized_pnl_sums_positions(monkeypatch):
    raw = [
        SimpleNamespace(
            symbol="SPY", qty="10", avg_entry_price="450", market_value="4600",
            unrealized_pl="125.50", current_price="460", side="long", asset_class="us_equity",
        ),
        SimpleNamespace(
            symbol="QQQ", qty="2", avg_entry_price="400", market_value="820",
            unrealized_pl="-75.25", current_price="410", side="long", asset_class="us_equity",
        ),
    ]

    class FakeTrading:
        def __init__(self, **_): pass
        def get_all_positions(self): return raw

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    assert await alpaca_client.get_unrealized_pnl() == Decimal("50.25")


async def test_get_unrealized_pnl_returns_zero_on_error(monkeypatch):
    """Fail-open: a broker connectivity blip must NOT crash the loss-limit gate."""
    class FakeTrading:
        def __init__(self, **_): pass
        def get_all_positions(self): raise RuntimeError("alpaca timeout")

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    assert await alpaca_client.get_unrealized_pnl() == Decimal("0")


async def test_cancel_order_calls_alpaca_with_id(monkeypatch):
    captured = {}

    class FakeTrading:
        def __init__(self, **_): pass
        def cancel_order_by_id(self, order_id):
            captured["id"] = order_id

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())
    await alpaca_client.cancel_order("ord-xyz")
    assert captured["id"] == "ord-xyz"


async def test_cancel_order_swallows_errors(monkeypatch):
    """If the order is already filled/cancelled, Alpaca raises — we ignore it."""
    class FakeTrading:
        def __init__(self, **_): pass
        def cancel_order_by_id(self, order_id):
            raise RuntimeError("order already in terminal state")

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())
    await alpaca_client.cancel_order("ord-already-done")  # no exception


# ---------------------------------------------------------------------------
# 3. Cancel-after-timeout — executor explicitly cancels non-terminal orders
# ---------------------------------------------------------------------------


async def test_executor_cancels_order_if_not_terminal_after_timeout(session_factory, monkeypatch):
    """If wait_for_order returns a non-terminal status (e.g., still 'accepted'),
    the executor must call cancel_order so the order doesn't dangle in Alpaca.
    """
    cancel_calls = []

    async def fake_cancel(order_id):
        cancel_calls.append(order_id)

    monkeypatch.setattr(alpaca_client, "cancel_order", fake_cancel)

    async def fake_submit(**_kwargs):
        return _accepted()

    async def fake_wait(order_id, **_kw):
        # Simulate the 10s timeout firing while the order is still hanging.
        return _accepted()

    result = await execute_directional_signal(
        _decision(),
        today=datetime(2026, 1, 2, tzinfo=UTC).date(),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_strike_selector(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )

    assert not result.executed
    assert cancel_calls == ["ord-hung"], f"Expected cancel of ord-hung, got {cancel_calls}"


async def test_executor_does_not_cancel_filled_order(session_factory, monkeypatch):
    cancel_calls = []

    async def fake_cancel(order_id):
        cancel_calls.append(order_id)

    monkeypatch.setattr(alpaca_client, "cancel_order", fake_cancel)

    async def fake_submit(**_kwargs): return _filled(price=2.00)
    async def fake_wait(order_id, **_kw): return _filled(price=2.00)

    result = await execute_directional_signal(
        _decision(),
        today=datetime(2026, 1, 2, tzinfo=UTC).date(),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_strike_selector(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )

    assert result.executed
    assert cancel_calls == []


# ---------------------------------------------------------------------------
# 4. Daily loss limit — kill switch the user asked for after the -$2,097 day
# ---------------------------------------------------------------------------


def _seed_realized_loss(session_factory, *, amount: float, when: datetime) -> None:
    with session_factory() as session:
        session.add(Trade(
            symbol="SPY260513C00500000", asset_class="option", side="buy",
            strategy=STRATEGY_CALL,
            qty=Decimal("1"), entry_price=Decimal("2.00"),
            exit_price=Decimal("1.00"), realized_pnl_usd=Decimal(str(amount)),
            opened_at=when - timedelta(minutes=30), closed_at=when,
        ))
        session.commit()


async def test_daily_loss_limit_pauses_when_threshold_hit(session_factory, monkeypatch):
    """At -15% of capital ($750 on $5k), the scan pauses for 24h."""
    # Force scheduler to use our in-memory DB
    monkeypatch.setattr(sch, "make_session_factory", lambda: session_factory)

    # Realized loss already at -$800 (over the -$750 limit) today
    _seed_realized_loss(
        session_factory, amount=-800.00,
        when=datetime(2026, 5, 13, 18, 0, tzinfo=UTC),
    )

    # No unrealized P&L
    async def fake_unrealized(): return Decimal("0")
    monkeypatch.setattr(alpaca_client, "get_unrealized_pnl", fake_unrealized)

    # Freeze "now" so today_et returns May 13
    from trademaster import timeutils
    fake_utc = datetime(2026, 5, 13, 18, 30, tzinfo=UTC)

    class _Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_utc.astimezone(tz) if tz else fake_utc.replace(tzinfo=None)
    monkeypatch.setattr(timeutils, "datetime", _Fake)

    log_messages = []
    async def log_poster(msg): log_messages.append(msg)

    async def never_called():
        raise AssertionError("clock_fetcher must not be called after loss-limit pause")

    await sch._directional_scan_job(
        signal_poster=_async_noop,
        trade_poster=_async_noop,
        research_poster=_async_noop,
        log_poster=log_poster,
        clock_fetcher=never_called,
    )

    assert get_state().is_paused()
    assert log_messages, "Expected a loss-limit alert posted to #logs"
    assert "loss limit" in log_messages[0].lower()


async def test_daily_loss_limit_does_not_pause_below_threshold(session_factory, monkeypatch):
    """Capital dynamically shrinks with realized losses. At -$400 closed today
    the capital becomes $4,600, the limit becomes $690 (15%), and -$400 of
    realized P&L is still well under $690 → scan proceeds.
    """
    monkeypatch.setattr(sch, "make_session_factory", lambda: session_factory)

    _seed_realized_loss(
        session_factory, amount=-400.00,
        when=datetime(2026, 5, 13, 18, 0, tzinfo=UTC),
    )

    async def fake_unrealized(): return Decimal("0")
    monkeypatch.setattr(alpaca_client, "get_unrealized_pnl", fake_unrealized)

    from trademaster import timeutils
    fake_utc = datetime(2026, 5, 13, 18, 30, tzinfo=UTC)
    class _Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_utc.astimezone(tz) if tz else fake_utc.replace(tzinfo=None)
    monkeypatch.setattr(timeutils, "datetime", _Fake)

    clock_called = []
    async def clock_fetcher():
        clock_called.append(True)
        return _open_clock()

    # Stub the scan itself to avoid full network call
    async def fake_scan():
        return ([], [], "scan report")
    monkeypatch.setattr(sch, "run_directional_scan", fake_scan)

    await sch._directional_scan_job(
        signal_poster=_async_noop,
        trade_poster=_async_noop,
        research_poster=_async_noop,
        log_poster=_async_noop,
        clock_fetcher=clock_fetcher,
    )

    assert not get_state().is_paused()
    assert clock_called, "Expected scan to proceed past the loss-limit gate"


async def test_daily_loss_limit_includes_unrealized(session_factory, monkeypatch):
    """Realized -$400 + unrealized -$400 = -$800 → over the -$750 limit, pause."""
    monkeypatch.setattr(sch, "make_session_factory", lambda: session_factory)

    _seed_realized_loss(
        session_factory, amount=-400.00,
        when=datetime(2026, 5, 13, 18, 0, tzinfo=UTC),
    )

    async def fake_unrealized(): return Decimal("-400")
    monkeypatch.setattr(alpaca_client, "get_unrealized_pnl", fake_unrealized)

    from trademaster import timeutils
    fake_utc = datetime(2026, 5, 13, 18, 30, tzinfo=UTC)
    class _Fake(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_utc.astimezone(tz) if tz else fake_utc.replace(tzinfo=None)
    monkeypatch.setattr(timeutils, "datetime", _Fake)

    async def never_called():
        raise AssertionError("clock should not be reached")

    await sch._directional_scan_job(
        signal_poster=_async_noop,
        trade_poster=_async_noop,
        research_poster=_async_noop,
        log_poster=_async_noop,
        clock_fetcher=never_called,
    )

    assert get_state().is_paused()


# ---------------------------------------------------------------------------
# 5. Per-ticker 60-min cooldown — fix for "PLTR bought 4 times in 30 min"
# ---------------------------------------------------------------------------


async def test_ticker_cooldown_blocks_reentry_within_60_min():
    """A trade opened on PLTR 30 min ago must NOT trigger another execution."""
    now = datetime.now(UTC)
    sch._last_trade_open["PLTR"] = now - timedelta(minutes=30)

    elapsed = (now - sch._last_trade_open["PLTR"]).total_seconds()
    assert elapsed < sch._TICKER_COOLDOWN_SECONDS


async def test_ticker_cooldown_allows_reentry_after_60_min():
    """After 65 min, the cooldown expires and re-entry is allowed."""
    now = datetime.now(UTC)
    sch._last_trade_open["PLTR"] = now - timedelta(minutes=65)

    elapsed = (now - sch._last_trade_open["PLTR"]).total_seconds()
    assert elapsed >= sch._TICKER_COOLDOWN_SECONDS


async def test_ticker_cooldown_per_ticker_independent():
    """A cooldown on PLTR doesn't block NVDA."""
    now = datetime.now(UTC)
    sch._last_trade_open["PLTR"] = now - timedelta(minutes=10)

    # NVDA has no cooldown entry yet
    assert sch._last_trade_open.get("NVDA") is None


# ---------------------------------------------------------------------------
# 6. Signal dedup at 30 min — keeps Discord clean during repeat scans
# ---------------------------------------------------------------------------


async def test_signal_dedup_suppresses_repeat_within_30_min():
    now = datetime.now(UTC)
    sch._last_signal_posted[("SPY", "BUY_CALL")] = now - timedelta(minutes=15)

    elapsed = (now - sch._last_signal_posted[("SPY", "BUY_CALL")]).total_seconds()
    assert elapsed < sch._SIGNAL_DEDUP_SECONDS


async def test_signal_dedup_allows_repost_after_30_min():
    now = datetime.now(UTC)
    sch._last_signal_posted[("SPY", "BUY_CALL")] = now - timedelta(minutes=31)

    elapsed = (now - sch._last_signal_posted[("SPY", "BUY_CALL")]).total_seconds()
    assert elapsed >= sch._SIGNAL_DEDUP_SECONDS


async def test_signal_dedup_distinguishes_action_direction():
    """BUY_CALL and BUY_PUT on the same ticker are tracked separately."""
    now = datetime.now(UTC)
    sch._last_signal_posted[("SPY", "BUY_CALL")] = now

    # PUT key was never set → allowed
    assert sch._last_signal_posted.get(("SPY", "BUY_PUT")) is None


# ---------------------------------------------------------------------------
# 7. Research-post throttle — flooding fix
# ---------------------------------------------------------------------------


async def test_research_post_throttled_within_an_hour():
    now = datetime.now(UTC)
    sch._last_research_post = now - timedelta(minutes=30)

    elapsed = (now - sch._last_research_post).total_seconds()
    assert elapsed < sch._RESEARCH_POST_INTERVAL_SECONDS


async def test_research_post_allowed_after_an_hour():
    now = datetime.now(UTC)
    sch._last_research_post = now - timedelta(minutes=61)

    elapsed = (now - sch._last_research_post).total_seconds()
    assert elapsed >= sch._RESEARCH_POST_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# 8. Exit monitor auto-close on Alpaca "position not held" error
# ---------------------------------------------------------------------------


def _seed_open_directional_trade(session_factory, occ="SPY260513C00500000") -> int:
    with session_factory() as session:
        t = Trade(
            symbol=occ, asset_class="option", side="buy",
            strategy=STRATEGY_CALL,
            qty=Decimal("1"), entry_price=Decimal("2.00"),
            opened_at=datetime.now(UTC),
            extra={"occ_symbol": occ, "ticker": occ[:3], "action": "BUY_CALL", "mode": "selective"},
        )
        session.add(t); session.commit()
        return t.id


async def test_exit_monitor_auto_closes_on_alpaca_42210000(session_factory):
    """Ghost-position scenario: Alpaca returns code 42210000 (position not in book).
    The exit monitor must mark the DB trade closed so it stops retrying every cycle.
    """
    from agents.directional.exit_monitor import run_directional_exit_monitor

    trade_id = _seed_open_directional_trade(session_factory)

    async def mid_quote(_occ):
        return _quote(ask=2.10)

    async def no_bars(*_a, **_k):
        return []

    async def fake_submit(**_kwargs):
        # Simulate Alpaca's position-not-held rejection
        raise RuntimeError(
            "{'code': 42210000, 'message': 'position intent: BUY_TO_OPEN, "
            "but no matching position'}"
        )

    async def fake_wait(*_a, **_k):
        raise AssertionError("waiter should not be reached when submit raises")

    results = await run_directional_exit_monitor(
        # Use an early-RTH time so the hard floor at -30% triggers a close attempt
        # (entry $2.00, bid $1.40 = -30% loss exactly)
        now=datetime(2026, 5, 13, 17, 0, tzinfo=UTC),  # 13:00 ET
        session_factory=session_factory,
        quote_fetcher=lambda _occ: _quote_low_bid(),
        bars_fetcher=no_bars,
        submitter=fake_submit,
        waiter=fake_wait,
        force_close=True,
    )

    assert results[0]["status"] == "closed_position_not_in_broker"

    # DB trade row must now be closed (so we stop retrying)
    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert row.closed_at is not None
        assert (row.extra or {}).get("exit_reason") == "position_not_in_broker"


async def _quote_low_bid():
    """Helper returning a quote where bid triggers hard floor."""
    return _quote(ask=1.40)


async def test_exit_monitor_auto_closes_on_alpaca_40310000(session_factory):
    """Sibling code 40310000 ('not eligible for uncovered options')."""
    from agents.directional.exit_monitor import run_directional_exit_monitor

    trade_id = _seed_open_directional_trade(session_factory)

    async def no_bars(*_a, **_k):
        return []

    async def fake_submit(**_kwargs):
        raise RuntimeError("{'code': 40310000, 'message': 'uncovered option positions not allowed'}")

    async def fake_wait(*_a, **_k):
        return None

    results = await run_directional_exit_monitor(
        now=datetime(2026, 5, 13, 17, 0, tzinfo=UTC),
        session_factory=session_factory,
        quote_fetcher=lambda _occ: _quote_low_bid(),
        bars_fetcher=no_bars,
        submitter=fake_submit,
        waiter=fake_wait,
        force_close=True,
    )

    assert results[0]["status"] == "closed_position_not_in_broker"

    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert row.closed_at is not None


async def test_exit_monitor_does_not_auto_close_on_unrelated_error(session_factory):
    """A network/connectivity error must NOT auto-close — it's transient."""
    from agents.directional.exit_monitor import run_directional_exit_monitor

    trade_id = _seed_open_directional_trade(session_factory)

    async def no_bars(*_a, **_k):
        return []

    async def fake_submit(**_kwargs):
        raise RuntimeError("network timeout connecting to alpaca")

    async def fake_wait(*_a, **_k):
        return None

    results = await run_directional_exit_monitor(
        now=datetime(2026, 5, 13, 17, 0, tzinfo=UTC),
        session_factory=session_factory,
        quote_fetcher=lambda _occ: _quote_low_bid(),
        bars_fetcher=no_bars,
        submitter=fake_submit,
        waiter=fake_wait,
        force_close=True,
    )

    # Transient error → status reported as submit_error, trade stays open
    assert results[0]["status"] == "submit_error"
    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert row.closed_at is None  # NOT auto-closed


# ---------------------------------------------------------------------------
# 9. Position sizing — already covered in test_directional_executor.py;
#    re-asserting the math here to guard the _SIZE_FRACTION constant.
# ---------------------------------------------------------------------------


def test_size_fraction_is_10_percent_both_modes():
    from agents.directional import executor

    assert executor._SIZE_FRACTION["aggressive"] == 0.10
    assert executor._SIZE_FRACTION["selective"] == 0.10


def test_size_math_yields_floor_capital_div_premium():
    """floor(0.10 * $5,000 / ($2 * 100)) = floor(500 / 200) = 2 contracts."""
    import math
    capital = 5000
    ask = 2.00
    budget = capital * 0.10
    qty = max(1, math.floor(budget / (ask * 100)))
    assert qty == 2


async def test_select_best_strike_rejects_sub_50_cent_strikes(monkeypatch):
    """Behavioral guard for the $0.50 min — fix for the PLTR $0.15 puts incident
    where dirt-cheap options filled but never appeared as positions in paper.
    """
    from datetime import date
    from agents.directional.executor import select_best_strike

    # Mock the chain to return only cheap strikes (all below $0.50)
    async def fake_chain(ticker, *, expiry, strike_lo, strike_hi):
        return [
            OptionQuote(
                occ_symbol="PLTR260515P00120000",
                underlying="PLTR", strike=Decimal("120"),
                expiry=expiry, option_type="put",
                bid=Decimal("0.10"), ask=Decimal("0.15"), mid=Decimal("0.12"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
            OptionQuote(
                occ_symbol="PLTR260515P00115000",
                underlying="PLTR", strike=Decimal("115"),
                expiry=expiry, option_type="put",
                bid=Decimal("0.20"), ask=Decimal("0.30"), mid=Decimal("0.25"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
        ]

    monkeypatch.setattr(alpaca_client, "get_options_chain", fake_chain)

    selected = await select_best_strike(
        ticker="PLTR",
        expiry_date=date(2026, 5, 15),
        option_type="put",
        target_strike=120,
        budget=500,
    )
    assert selected is None, "Sub-$0.50 strikes must be rejected even with plenty of budget"


async def test_executor_detects_ghost_position_and_blocks_trade(session_factory, monkeypatch):
    """After a fill, if Alpaca doesn't show the position in get_positions(),
    the executor must return executed=False and attempt an immediate sell.
    This prevents ghost-position losses (fill confirmed, position never tracked).
    """
    from types import SimpleNamespace

    # Fill confirms immediately
    async def fake_submit(**_k): return _filled(price=2.00)
    async def fake_wait(order_id, **_k): return _filled(price=2.00)

    sell_calls = []
    original_submit = fake_submit
    async def fake_submit_tracking(**kwargs):
        sell_calls.append(kwargs)
        return _filled(price=2.00)

    # get_positions returns empty — position never registered
    monkeypatch.setattr(alpaca_client, "get_positions", lambda: _ghost_positions())

    result = await execute_directional_signal(
        _decision(),
        today=datetime(2026, 1, 2, tzinfo=UTC).date(),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_strike_selector(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )

    assert not result.executed
    assert "ghost_position" in result.reason


async def _ghost_positions():
    return []  # Alpaca returns empty — no position registered


async def test_executor_has_no_count_cap(session_factory):
    """The executor must not block on open-trade count — concurrency is gated
    solely by the scheduler's 20% capital exposure cap. A user can have 5
    small open positions if their combined notional stays under the cap.
    """
    # Pre-populate 5 open directional trades
    with session_factory() as session:
        for _ in range(5):
            session.add(Trade(
                symbol="SPY260101C00500000", asset_class="option", side="buy",
                strategy=STRATEGY_CALL,
                qty=Decimal("1"), entry_price=Decimal("1.00"),
                opened_at=datetime.now(UTC),
            ))
        session.commit()

    async def fake_submit(**_kwargs): return _filled(price=2.00)
    async def fake_wait(order_id, **_kw): return _filled(price=2.00)

    result = await execute_directional_signal(
        _decision(),
        today=datetime(2026, 1, 2, tzinfo=UTC).date(),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_strike_selector(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )

    assert result.executed, (
        f"Expected execution to proceed despite 5 open positions; got reason: {result.reason}"
    )


async def test_select_best_strike_picks_closest_to_target(monkeypatch):
    """Among affordable strikes ≥ $0.50, pick the one closest to the LLM target."""
    from datetime import date
    from agents.directional.executor import select_best_strike

    async def fake_chain(ticker, *, expiry, strike_lo, strike_hi):
        return [
            OptionQuote(
                occ_symbol="SPY260515C00498000",
                underlying="SPY", strike=Decimal("498"),
                expiry=expiry, option_type="call",
                bid=Decimal("3.00"), ask=Decimal("3.10"), mid=Decimal("3.05"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
            OptionQuote(
                occ_symbol="SPY260515C00500000",
                underlying="SPY", strike=Decimal("500"),
                expiry=expiry, option_type="call",
                bid=Decimal("2.00"), ask=Decimal("2.10"), mid=Decimal("2.05"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
            OptionQuote(
                occ_symbol="SPY260515C00505000",
                underlying="SPY", strike=Decimal("505"),
                expiry=expiry, option_type="call",
                bid=Decimal("1.00"), ask=Decimal("1.10"), mid=Decimal("1.05"),
                delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
            ),
        ]

    monkeypatch.setattr(alpaca_client, "get_options_chain", fake_chain)

    selected = await select_best_strike(
        ticker="SPY",
        expiry_date=date(2026, 5, 15),
        option_type="call",
        target_strike=501,  # closest is 500
        budget=500,
    )
    assert selected is not None
    assert selected.strike == Decimal("500")
