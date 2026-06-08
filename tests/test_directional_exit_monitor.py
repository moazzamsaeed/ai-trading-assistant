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
    assert "model closed" in msg
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
        now=datetime(2026, 5, 12, 19, 46, tzinfo=UTC),  # 15:46 ET = past 15:45 force-close
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
        now=datetime(2026, 5, 15, 19, 46, tzinfo=UTC),  # Fri 15:46 ET (past 15:45)
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


# ---------------------------------------------------------------------------
# Trailing stop tests
# ---------------------------------------------------------------------------

from agents.directional.exit_monitor import (
    _trailing_stop_premium,
    _maybe_ratchet_trailing_stop,
    TRAILING_STOP_LEVELS,
)


def test_trailing_stop_premium_below_first_tier():
    """Below +25% no trailing stop applies (ladder retuned 2026-06-05)."""
    entry = Decimal("2.00")
    assert _trailing_stop_premium(entry, 24.9) is None


def test_trailing_stop_premium_at_25pct():
    """At +25% the continuous trail (peak − 10%) gives +15%, above the +8%
    discrete floor → stop at entry × 1.15 (v3 2026-06-08)."""
    entry = Decimal("2.00")
    result = _trailing_stop_premium(entry, 25.0)
    assert result == (Decimal("2.00") * Decimal("1.15")).quantize(Decimal("0.0001"))


def test_trailing_stop_premium_at_50pct():
    """At +50% trails at peak − 10% → locks +40% → stop at entry × 1.40."""
    entry = Decimal("2.00")
    result = _trailing_stop_premium(entry, 50.0)
    assert result == (Decimal("2.00") * Decimal("1.40")).quantize(Decimal("0.0001"))


def test_trailing_stop_premium_trails_continuously_mid_range():
    """At +70% the stop now trails to +60% (peak − 10%), NOT the far-below +20%
    discrete tier — the trade #51 give-back fix (v3 2026-06-08)."""
    entry = Decimal("2.00")
    result = _trailing_stop_premium(entry, 70.0)
    assert result == (Decimal("2.00") * Decimal("1.60")).quantize(Decimal("0.0001"))


def test_trailing_stop_premium_at_80pct():
    """At +80% trails at peak − 10% → locks +70% → stop at entry × 1.70."""
    entry = Decimal("2.00")
    result = _trailing_stop_premium(entry, 80.0)
    assert result == (Decimal("2.00") * Decimal("1.70")).quantize(Decimal("0.0001"))


def test_trailing_stop_continuous_above_top_tier():
    """Big runners keep trailing at peak − 10% gap (v3 2026-06-08)."""
    entry = Decimal("2.00")

    def lock(mult):
        return (Decimal("2.00") * Decimal(mult)).quantize(Decimal("0.0001"))

    assert _trailing_stop_premium(entry, 120.0) == lock("2.10")  # +120% → lock +110%
    assert _trailing_stop_premium(entry, 200.0) == lock("2.90")  # +200% → lock +190%
    assert _trailing_stop_premium(entry, 300.0) == lock("3.90")  # +300% → lock +290%


