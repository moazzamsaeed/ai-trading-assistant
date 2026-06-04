"""Tests for the directional options executor."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.directional.executor import (
    STRATEGY_CALL,
    STRATEGY_PUT,
    _SelectedStrike,
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


def _selected(ask: float = 2.00):
    """Return a strike_selector that always picks the given ask price."""
    q = _quote(ask=ask)
    sel = _SelectedStrike(strike=Decimal("500"), occ="SPY260101C00500000", quote=q)
    async def _fn(_ticker, _expiry, _opt_type, _target, _budget):
        if ask * 100 > _budget:
            return None
        return sel
    return _fn


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
    monday = date(2026, 5, 11)  # Monday
    tuesday = date(2026, 5, 12)
    wednesday = date(2026, 5, 13)

    # SPY has true daily 0DTE — every weekday
    assert _resolve_expiry("0DTE", monday, "SPY") == monday
    assert _resolve_expiry("0DTE", tuesday, "SPY") == tuesday

    # QQQ/IWM only have 0DTE on Mon/Wed/Fri — Bug 8 regression
    assert _resolve_expiry("0DTE", monday, "QQQ") == monday       # Mon: allowed
    assert _resolve_expiry("0DTE", wednesday, "QQQ") == wednesday  # Wed: allowed
    assert _resolve_expiry("0DTE", tuesday, "QQQ") == date(2026, 5, 15)   # Tue: redirect to Friday
    assert _resolve_expiry("0DTE", tuesday, "IWM") == date(2026, 5, 15)   # Tue: redirect to Friday

    # AMD only has weekly options
    assert _resolve_expiry("0DTE", monday, "AMD") == date(2026, 5, 15)
    # No ticker given — defaults to next Friday (safe fallback)
    assert _resolve_expiry("0DTE", monday) == date(2026, 5, 15)


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


async def test_execute_medium_conviction_0dte_allowed(session_factory):
    """MEDIUM conviction 0DTE is now allowed for SPY — the 2:30 PM time window
    in the scheduler is the only theta protection needed."""
    async def fake_submit(**_k): return _filled_order(price=1.50)
    async def fake_wait(order_id, **_k): return _filled_order(price=1.50)

    result = await execute_directional_signal(
        TickerDecision("SPY", "BUY_CALL", 500.0, "0DTE", "MEDIUM", "test"),
        today=date(2026, 1, 2),
        session_factory=session_factory,
        strike_selector=_selected(ask=1.50),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed


async def test_execute_medium_conviction_weekly_allowed(session_factory):
    """MEDIUM conviction WEEKLY is allowed — theta isn't lethal with 3-5 DTE."""
    async def fake_submit(**_k): return _filled_order(price=1.50)
    async def fake_wait(order_id, **_k): return _filled_order(price=1.50)

    result = await execute_directional_signal(
        TickerDecision("SPY", "BUY_CALL", 500.0, "WEEKLY", "MEDIUM", "test"),
        today=date(2026, 1, 2),        # Monday — WEEKLY resolves to Friday Jan 6
        session_factory=session_factory,
        strike_selector=_selected(ask=1.50),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed


async def test_execute_high_conviction_0dte_allowed(session_factory):
    """HIGH conviction 0DTE is allowed — ATM with max gamma is the right choice."""
    async def fake_submit(**_k): return _filled_order(price=2.00)
    async def fake_wait(order_id, **_k): return _filled_order(price=2.00)

    result = await execute_directional_signal(
        _decision(action="BUY_CALL", expiry="0DTE", conviction="HIGH"),
        today=date(2026, 1, 2),
        session_factory=session_factory,
        strike_selector=_selected(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed


async def test_execute_missing_strike_returns_no_execute():
    d = TickerDecision("SPY", "BUY_CALL", None, "0DTE", "HIGH", "test")
    result = await execute_directional_signal(d)
    assert not result.executed


async def test_execute_too_expensive_skips(session_factory):
    """selector returns None when ask exceeds budget — execution skipped."""
    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        capital_usd=Decimal("100"),  # $100 budget — $60/contract (ask=0.60) won't fit
        session_factory=session_factory,
        strike_selector=_selected(ask=1.50),  # $150/contract > $100 budget
    )
    assert not result.executed
    assert "budget" in result.reason or "no affordable" in result.reason


async def test_execute_no_quote_skips(session_factory):
    """selector returns None when no chain strike found — execution skipped."""
    async def no_strike(_ticker, _expiry, _opt_type, _target, _budget):
        return None

    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=no_strike,
    )
    assert not result.executed
    assert "budget" in result.reason or "no affordable" in result.reason


async def test_execute_order_rejected_no_trade_row(session_factory):
    async def fake_submit(**_kwargs):
        return _rejected_order()

    async def fake_wait(order_id, **_kw):
        return _rejected_order()

    result = await execute_directional_signal(
        _decision(),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_selected(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert not result.executed
    assert "rejected" in result.reason

    with session_factory() as session:
        assert session.query(Trade).count() == 0


async def test_execute_success_persists_trade(session_factory):
    async def fake_submit(**_kwargs):
        return _filled_order(price=2.00)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=2.00)

    result = await execute_directional_signal(
        _decision(action="BUY_CALL", expiry="0DTE"),
        today=date(2026, 1, 2),
        mode="aggressive",
        session_factory=session_factory,
        strike_selector=_selected(ask=2.00),
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
    async def fake_submit(**_kwargs):
        return _filled_order(price=1.50)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=1.50)

    result = await execute_directional_signal(
        _decision(action="BUY_PUT", expiry="0DTE"),
        today=date(2026, 1, 2),
        mode="selective",
        session_factory=session_factory,
        strike_selector=_selected(ask=1.50),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed
    with session_factory() as session:
        trade = session.get(Trade, result.trade_id)
        assert trade.strategy == STRATEGY_PUT


async def test_execute_aggressive_sizing(session_factory):
    """Position sizing uses available budget but is capped by MAX_LOSS_PER_TRADE_USD.

    The cap was introduced 2026-05-30 as the direct defense against the trade
    #37 pattern (large contract count × cheap premium = big absolute loss when
    the option goes to zero). At $5000 budget and $2.00/share ask:
      - Without cap: floor($5000 / $200) = 25 contracts → up to $5000 loss
      - With cap:    floor($500 / $200)  = 2 contracts  → bounded $500 loss
    """
    submitted_kwargs = {}

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
        strike_selector=_selected(ask=2.00),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert submitted_kwargs["qty"] == 2


# ---------------------------------------------------------------------------
# Bug fixes — regression tests
# ---------------------------------------------------------------------------


async def test_ghost_position_calls_seller_not_submitter(session_factory):
    """Bug 1: ghost position recovery must call SELL, not BUY again."""
    buy_calls = []
    sell_calls = []

    async def fake_submit(**kwargs):
        buy_calls.append(kwargs)
        return _filled_order(price=2.00)

    async def fake_sell(**kwargs):
        sell_calls.append(kwargs)
        return _filled_order(price=2.00)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=2.00)

    # Simulate ghost: position never appears in Alpaca book
    async def no_positions():
        return []

    import agents.directional.executor as _ex
    original = _ex.alpaca_client.get_positions
    _ex.alpaca_client.get_positions = no_positions

    try:
        result = await execute_directional_signal(
            _decision(action="BUY_CALL", expiry="0DTE", conviction="HIGH"),
            today=date(2026, 1, 2),
            mode="aggressive",
            session_factory=session_factory,
            strike_selector=_selected(ask=2.00),
            submitter=fake_submit,
            seller=fake_sell,
            waiter=fake_wait,
        )
    finally:
        _ex.alpaca_client.get_positions = original

    assert not result.executed
    assert "ghost" in result.reason
    assert len(buy_calls) == 1   # only the original BUY
    assert len(sell_calls) == 1  # recovery used SELL, not BUY again


async def test_pt_sl_computed_from_fill_price_not_ask(session_factory):
    """Bug 4: PT/SL must use actual fill price, not the pre-order ask."""
    ask_price = 2.00
    fill_price = 1.85  # better fill than ask

    async def fake_submit(**_kw):
        return _filled_order(price=fill_price)

    async def fake_wait(order_id, **_kw):
        return _filled_order(price=fill_price)

    result = await execute_directional_signal(
        _decision(action="BUY_CALL", expiry="0DTE", conviction="HIGH"),
        today=date(2026, 1, 2),
        mode="selective",
        session_factory=session_factory,
        strike_selector=_selected(ask=ask_price),
        submitter=fake_submit,
        waiter=fake_wait,
    )
    assert result.executed

    with session_factory() as session:
        trade = session.get(__import__("trademaster.db", fromlist=["Trade"]).Trade, result.trade_id)
        extra = trade.extra or {}
        stop = float(extra["stop_premium"])
        pt = float(extra["profit_target_premium"])

        # selective: stop=-30%, pt=+50% — both from FILL price, not ask
        expected_stop = fill_price * 0.70
        expected_pt = fill_price * 1.50
        assert abs(stop - expected_stop) < 0.001, f"stop {stop} should be ~{expected_stop} (from fill)"
        assert abs(pt - expected_pt) < 0.001, f"pt {pt} should be ~{expected_pt} (from fill)"

        # Sanity: if computed from ask they'd be different
        ask_stop = ask_price * 0.70
        assert abs(stop - ask_stop) > 0.001, "stop must NOT match the pre-order ask price"


async def test_bid_ask_spread_filter_rejects_wide_spread(monkeypatch):
    """Options with spread > 50% of mid are filtered out in select_best_strike."""
    from agents.directional.executor import select_best_strike
    from integrations.alpaca_client import OptionQuote
    from decimal import Decimal as D
    from datetime import date

    # bid=0.50, ask=3.00 → spread=2.50, mid=1.75, spread_pct=143% — too wide
    wide = OptionQuote(
        occ_symbol="SPY260101C00500000", underlying="SPY",
        strike=D("500"), expiry=date(2026, 1, 1), option_type="call",
        bid=D("0.50"), ask=D("3.00"), mid=D("1.75"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    # tight spread: bid=1.80, ask=2.20 → spread=0.40, mid=2.00, pct=20% — OK
    tight = OptionQuote(
        occ_symbol="SPY260101C00500000", underlying="SPY",
        strike=D("500"), expiry=date(2026, 1, 1), option_type="call",
        bid=D("1.80"), ask=D("2.20"), mid=D("2.00"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )

    import integrations.alpaca_client as _ac

    async def chain_wide(_ticker, **_k):
        return [wide]

    async def chain_tight(_ticker, **_k):
        return [tight]

    # Wide spread: select_best_strike should return None (retry_delay_s=0 to
    # avoid the real retry sleeps in the no-candidate path).
    monkeypatch.setattr(_ac, "get_options_chain", chain_wide)
    result_wide = await select_best_strike(
        "SPY", date(2026, 1, 1), "call", 500.0, 500.0, retry_delay_s=0.0
    )
    assert result_wide is None, "wide spread must be rejected"

    # Tight spread: should be accepted
    monkeypatch.setattr(_ac, "get_options_chain", chain_tight)
    result_tight = await select_best_strike("SPY", date(2026, 1, 1), "call", 500.0, 500.0)
    assert result_tight is not None, "tight spread must be accepted"


async def test_select_best_strike_retries_until_ask_populates(monkeypatch):
    """No-ask first attempt, then a live ask appears → retry succeeds (the
    2026-06-03 BUY_PUT miss: indicative feed had no ask early-session)."""
    from agents.directional.executor import select_best_strike
    from integrations.alpaca_client import OptionQuote
    from decimal import Decimal as D
    from datetime import date
    import integrations.alpaca_client as _ac

    no_ask = OptionQuote(
        occ_symbol="SPY260101P00500000", underlying="SPY",
        strike=D("500"), expiry=date(2026, 1, 1), option_type="put",
        bid=D("1.50"), ask=D("0"), mid=D("0.75"),  # ask=0 → un-buyable
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    good = OptionQuote(
        occ_symbol="SPY260101P00500000", underlying="SPY",
        strike=D("500"), expiry=date(2026, 1, 1), option_type="put",
        bid=D("1.80"), ask=D("2.10"), mid=D("1.95"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )

    calls = {"n": 0}

    async def flaky_chain(_ticker, **_k):
        calls["n"] += 1
        return [no_ask] if calls["n"] == 1 else [good]

    monkeypatch.setattr(_ac, "get_options_chain", flaky_chain)
    result = await select_best_strike(
        "SPY", date(2026, 1, 1), "put", 500.0, 500.0, retry_delay_s=0.0
    )
    assert result is not None, "should succeed once the ask populates on retry"
    assert calls["n"] == 2, "should have retried exactly once"


async def test_select_best_strike_gives_up_after_retries_when_no_ask(monkeypatch):
    """Ask never populates → None after exhausting retries (the real miss case)."""
    from agents.directional.executor import select_best_strike
    from integrations.alpaca_client import OptionQuote
    from decimal import Decimal as D
    from datetime import date
    import integrations.alpaca_client as _ac

    no_ask = OptionQuote(
        occ_symbol="SPY260101P00500000", underlying="SPY",
        strike=D("500"), expiry=date(2026, 1, 1), option_type="put",
        bid=D("1.50"), ask=D("0"), mid=D("0.75"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    calls = {"n": 0}

    async def dead_chain(_ticker, **_k):
        calls["n"] += 1
        return [no_ask]

    monkeypatch.setattr(_ac, "get_options_chain", dead_chain)
    result = await select_best_strike(
        "SPY", date(2026, 1, 1), "put", 500.0, 500.0, retries=2, retry_delay_s=0.0
    )
    assert result is None
    assert calls["n"] == 3, "1 initial + 2 retries"


# ----------------- strike-range direction (2026-06-03 BUY_PUT regression) -----------------


async def test_put_strike_range_includes_atm(monkeypatch):
    """Put search range must span $30 OTM to $10 ITM (i.e. include ATM), not
    skew $10-30 OTM. Regression for the 2026-06-03 BUY_PUT misses where deep-OTM
    puts priced under the $0.30 floor were the only strikes searched."""
    from agents.directional.executor import select_best_strike
    from integrations.alpaca_client import OptionQuote
    from decimal import Decimal as D
    from datetime import date
    import integrations.alpaca_client as _ac

    atm = OptionQuote(  # tradeable ATM put
        occ_symbol="SPY260101P00750000", underlying="SPY", strike=D("750"),
        expiry=date(2026, 1, 1), option_type="put",
        bid=D("0.40"), ask=D("0.45"), mid=D("0.425"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    deep = OptionQuote(  # deep-OTM put under the $0.30 floor
        occ_symbol="SPY260101P00725000", underlying="SPY", strike=D("725"),
        expiry=date(2026, 1, 1), option_type="put",
        bid=D("0.02"), ask=D("0.04"), mid=D("0.03"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    captured = {}

    async def chain(_ticker, *, expiry, strike_lo, strike_hi):
        captured["lo"] = float(strike_lo)
        captured["hi"] = float(strike_hi)
        return [q for q in (atm, deep) if strike_lo <= q.strike <= strike_hi]

    monkeypatch.setattr(_ac, "get_options_chain", chain)
    res = await select_best_strike(
        "SPY", date(2026, 1, 1), "put", 750.0, 3000.0, retry_delay_s=0.0
    )

    # put range = target-30 .. target+10 (includes ATM)
    assert captured["lo"] == 720.0 and captured["hi"] == 760.0
    assert res is not None and float(res.strike) == 750.0, "must pick the tradeable ATM put"


async def test_call_strike_range_unchanged(monkeypatch):
    """Call search range stays $10 ITM to $30 OTM (target-10 .. target+30)."""
    from agents.directional.executor import select_best_strike
    from integrations.alpaca_client import OptionQuote
    from decimal import Decimal as D
    from datetime import date
    import integrations.alpaca_client as _ac

    atm = OptionQuote(
        occ_symbol="SPY260101C00750000", underlying="SPY", strike=D("750"),
        expiry=date(2026, 1, 1), option_type="call",
        bid=D("1.40"), ask=D("1.45"), mid=D("1.425"),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )
    captured = {}

    async def chain(_ticker, *, expiry, strike_lo, strike_hi):
        captured["lo"] = float(strike_lo)
        captured["hi"] = float(strike_hi)
        return [atm] if strike_lo <= atm.strike <= strike_hi else []

    monkeypatch.setattr(_ac, "get_options_chain", chain)
    res = await select_best_strike(
        "SPY", date(2026, 1, 1), "call", 750.0, 3000.0, retry_delay_s=0.0
    )
    # call range = target-10 .. target+30
    assert captured["lo"] == 740.0 and captured["hi"] == 780.0
    assert res is not None and float(res.strike) == 750.0
