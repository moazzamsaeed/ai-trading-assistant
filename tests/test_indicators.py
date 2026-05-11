"""Indicator math tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from integrations.alpaca_client import Bar
from trademaster import indicators


def _bar(t: datetime, close: float, *, vol: int = 1000, vwap=None) -> Bar:
    return Bar(
        timestamp=t,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=vol,
        vwap=Decimal(str(vwap)) if vwap is not None else None,
    )


def _bars(closes: list[float], *, start_vol: int = 1000) -> list[Bar]:
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    out = []
    for i, c in enumerate(closes):
        out.append(_bar(t + timedelta(minutes=i * 5), c, vol=start_vol))
    return out


# ----------------- VWAP -----------------


def test_vwap_empty_returns_none():
    assert indicators.vwap([]) is None


def test_vwap_constant_price():
    bars = _bars([100.0] * 5)
    # All bars at $100 → VWAP should equal $100
    assert indicators.vwap(bars) == Decimal("100.00")


def test_vwap_weights_by_volume():
    bars = [
        _bar(datetime(2026, 5, 11, 14, 0, tzinfo=UTC), 100, vol=100),
        _bar(datetime(2026, 5, 11, 14, 5, tzinfo=UTC), 110, vol=900),
    ]
    # Weighted: (100*100 + 110*900) / 1000 = 109
    v = indicators.vwap(bars)
    assert v is not None
    assert abs(v - Decimal("109.00")) < Decimal("0.50")  # tolerance for typical-price math


# ----------------- EMA -----------------


def test_ema_returns_none_when_too_few_bars():
    bars = _bars([100.0] * 5)
    assert indicators.ema(bars, 20) is None


def test_ema_of_flat_series_equals_price():
    bars = _bars([100.0] * 30)
    assert indicators.ema(bars, 20) == Decimal("100.00")


def test_ema_responds_to_uptrend():
    bars = _bars([100 + i for i in range(30)])
    e20 = indicators.ema(bars, 20)
    # EMA of an arithmetic ramp lags the latest value but is well above start
    assert Decimal("110") < e20 < Decimal("130")


# ----------------- RSI -----------------


def test_rsi_returns_none_when_too_few_bars():
    bars = _bars([100.0, 101.0])
    assert indicators.rsi(bars, period=14) is None


def test_rsi_pure_uptrend_is_100():
    bars = _bars([100 + i for i in range(20)])
    r = indicators.rsi(bars, period=14)
    assert r == Decimal("100.00")


def test_rsi_pure_downtrend_is_zero():
    bars = _bars([200 - i for i in range(20)])
    r = indicators.rsi(bars, period=14)
    assert r == Decimal("0.00")


def test_rsi_choppy_is_near_50():
    closes = []
    for i in range(20):
        closes.append(100 + (1 if i % 2 == 0 else -1))
    bars = _bars(closes)
    r = indicators.rsi(bars, period=14)
    # Alternating up/down → RSI hovers around 50
    assert Decimal("40") < r < Decimal("60")


# ----------------- ATR -----------------


def test_atr_returns_none_when_too_few_bars():
    bars = _bars([100.0, 101.0])
    assert indicators.atr(bars, period=14) is None


def test_atr_constant_range():
    # 20 bars, each with high-low = 0.4
    bars = _bars([100.0] * 20)
    a = indicators.atr(bars, period=14)
    # True range is ~0.4 per bar; ATR should be close to that
    assert Decimal("0.30") < a < Decimal("0.50")


# ----------------- volume ratio -----------------


def test_volume_ratio_surge():
    bars = []
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    for i in range(20):
        bars.append(_bar(t + timedelta(minutes=i * 5), 100, vol=1000))
    # Add a surge bar at the end
    bars.append(_bar(t + timedelta(minutes=100), 100, vol=3000))
    ratio = indicators.volume_ratio(bars, lookback=20)
    assert ratio == Decimal("3.00")


def test_volume_ratio_none_when_too_few_bars():
    bars = _bars([100.0] * 5)
    assert indicators.volume_ratio(bars, lookback=20) is None


# ----------------- snapshot -----------------


def test_snapshot_includes_all_keys():
    bars = _bars([100 + i * 0.1 for i in range(60)])
    snap = indicators.snapshot(bars)
    for k in ("bars", "last_close", "last_volume", "vwap", "rsi14", "ema20", "ema50", "atr14"):
        assert k in snap
    assert snap["bars"] == 60
    assert snap["ema20"] is not None
    assert snap["ema50"] is not None


def test_snapshot_handles_few_bars():
    bars = _bars([100.0] * 5)
    snap = indicators.snapshot(bars)
    assert snap["bars"] == 5
    assert snap["vwap"] is not None
    assert snap["ema20"] is None  # insufficient data
    assert snap["rsi14"] is None


def test_snapshot_empty():
    snap = indicators.snapshot([])
    assert snap == {"bars": 0}