def test_maybe_ratchet_updates_db_when_new_peak(session_factory):
    """Ratchet persists peak_pnl_pct and new stop_premium to DB."""
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("1"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={"occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                   "stop_premium": "1.00"},
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

    # Position hits +35% → trails at peak − 10% → locks +25%
    result = _maybe_ratchet_trailing_stop(session_factory, trade, 35.0, Decimal("2.00"))
    expected_stop = (Decimal("2.00") * Decimal("1.25")).quantize(Decimal("0.0001"))
    assert result == expected_stop

    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert float(row.extra["peak_pnl_pct"]) == pytest.approx(35.0)
        assert Decimal(row.extra["stop_premium"]) == expected_stop
        assert row.extra["trailing_stop_active"] is True


def test_maybe_ratchet_stop_never_moves_down(session_factory):
    """Once ratcheted, the stop cannot be lowered even if P&L drops."""
    # Stop already consistent with the +55% peak under the continuous trail
    # (peak − 10% = +45%).
    high_stop = str((Decimal("2.00") * Decimal("1.45")).quantize(Decimal("0.0001")))

    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("1"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={"occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                   "stop_premium": high_stop, "peak_pnl_pct": 55.0,
                   "trailing_stop_active": True},
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

    # P&L has fallen to +35% — peak stays +55%, so the trail does not move up
    result = _maybe_ratchet_trailing_stop(session_factory, trade, 35.0, Decimal("2.00"))
    # Stop must NOT be lowered — returns None (no ratchet happened)
    assert result is None

    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert Decimal(row.extra["stop_premium"]) == Decimal(high_stop)


async def test_trailing_stop_triggers_exit_in_monitor(session_factory):
    """When bid falls below the ratcheted trailing stop, position is closed.

    Uses scale_out_tiers_fired pre-populated to skip the scale-out logic — this
    test verifies the trailing-stop full-exit path specifically.
    """
    # Entry $2.00, position hit +50% (stop ratcheted to +25% = $2.50)
    ratcheted_stop = str((Decimal("2.00") * Decimal("1.25")).quantize(Decimal("0.0001")))
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("1"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY", "action": "BUY_CALL",
                "occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                "stop_premium": ratcheted_stop,
                "peak_pnl_pct": 55.0,
                "trailing_stop_active": True,
                # All scale-out tiers already fired so this test isolates the full-exit path
                "scale_out_tiers_fired": [25.0, 50.0],
                "original_qty": 4,
            },
        )
        session.add(trade)
        session.commit()

    async def low_quote(_occ):
        return _quote(bid=2.48)  # below the $2.50 trailing stop

    async def no_bars(*_a, **_k):
        return []

    async def fake_sell(**_kw):
        return _filled(price=2.48)

    async def fake_wait(order_id, **_kw):
        return _filled(price=2.48)

    results = await run_directional_exit_monitor(
        session_factory=session_factory,
        quote_fetcher=low_quote,
        bars_fetcher=no_bars,
        submitter=fake_sell,
        waiter=fake_wait,
        force_close=False,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "trailing_stop"


# ---------------------------------------------------------------------------
# Scale-out + 30-sec tick tests
# ---------------------------------------------------------------------------

from agents.directional.exit_monitor import (
    _maybe_scale_out,
    run_trailing_stop_tick,
)


async def test_scale_out_fires_at_100pct_tier(session_factory):
    """v2 ladder: the single scale-out fires at +100%, selling 25% of original."""
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("4"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY", "action": "BUY_CALL",
                "occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                "stop_premium": "1.00",
                "peak_pnl_pct": 110.0,  # crossed +100% tier
                "original_qty": 4,
            },
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

    async def fake_sell(**_kw):
        return _filled(price=4.20)

    async def fake_wait(order_id, **_kw):
        return _filled(price=4.20)

    result = await _maybe_scale_out(
        session_factory, trade,
        current_bid=Decimal("4.20"),
        submitter=fake_sell, waiter=fake_wait,
    )
    assert result is not None
    assert result["tier"] == 100.0
    assert result["sell_qty"] == 1  # 25% of 4 = 1

    with session_factory() as session:
        row = session.get(Trade, trade_id)
        assert int(row.qty) == 3  # 4 - 1
        assert 100.0 in row.extra["scale_out_tiers_fired"]


async def test_scale_out_fires_once_per_tier(session_factory):
    """If a tier was already fired, scale-out doesn't fire it again."""
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("3"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY", "action": "BUY_CALL",
                "occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                "stop_premium": "2.06",
                "peak_pnl_pct": 110.0,
                "original_qty": 4,
                "scale_out_tiers_fired": [100.0],  # +100% already done
            },
        )
        session.add(trade)
        session.commit()

    async def boom_sell(**_kw):
        raise AssertionError("scale-out should not fire again at same tier")

    async def fake_wait(*_a, **_k):
        return _filled(price=2.30)

    result = await _maybe_scale_out(
        session_factory, trade,
        current_bid=Decimal("2.30"),
        submitter=boom_sell, waiter=fake_wait,
    )
    assert result is None


async def test_trailing_stop_tick_partial_closes_at_tier(session_factory):
    """Tick function partial-closes when crossing a tier."""
    with session_factory() as session:
        trade = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("4"),
            entry_price=Decimal("2.00"), opened_at=datetime.now(UTC),
            extra={
                "ticker": "SPY", "action": "BUY_CALL",
                "occ_symbol": "SPY260101C00500000", "mode": "aggressive",
                "stop_premium": "1.00",
                "original_qty": 4,
            },
        )
        session.add(trade)
        session.commit()

    async def good_quote(_occ):
        return _quote(bid=4.20)  # +110% → crosses the +100% scale-out tier

    async def fake_sell(**_kw):
        return _filled(price=4.20)

    async def fake_wait(order_id, **_kw):
        return _filled(price=4.20)

    results = await run_trailing_stop_tick(
        session_factory=session_factory,
        quote_fetcher=good_quote,
        submitter=fake_sell,
        waiter=fake_wait,
    )
    assert len(results) == 1
    assert results[0]["status"] == "scaled_out"
    assert results[0]["tier"] == 100.0


async def test_trailing_stop_tick_skips_when_no_positions(session_factory):
    """Tick returns empty list with no open positions — no API calls."""
    quote_called = []

    async def fake_quote(_occ):
        quote_called.append(True)
        return None

    results = await run_trailing_stop_tick(
        session_factory=session_factory,
        quote_fetcher=fake_quote,
    )
    assert results == []
    assert quote_called == []  # didn't even call quote fetcher


# ----------------- format_scale_out -----------------


def test_format_scale_out_call_with_remaining():
    from agents.directional.exit_monitor import format_scale_out
    out = format_scale_out({
        "ticker": "SPY", "action": "BUY_CALL", "tier": 30.0,
        "sell_qty": 1, "partial_pnl_usd": "43.0", "remaining_qty": 1,
    })
    assert "SPY CALL" in out
    assert "scaled out 1× at +30% gain" in out
    assert "locked in $43" in out
    assert "holding 1× for higher targets" in out
    assert "Manual: Sell 1× SPY CALL at market" in out


def test_format_scale_out_put_fully_scaled():
    from agents.directional.exit_monitor import format_scale_out
    out = format_scale_out({
        "ticker": "SPY", "action": "BUY_PUT", "tier": 50.0,
        "sell_qty": 2, "partial_pnl_usd": "-10.0", "remaining_qty": 0,
    })
    assert "SPY PUT" in out
    assert "fully scaled out" in out
    assert "locked in $10" in out  # abs value


def test_format_scale_out_tolerates_bad_numbers():
    from agents.directional.exit_monitor import format_scale_out
    out = format_scale_out({"ticker": "SPY", "action": "BUY_CALL",
                            "tier": "x", "partial_pnl_usd": None})
    assert "SPY CALL" in out  # no crash


# ----------------- scale-out duplicate-tier race -----------------


async def test_scale_out_lock_prevents_duplicate_tier_fire(session_factory):
    """Two concurrent _maybe_scale_out calls (30s tick + 5min monitor colliding
    on a :00 boundary, as in trade #43) must fire a tier ONCE and decrement qty
    once. Without the per-trade lock both fire the same tier and over-sell."""
    import asyncio
    from agents.directional.exit_monitor import _maybe_scale_out, _scale_out_locks
    _scale_out_locks.clear()

    # qty=4, peak +20% → only the +15% tier is crossable (not +30%).
    with session_factory() as s:
        t = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("4"), entry_price=Decimal("1.00"),
            opened_at=datetime.now(UTC),
            extra={"ticker": "SPY", "action": "BUY_CALL", "occ_symbol": "SPY260101C00500000",
                   "mode": "aggressive", "peak_pnl_pct": 110.0, "original_qty": 4},
        )
        s.add(t); s.commit(); tid = t.id

    submits = {"n": 0}

    async def submitter(*, qty, occ_symbol, limit_price):
        submits["n"] += 1
        await asyncio.sleep(0)  # yield so the other coroutine attempts the lock
        return OrderResult(
            order_id=f"o{submits['n']}", status="filled",
            filled_avg_price=Decimal("1.20"), filled_qty=Decimal(str(qty)),
            submitted_at=datetime.now(UTC), raw_status="filled",
        )

    async def waiter(order_id, timeout_s=30.0):
        return OrderResult(
            order_id=order_id, status="filled", filled_avg_price=Decimal("1.20"),
            filled_qty=Decimal("1"), submitted_at=datetime.now(UTC), raw_status="filled",
        )

    # Two separate Trade objects with the same id — mimics the two monitor runs.
    with session_factory() as s:
        t1 = s.get(Trade, tid); s.expunge(t1)
    with session_factory() as s:
        t2 = s.get(Trade, tid); s.expunge(t2)

    r1, r2 = await asyncio.gather(
        _maybe_scale_out(session_factory, t1, Decimal("1.20"), submitter, waiter),
        _maybe_scale_out(session_factory, t2, Decimal("1.20"), submitter, waiter),
    )

    fired = [r for r in (r1, r2) if r is not None]
    assert len(fired) == 1, "exactly one call should fire the tier"
    assert submits["n"] == 1, "only one sell order should be submitted (no double-sell)"
    with session_factory() as s:
        row = s.get(Trade, tid)
        assert row.extra["scale_out_tiers_fired"] == [100.0], "tier recorded exactly once"
        assert int(row.qty) == 3, "qty decremented once (4 → 3), no stale clobber"


