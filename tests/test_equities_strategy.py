"""Tests for the equities entry strategy (agents/equities/strategy.py).

Each gate is exercised independently. The decision reads scalar indicators from
`snap` and uses `bars` only for momentum/ADX slope and the resuming check, so most
tests pass a short 2-bar list (slopes → None, skipped) and drive the gate purely
through snap + market_ctx. The two slope gates get dedicated longer-series tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.equities import strategy
from agents.equities.strategy import decide_equity, _macd_hist_slope, _adx_slope
from integrations.alpaca_client import Bar

_T0 = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)


def _bar(close, i=0, *, open_=None):
    o = open_ if open_ is not None else close
    return Bar(timestamp=_T0 + timedelta(minutes=5 * i),
               open=Decimal(str(o)), high=Decimal(str(close + 0.1)),
               low=Decimal(str(close - 0.1)), close=Decimal(str(close)),
               volume=1000, vwap=Decimal(str(close)))


def _bars(closes):
    return [_bar(c, i) for i, c in enumerate(closes)]


def _snap(*, price, vwap, ema20, adx, atr10, rsi9=55, vol=1.5, macd=0.2, macd_signal=0.0):
    return {
        "bars": 40, "last_close": str(price), "vwap": str(vwap), "ema20": str(ema20),
        "adx": str(adx), "atr10": str(atr10), "rsi9": str(rsi9),
        "volume_ratio_20": str(vol), "macd": str(macd), "macd_signal": str(macd_signal),
    }


def _ctx(*, session_high, session_low, levels=None):
    """levels = extra S/R prices via multi_day (prev_high used as a generic level)."""
    ctx = {"session_high": session_high, "session_low": session_low, "multi_day": {}}
    if levels:
        # stash arbitrary levels where _collect_levels will find them
        for i, lv in enumerate(levels):
            ctx["multi_day"][["prev_high", "prev_low", "ma5", "ma10", "prev_close"][i]] = lv
    return ctx


# ── pullback → HIGH ───────────────────────────────────────────────────────────
def test_pullback_uptrend_is_high_call():
    # uptrend, retraced 40% off the high, last bar turning back up, ~2.4 ATR room.
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0)  # only level above 101.6 is 104 → 2.4 ATR
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, ctx)
    assert d.action == "BUY_CALL"
    assert d.conviction == "HIGH"
    assert d.analysis["setup"] == "pullback"


def test_pullback_downtrend_is_high_put():
    snap = _snap(price=98.4, vwap=99.5, ema20=100.0, adx=30, atr10=1.0, rsi9=45)
    ctx = _ctx(session_high=102.0, session_low=96.0)  # support 96 is 2.4 ATR below
    d = decide_equity("AMZN", _bars([98.8, 98.4]), snap, ctx)
    assert d.action == "BUY_PUT"
    assert d.conviction == "HIGH"
    assert d.analysis["setup"] == "pullback"


# ── breakout → MEDIUM ─────────────────────────────────────────────────────────
def test_volume_breakout_is_medium():
    # price at the session high with volume → breakout; resistance (prev_high 107) gives room.
    snap = _snap(price=104.0, vwap=102.5, ema20=102.0, adx=30, atr10=1.0, rsi9=60, vol=1.5)
    ctx = _ctx(session_high=104.0, session_low=98.0, levels=[107.0])
    d = decide_equity("META", _bars([103.5, 104.0]), snap, ctx)
    assert d.action == "BUY_CALL"
    assert d.conviction == "MEDIUM"
    assert d.analysis["setup"] == "breakout"


def test_at_high_without_volume_is_chase_hold():
    snap = _snap(price=104.0, vwap=102.5, ema20=102.0, adx=30, atr10=1.0, vol=0.9)  # weak vol
    ctx = _ctx(session_high=104.0, session_low=98.0, levels=[107.0])
    d = decide_equity("META", _bars([103.5, 104.0]), snap, ctx)
    assert d.action == "HOLD"
    assert "chase" in d.reasoning


# ── chase → HOLD ──────────────────────────────────────────────────────────────
def test_mid_extension_not_resuming_is_chase():
    # same pullback geometry but last bar is DOWN → not resuming → chase.
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", _bars([102.0, 101.6]), snap, ctx)
    assert d.action == "HOLD"
    assert "chase" in d.reasoning


# ── room gate ─────────────────────────────────────────────────────────────────
def test_no_room_into_resistance_holds():
    # resistance (ma5 = 102.0) only 0.4 ATR above 101.6 → no room.
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0, levels=[102.0])
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, ctx)
    assert d.action == "HOLD"
    assert "no room" in d.reasoning


# ── exhaustion gates ──────────────────────────────────────────────────────────
def test_rsi_exhausted_call_holds():
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0, rsi9=75)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, ctx)
    assert d.action == "HOLD"
    assert "RSI exhausted" in d.reasoning


def test_volume_fading_holds():
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0, vol=0.5)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, ctx)
    assert d.action == "HOLD"
    assert "volume fading" in d.reasoning


# ── overextension / trend qualification ───────────────────────────────────────
def test_overextended_in_atr_holds():
    snap = _snap(price=105.0, vwap=100.0, ema20=99.0, adx=30, atr10=1.0)  # 5 ATR from VWAP
    ctx = _ctx(session_high=106.0, session_low=98.0)
    d = decide_equity("MU", _bars([104.0, 105.0]), snap, ctx)
    assert d.action == "HOLD"
    assert "overextended" in d.reasoning


def test_weak_adx_holds():
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=20, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, ctx)
    assert d.action == "HOLD"
    assert "weak trend" in d.reasoning


def test_no_trend_holds():
    # price above VWAP but below EMA → neither up nor down.
    snap = _snap(price=100.0, vwap=99.0, ema20=101.0, adx=30, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("QQQ", _bars([99.5, 100.0]), snap, ctx)
    assert d.action == "HOLD"
    assert "no trend" in d.reasoning


def test_unbootstrapped_holds():
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0)
    snap["atr10"] = None  # not bootstrapped
    d = decide_equity("NVDA", _bars([101.0, 101.6]), snap, _ctx(session_high=104, session_low=98))
    assert d.action == "HOLD"
    assert "not bootstrapped" in d.reasoning


# ── the multi-ticker proof: ATR-normalization doesn't over-filter a volatile name ──
def test_volatile_ticker_not_over_filtered():
    # PLTR-like: 2% from VWAP would be "overextended" under the base 0.5% raw band,
    # but it's only 1 ATR here → must pass and fire a pullback HIGH.
    snap = _snap(price=300.0, vwap=294.0, ema20=295.0, adx=30, atr10=6.0)  # 1 ATR from VWAP
    ctx = _ctx(session_high=312.0, session_low=288.0)  # room 2 ATR; retrace 50%
    d = decide_equity("PLTR", _bars([299.0, 300.0]), snap, ctx)
    assert d.action == "BUY_CALL"
    assert d.conviction == "HIGH"
    assert d.analysis["dist_atr"] == 1.0


# ── slope gates (longer series) ───────────────────────────────────────────────
def test_decelerating_uptrend_holds_on_macd():
    # Strong rise that rolls over at the end → MACD histogram contracting.
    closes = [100 + min(i, 20) * 0.5 for i in range(28)] + [109.6, 109.0, 108.4]
    bars = _bars(closes)
    slope = _macd_hist_slope(bars)
    assert slope is not None and slope < 0, "fixture must produce a contracting histogram"
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=30, atr10=1.0)
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", bars, snap, ctx)
    assert d.action == "HOLD"
    assert "momentum decelerating" in d.reasoning


def test_adx_rollover_holds():
    # Strong trend then chop at the end → ADX declining; snap reports a high ADX.
    closes = [100 + i * 0.6 for i in range(30)] + [118.0, 117.6, 118.1, 117.7, 118.0]
    bars = _bars(closes)
    slope = _adx_slope(bars, 38.0)
    assert slope is not None and slope < 0, "fixture must produce a falling ADX"
    snap = _snap(price=101.6, vwap=100.5, ema20=100.0, adx=38, atr10=1.0)  # adx >= rollover hi
    ctx = _ctx(session_high=104.0, session_low=98.0)
    d = decide_equity("NVDA", bars, snap, ctx)
    assert d.action == "HOLD"
    assert "rolling over" in d.reasoning
