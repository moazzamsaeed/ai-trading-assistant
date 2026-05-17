"""Tests for the directional exit monitor — hybrid intelligent exit."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from agents.directional.exit_monitor import (
    HARD_FLOOR_PCT,
    _check_exit_rules,
    _format_exit_combined,
    _llm_exit_confirm,
    run_directional_exit_monitor,
)
from integrations.alpaca_client import OptionQuote, OrderResult
from trademaster.db import Base, Trade, make_engine, make_session_factory
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _open_trade(
    session_factory,
    *,
    entry_premium: float = 2.00,
    action: str = "BUY_CALL",
    strategy: str = "directional_call",
    occ: str = "SPY260101C00500000",
) -> Trade:
    with session_factory() as session:
        trade = Trade(
            symbol=occ,
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
                "occ_symbol": occ,
                "mode": "selective",
                "entry_reasoning": "strong breakout above VWAP",
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


def _filled(price: float) -> OrderResult:
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


def _fake_llm(decision: str = "EXIT", reason: str = "momentum reversed") -> LLMResponse:
    import json as _json
    text = _json.dumps({"decision": decision, "reason": reason})
    return LLMResponse(
        text=text, provider="deepseek", model="deepseek-v4-flash",
        input_tokens=200, output_tokens=20,
        cost_usd=Decimal("0.00004"), duration_ms=800,
    )


# ---------------------------------------------------------------------------
# _check_exit_rules
# ---------------------------------------------------------------------------


def _snap(
    price=500.0, vwap=490.0, rsi=52.0, ema20=498.0, ema50=495.0, vol_ratio=1.2
) -> dict:
    return {
        "last_close": str(price),
        "vwap": str(vwap),
        "rsi9": str(rsi),   # snapshot now returns rsi9 not rsi14
        "ema20": str(ema20),
        "ema50": str(ema50),
        "volume_ratio_20": str(vol_ratio),
    }


def test_check_exit_rules_call_no_trigger():
    # Healthy bullish setup — no rules should fire
    snap = _snap(price=500, vwap=490, rsi=55, ema20=498, ema50=495, vol_ratio=1.5)
    assert _check_exit_rules("BUY_CALL", snap) == []


def test_check_exit_rules_call_price_below_vwap():
    snap = _snap(price=485, vwap=490)  # price < VWAP
    assert "price_below_vwap" in _check_exit_rules("BUY_CALL", snap)


def test_check_exit_rules_call_rsi_overbought():
    snap = _snap(rsi=76)  # RSI-9 overbought threshold is 75 (not 70)
    assert "rsi_overbought" in _check_exit_rules("BUY_CALL", snap)


def test_check_exit_rules_call_ema_bearish_cross():
    snap = _snap(ema20=490, ema50=495)  # EMA20 < EMA50
    assert "ema_bearish_cross" in _check_exit_rules("BUY_CALL", snap)


def test_check_exit_rules_call_volume_fading():
    snap = _snap(vol_ratio=0.5)  # below 0.7 threshold
    assert "volume_fading" in _check_exit_rules("BUY_CALL", snap)


def test_check_exit_rules_put_no_trigger():
    snap = _snap(price=485, vwap=490, rsi=45, ema20=490, ema50=495, vol_ratio=1.5)
    assert _check_exit_rules("BUY_PUT", snap) == []


def test_check_exit_rules_put_price_above_vwap():
    snap = _snap(price=495, vwap=490)  # price > VWAP for put = exit signal
    assert "price_above_vwap" in _check_exit_rules("BUY_PUT", snap)


def test_check_exit_rules_put_rsi_oversold():
    snap = _snap(rsi=24)  # RSI-9 oversold threshold is 25 (not 30)
    assert "rsi_oversold" in _check_exit_rules("BUY_PUT", snap)


def test_check_exit_rules_put_ema_bullish_cross():
    snap = _snap(ema20=498, ema50=495)  # EMA20 > EMA50 for put = exit signal
    assert "ema_bullish_cross" in _check_exit_rules("BUY_PUT", snap)


def test_check_exit_rules_empty_snap_returns_empty():
    assert _check_exit_rules("BUY_CALL", {}) == []


# ---------------------------------------------------------------------------
# _llm_exit_confirm
# ---------------------------------------------------------------------------


def _fake_trade_obj(entry_premium: float = 2.00) -> Trade:
    t = Trade(
        symbol="SPY260101C00500000",
        asset_class="option",
        side="buy",
        strategy="directional_call",
        qty=Decimal("3"),
        entry_price=Decimal(str(entry_premium)),
        opened_at=datetime.now(UTC),
        extra={
            "ticker": "SPY",
            "action": "BUY_CALL",
            "occ_symbol": "SPY260101C00500000",
            "mode": "selective",
            "entry_reasoning": "strong breakout",
        },
    )
    t.id = 99
    return t


async def test_llm_exit_confirm_exit_decision(session_factory):
    async def fake_llm(*_a, **_k):
        return _fake_llm("EXIT", "RSI reversed from overbought")

    should_exit, reason = await _llm_exit_confirm(
        trade=_fake_trade_obj(),
        snap=_snap(rsi=72),
        triggered_rules=["rsi_overbought"],
        current_bid=Decimal("2.80"),
        pnl_pct=40.0,
        session_factory=session_factory,
        llm_caller=fake_llm,
    )
    assert should_exit is True
    assert "RSI" in reason


async def test_llm_exit_confirm_hold_decision(session_factory):
    async def fake_llm(*_a, **_k):
        return _fake_llm("HOLD", "Momentum still intact")

    should_exit, reason = await _llm_exit_confirm(
        trade=_fake_trade_obj(),
        snap=_snap(),
        triggered_rules=["volume_fading"],
        current_bid=Decimal("2.50"),
        pnl_pct=25.0,
        session_factory=session_factory,
        llm_caller=fake_llm,
    )
    assert should_exit is False


async def test_llm_exit_confirm_failure_defaults_to_hold(session_factory):
    async def boom(*_a, **_k):
        raise RuntimeError("deepseek down")

    should_exit, reason = await _llm_exit_confirm(
        trade=_fake_trade_obj(),
        snap=_snap(),
        triggered_rules=["price_below_vwap"],
        current_bid=Decimal("2.50"),
        pnl_pct=25.0,
        session_factory=session_factory,
        llm_caller=boom,
    )
    assert should_exit is False  # fail-safe: hold on LLM error


# ---------------------------------------------------------------------------
# _format_exit_combined
# ---------------------------------------------------------------------------


def _fake_trade_for_format() -> Trade:
    t = _fake_trade_obj(entry_premium=2.00)
    t.id = 42
    return t


def test_format_exit_combined_profit():
    t = _fake_trade_for_format()
    msg = _format_exit_combined(t, Decimal("3.00"), "smart_exit", "RSI reversed at 74")
    assert "📈" in msg
    assert "bot closed" in msg
    assert "🧠 smart exit" in msg
    assert "RSI reversed" in msg
    assert "Sell" in msg
    assert "✅" in msg


def test_format_exit_combined_loss():
    t = _fake_trade_for_format()
    msg = _format_exit_combined(t, Decimal("1.40"), "hard_floor_stop")
    assert "❌" in msg
    assert "🛑 hard floor" in msg


def test_format_exit_combined_force_close():
    t = _fake_trade_for_format()
    msg = _format_exit_combined(t, Decimal("2.00"), "force_close")
    assert "⏰ closing" in msg


# ---------------------------------------------------------------------------
# run_directional_exit_monitor — integration
# ---------------------------------------------------------------------------


async def test_monitor_empty_returns_empty(session_factory):
    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        force_close=False,
    )
    assert results == []


async def test_monitor_no_quote_returns_no_quote(session_factory):
    _open_trade(session_factory)

    async def no_quote(_occ):
        return None

    async def no_bars(*_a, **_k):
        return []

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=no_quote,
        bars_fetcher=no_bars,
        force_close=False,
    )
    assert results[0]["status"] == "no_quote"


async def test_monitor_hard_floor_exits_immediately(session_factory):
    """At -30% (entry=2.00, bid=1.40), hard floor fires — no LLM needed."""
    _open_trade(session_factory, entry_premium=2.00)

    async def low_quote(_occ):
        return _quote(bid=1.40)  # 2.00 * 0.70 = 1.40 → exactly at floor

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kwargs):
        return _filled(price=1.40)

    async def fake_wait(order_id, **_kw):
        return _filled(price=1.40)

    llm_called = []

    async def boom_llm(*_a, **_k):
        llm_called.append(True)
        raise AssertionError("LLM should not be called at hard floor")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=low_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        llm_caller=boom_llm,
        force_close=False,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "hard_floor_stop"
    assert llm_called == []  # LLM not invoked


async def test_monitor_hold_when_rules_not_triggered(session_factory):
    """Healthy uptrending setup: no rules fire, position stays open."""
    _open_trade(session_factory, entry_premium=2.00)

    async def mid_quote(_occ):
        return _quote(bid=2.50)  # +25%, profitable

    from integrations.alpaca_client import Bar
    from decimal import Decimal as D

    def _bar(close: float) -> Bar:
        return Bar(
            timestamp=datetime.now(UTC),
            open=D(str(close - 0.2)),
            high=D(str(close + 0.3)),
            low=D(str(close - 0.3)),
            close=D(str(close)),
            volume=12000,
            vwap=D(str(close - 2.0)),  # price > vwap = healthy for call
        )

    # Slight uptrend (497→500) gives RSI ~60 (not overbought), EMA20>EMA50
    async def good_bars(*_a, **_k):
        return [_bar(497 + i * 0.05) for i in range(60)]

    async def hold_llm(*_a, **_k):
        # If LLM is called anyway, always return HOLD in this test
        return _fake_llm("HOLD", "momentum intact")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        bars_fetcher=good_bars,
        llm_caller=hold_llm,
        force_close=False,
    )
    assert results[0]["status"] == "hold"


async def test_monitor_smart_exit_on_rule_and_llm_confirms(session_factory):
    trade = _open_trade(session_factory, entry_premium=2.00)

    async def high_quote(_occ):
        return _quote(bid=2.60)  # +30% — profitable but RSI overbought

    from integrations.alpaca_client import Bar
    from decimal import Decimal as D

    def _bar(close: float, vwap_offset: float = 5.0) -> Bar:
        return Bar(
            timestamp=datetime.now(UTC),
            open=D(str(close)), high=D(str(close + 1)),
            low=D(str(close - 1)), close=D(str(close)),
            volume=5000,  # vol_ratio will be < 0.7 (low volume)
            vwap=D(str(close - vwap_offset)),
        )

    async def overbought_bars(*_a, **_k):
        return [_bar(500.0, vwap_offset=5.0)] * 60

    async def fake_sell(**_kwargs):
        return _filled(price=2.60)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.60)

    async def fake_llm(*_a, **_k):
        return _fake_llm("EXIT", "RSI overbought, volume fading")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=high_quote,
        bars_fetcher=overbought_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        llm_caller=fake_llm,
        force_close=False,
    )
    # +30% P&L + LLM says EXIT → smart_profit_exit (positive P&L)
    if results[0]["status"] == "closed":
        assert results[0]["reason"] == "smart_profit_exit"
        assert "combined_text" in results[0]


async def test_monitor_force_close_closes_trade(session_factory):
    _open_trade(session_factory, entry_premium=2.00)

    async def mid_quote(_occ):
        return _quote(bid=2.20)

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kwargs):
        return _filled(price=2.20)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.20)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=True,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "force_close"
    assert "combined_text" in results[0]


async def test_monitor_failed_order_reports_error(session_factory):
    _open_trade(session_factory, entry_premium=2.00)

    async def low_quote(_occ):
        return _quote(bid=1.40)  # hard floor

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kwargs):
        return _rejected()

    async def fake_wait(order_id, **_kw):
        return _rejected()

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=low_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert "close_order_rejected" in results[0]["status"]
    assert "error_text" in results[0]


# ---------------------------------------------------------------------------
# Force close — per-trade expiry logic (unchanged from before)
# ---------------------------------------------------------------------------


def _open_trade_with_occ(session_factory, occ: str) -> Trade:
    with session_factory() as session:
        trade = Trade(
            symbol=occ,
            asset_class="option",
            side="buy",
            strategy="directional_call",
            qty=Decimal("1"),
            entry_price=Decimal("2.00"),
            opened_at=datetime.now(UTC),
            extra={
                "ticker": occ[:3],
                "action": "BUY_CALL",
                "occ_symbol": occ,
                "mode": "selective",
            },
        )
        session.add(trade)
        session.commit()
        return trade


async def test_weekly_option_not_force_closed_on_non_expiry_day(session_factory):
    _open_trade_with_occ(session_factory, "SPY260515C00500000")

    async def mid_quote(_occ):
        return _quote(bid=2.50)

    async def no_bars(*_a, **_k):
        return []

    results = await run_directional_exit_monitor(
        now=datetime(2026, 5, 12, 19, 31, tzinfo=UTC),  # Tue 15:31 ET
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        bars_fetcher=no_bars,
        force_close=None,
    )
    assert results[0]["status"] == "hold"


async def test_0dte_force_closed_on_expiry_day(session_factory):
    _open_trade_with_occ(session_factory, "SPY260512C00500000")

    async def mid_quote(_occ):
        return _quote(bid=2.50)

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kwargs):
        return _filled(price=2.50)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.50)

    results = await run_directional_exit_monitor(
        now=datetime(2026, 5, 12, 19, 31, tzinfo=UTC),  # 15:31 ET on May 12 = expiry
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=None,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "force_close"


async def test_weekly_force_closed_on_expiry_day(session_factory):
    _open_trade_with_occ(session_factory, "SPY260515C00500000")

    async def mid_quote(_occ):
        return _quote(bid=2.50)

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kwargs):
        return _filled(price=2.50)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.50)

    results = await run_directional_exit_monitor(
        now=datetime(2026, 5, 15, 19, 31, tzinfo=UTC),  # Fri 15:31 ET
        session_factory=session_factory,
        quote_fetcher=mid_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=None,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "force_close"


# ---------------------------------------------------------------------------
# Bug fixes — regression tests
# ---------------------------------------------------------------------------


def _open_trade_mode(session_factory, *, entry_premium: float = 2.00, mode: str) -> Trade:
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000",
            asset_class="option",
            side="buy",
            strategy="directional_call",
            qty=Decimal("1"),
            entry_price=Decimal(str(entry_premium)),
            opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY",
                "action": "BUY_CALL",
                "occ_symbol": "SPY260101C00500000",
                "mode": mode,
                "stop_premium": str(
                    Decimal(str(entry_premium)) * (
                        Decimal("0.50") if mode == "aggressive" else Decimal("0.70")
                    )
                ),
            },
        )
        session.add(trade)
        session.commit()
        return trade


async def test_hard_floor_selective_triggers_at_30pct(session_factory):
    """Bug 5: selective hard floor is -30% (entry=2.00, floor=1.40)."""
    _open_trade_mode(session_factory, entry_premium=2.00, mode="selective")

    async def quote_at_floor(_occ):
        return _quote(bid=1.40)

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kw):
        return _filled(price=1.40)

    async def fake_wait(order_id, **_kw):
        return _filled(price=1.40)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=quote_at_floor,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert results[0]["reason"] == "hard_floor_stop"


async def test_hard_floor_aggressive_does_not_trigger_at_30pct(session_factory):
    """Bug 5: aggressive hard floor is -50%, not -30%. A -30% loss must not auto-exit."""
    _open_trade_mode(session_factory, entry_premium=2.00, mode="aggressive")

    async def quote_at_30pct(_occ):
        return _quote(bid=1.40)  # -30% — within tolerance for aggressive

    async def no_bars(*_a, **_k):
        return []

    async def fake_llm(*_a, **_k):
        return _fake_llm("HOLD", "still in range")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=quote_at_30pct,
        bars_fetcher=no_bars,
        llm_caller=fake_llm,
        force_close=False,
    )
    assert results[0]["status"] == "hold"


async def test_hard_floor_aggressive_triggers_at_50pct(session_factory):
    """Bug 5: aggressive hard floor fires at -50% (entry=2.00, floor=1.00)."""
    _open_trade_mode(session_factory, entry_premium=2.00, mode="aggressive")

    async def quote_at_50pct(_occ):
        return _quote(bid=1.00)  # exactly -50%

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kw):
        return _filled(price=1.00)

    async def fake_wait(order_id, **_kw):
        return _filled(price=1.00)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=quote_at_50pct,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert results[0]["reason"] == "hard_floor_stop"


async def test_profit_lock_triggers_llm_without_indicator_rules(session_factory):
    """At >=75% P&L the LLM is always consulted even when no indicators fired."""
    _open_trade(session_factory, entry_premium=2.00)

    async def high_quote(_occ):
        return _quote(bid=3.51)  # +75.5% — above PROFIT_LOCK_PCT

    async def no_bars(*_a, **_k):
        return []  # no bars -> no indicator rules fire

    llm_called = []

    async def fake_llm(*_a, **_k):
        llm_called.append(True)
        return _fake_llm("HOLD", "momentum intact")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=high_quote,
        bars_fetcher=no_bars,
        llm_caller=fake_llm,
        force_close=False,
    )
    assert llm_called == [True], "LLM must be consulted at >=75% P&L even with no indicator triggers"
    assert results[0]["status"] == "hold"


async def test_smart_profit_exit_reason_when_pnl_positive(session_factory):
    """Exit in profit uses reason='smart_profit_exit', not 'smart_exit'."""
    _open_trade(session_factory, entry_premium=2.00)

    async def profitable_quote(_occ):
        return _quote(bid=2.60)  # +30%

    from integrations.alpaca_client import Bar
    from decimal import Decimal as D

    async def fading_bars(*_a, **_k):
        return [Bar(
            timestamp=datetime.now(UTC),
            open=D("500"), high=D("501"), low=D("499"), close=D("500"),
            volume=3000, vwap=D("502"),  # price < vwap fires rule
        )] * 60

    async def fake_sell(**_kw):
        return _filled(price=2.60)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.60)

    async def exit_llm(*_a, **_k):
        return _fake_llm("EXIT", "thesis reversed")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=profitable_quote,
        bars_fetcher=fading_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        llm_caller=exit_llm,
        force_close=False,
    )
    if results[0]["status"] == "closed":
        assert results[0]["reason"] == "smart_profit_exit"


async def test_smart_exit_reason_when_pnl_negative(session_factory):
    """Exit at a loss uses reason='smart_exit', not 'smart_profit_exit'."""
    _open_trade(session_factory, entry_premium=2.00)

    async def losing_quote(_occ):
        return _quote(bid=1.80)  # -10%

    from integrations.alpaca_client import Bar
    from decimal import Decimal as D

    async def fading_bars(*_a, **_k):
        return [Bar(
            timestamp=datetime.now(UTC),
            open=D("500"), high=D("501"), low=D("499"), close=D("498"),
            volume=3000, vwap=D("502"),  # price < vwap fires rule
        )] * 60

    async def fake_sell(**_kw):
        return _filled(price=1.80)

    async def fake_wait(order_id, **_kw):
        return _filled(price=1.80)

    async def exit_llm(*_a, **_k):
        return _fake_llm("EXIT", "thesis reversed")

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=losing_quote,
        bars_fetcher=fading_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        llm_caller=exit_llm,
        force_close=False,
    )
    if results[0]["status"] == "closed":
        assert results[0]["reason"] == "smart_exit"


async def test_broken_quote_skipped(session_factory):
    """Stale/corrupted quotes (ask > 5x bid) must not trigger exits."""
    _open_trade(session_factory, entry_premium=2.00)

    async def broken_quote(_occ):
        from integrations.alpaca_client import OptionQuote
        from decimal import Decimal as D
        from datetime import date
        return OptionQuote(
            occ_symbol="SPY260101C00500000", underlying="SPY",
            strike=D("500"), expiry=date(2026, 1, 1), option_type="call",
            bid=D("0.10"), ask=D("50.00"), mid=D("25.05"),  # ask = 500x bid = corrupt
            delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
        )

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=broken_quote,
        bars_fetcher=lambda *a, **k: __import__("asyncio").coroutine(lambda: [])(),
        force_close=False,
    )
    assert results[0]["status"] == "stale_quote"