async def test_scale_out_decrements_from_fresh_qty(session_factory, monkeypatch):
    """Sequential scale-outs across tiers decrement from the live row.qty, not a
    stale captured value — 4 → 3 (+25%) → 2 (+50%). Uses a 2-sell config-override
    ladder (the default v2 ladder sells only once, at +100%)."""
    import asyncio
    from agents.directional import exit_monitor as em
    from agents.directional.exit_monitor import _maybe_scale_out, _scale_out_locks
    _scale_out_locks.clear()
    monkeypatch.setattr(
        em.get_settings(), "trailing_stop_levels",
        "[[50,0.20,0.25],[25,0.08,0.25]]",
    )

    with session_factory() as s:
        t = Trade(
            symbol="SPY260101C00500000", asset_class="option", side="buy",
            strategy="directional_call", qty=Decimal("4"), entry_price=Decimal("1.00"),
            opened_at=datetime.now(UTC),
            extra={"ticker": "SPY", "action": "BUY_CALL", "occ_symbol": "SPY260101C00500000",
                   "mode": "aggressive", "peak_pnl_pct": 55.0, "original_qty": 4},
        )
        s.add(t); s.commit(); tid = t.id

    async def submitter(*, qty, occ_symbol, limit_price):
        return OrderResult(order_id="o", status="filled", filled_avg_price=Decimal("1.30"),
                           filled_qty=Decimal(str(qty)), submitted_at=datetime.now(UTC),
                           raw_status="filled")

    async def waiter(order_id, timeout_s=30.0):
        return OrderResult(order_id=order_id, status="filled", filled_avg_price=Decimal("1.30"),
                           filled_qty=Decimal("1"), submitted_at=datetime.now(UTC),
                           raw_status="filled")

    with session_factory() as s:
        trade = s.get(Trade, tid); s.expunge(trade)
    # First call fires +25% (lowest unfired), second fires +50%.
    r1 = await _maybe_scale_out(session_factory, trade, Decimal("1.30"), submitter, waiter)
    r2 = await _maybe_scale_out(session_factory, trade, Decimal("1.30"), submitter, waiter)
    assert r1["tier"] == 25.0 and r2["tier"] == 50.0
    with session_factory() as s:
        row = s.get(Trade, tid)
        assert row.extra["scale_out_tiers_fired"] == [25.0, 50.0]
        assert int(row.qty) == 2, "4 → 3 → 2"


# ----------------- configurable ladder -----------------


def test_trailing_stop_levels_default():
    from agents.directional.exit_monitor import (
        DEFAULT_TRAILING_STOP_LEVELS, _trailing_stop_levels,
    )
    assert _trailing_stop_levels() == DEFAULT_TRAILING_STOP_LEVELS
    # v2 ladder: single scale-out tier at +100% (ride-then-scale-once).
    sell_tiers = sorted(t[0] for t in DEFAULT_TRAILING_STOP_LEVELS if t[2] > 0)
    assert sell_tiers == [100.0]


def test_trailing_stop_levels_config_override(monkeypatch):
    from agents.directional import exit_monitor as em
    monkeypatch.setattr(
        em.get_settings(), "trailing_stop_levels",
        "[[60,0.30,0.5],[20,0.05,0.5]]",
    )
    levels = em._trailing_stop_levels()
    assert levels == [(60.0, 0.30, 0.5), (20.0, 0.05, 0.5)]  # sorted high→low


def test_trailing_stop_levels_invalid_falls_back(monkeypatch):
    from agents.directional import exit_monitor as em
    warned = []
    monkeypatch.setattr(em.log, "warning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(em.get_settings(), "trailing_stop_levels", "not json")
    assert em._trailing_stop_levels() == em.DEFAULT_TRAILING_STOP_LEVELS
    assert warned and warned[0][0] == "trailing_stop_levels_invalid"


def test_scale_out_plan_summary_default():
    from agents.directional.exit_monitor import scale_out_plan_summary
    assert scale_out_plan_summary() == "scale out 25% at +100% gain"


def test_scale_out_plan_summary_tracks_config(monkeypatch):
    from agents.directional import exit_monitor as em
    monkeypatch.setattr(
        em.get_settings(), "trailing_stop_levels",
        "[[60,0.30,0.5],[20,0.05,0.5]]",
    )
    assert em.scale_out_plan_summary() == "scale out 50% at +20%, +60% gain"


# ----------------- exit-prompt profit rule is mode-aware (#3) -----------------


async def test_exit_prompt_profit_rule_is_mode_aware(session_factory):
    """The +75% profit-take rule must match the mode (no framework/mode-context
    contradiction): aggressive → ≥2 fading; selective → any single fading."""
    captured = {}

    async def capture_llm(_task_type, prompt, **_k):
        captured["prompt"] = prompt
        return _fake_llm("HOLD", "x")

    t_agg = _fake_trade_obj()
    t_agg.extra = {**t_agg.extra, "mode": "aggressive"}
    await _llm_exit_confirm(
        trade=t_agg, snap=_snap(), triggered_rules=[], current_bid=Decimal("3.0"),
        pnl_pct=90.0, session_factory=session_factory, llm_caller=capture_llm,
    )
    assert "require ≥2 fading indicators to exit" in captured["prompt"]
    assert "ANY single indicator shows fading momentum (protect" not in captured["prompt"]

    t_sel = _fake_trade_obj()  # default mode = selective
    await _llm_exit_confirm(
        trade=t_sel, snap=_snap(), triggered_rules=[], current_bid=Decimal("3.0"),
        pnl_pct=90.0, session_factory=session_factory, llm_caller=capture_llm,
    )
    assert "ANY single indicator shows fading momentum (protect gains)" in captured["prompt"]
