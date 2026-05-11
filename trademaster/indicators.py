"""Pure-Python technical indicators for the directional intraday agent.

No numpy/pandas dependencies — all bars-in, scalar-out. Inputs are
sequences of `alpaca_client.Bar` objects (oldest-first). Outputs are
Decimals (or None when there's insufficient data).

Indicators here are descriptive — they distill a 30-bar history into a
small set of numbers the LLM can reason about. We deliberately keep
this simple: VWAP, RSI(14), EMA(20), EMA(50), ATR(14), volume ratio.
"""

from __future__ import annotations

from decimal import Decimal

from integrations.alpaca_client import Bar


def _typical(b: Bar) -> Decimal:
    """Typical price = (high + low + close) / 3, used for VWAP if Alpaca's
    `vwap` field is missing."""
    return (b.high + b.low + b.close) / Decimal("3")


# ----------------- VWAP -----------------


def vwap(bars: list[Bar]) -> Decimal | None:
    """Volume-weighted average price over the supplied bars.

    Prefers Alpaca's per-bar vwap when present; otherwise computes from
    (high+low+close)/3 × volume.
    """
    if not bars:
        return None
    num = Decimal("0")
    vol = Decimal("0")
    for b in bars:
        v = Decimal(b.volume)
        if v <= 0:
            continue
        price = b.vwap if b.vwap is not None else _typical(b)
        num += price * v
        vol += v
    if vol == 0:
        return None
    return (num / vol).quantize(Decimal("0.01"))


# ----------------- EMA -----------------


def ema(bars: list[Bar], period: int) -> Decimal | None:
    """Exponential moving average of close prices over `period` bars.

    Returns None if we have fewer than `period` bars. Uses standard
    α = 2 / (period + 1) smoothing.
    """
    if len(bars) < period:
        return None
    alpha = Decimal(2) / Decimal(period + 1)
    # Seed with simple average of the first `period` closes.
    seed = sum((b.close for b in bars[:period]), Decimal("0")) / Decimal(period)
    e = seed
    for b in bars[period:]:
        e = (b.close * alpha) + (e * (Decimal(1) - alpha))
    return e.quantize(Decimal("0.01"))


# ----------------- RSI -----------------


def rsi(bars: list[Bar], period: int = 14) -> Decimal | None:
    """Relative Strength Index — Wilder's smoothing.

    Returns a value in [0, 100], or None when we have fewer than
    `period + 1` bars (need at least one delta seed plus warm-up).
    """
    if len(bars) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for i in range(1, len(bars)):
        diff = bars[i].close - bars[i - 1].close
        if diff > 0:
            gains.append(diff)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(-diff)

    # Wilder's smoothing: first avg = simple over `period`, then EMA-like.
    avg_gain = sum(gains[:period], Decimal("0")) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal("0")) / Decimal(period)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * Decimal(period - 1) + gains[i]) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + losses[i]) / Decimal(period)

    if avg_loss == 0:
        return Decimal("100.00")
    rs = avg_gain / avg_loss
    rsi_val = Decimal(100) - (Decimal(100) / (Decimal(1) + rs))
    return rsi_val.quantize(Decimal("0.01"))


# ----------------- ATR -----------------


def atr(bars: list[Bar], period: int = 14) -> Decimal | None:
    """Average True Range — Wilder's smoothing.

    Measures recent volatility. Useful for sizing stops.
    """
    if len(bars) < period + 1:
        return None
    trs: list[Decimal] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high = bars[i].high
        low = bars[i].low
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    # Wilder smoothing
    atr_val = sum(trs[:period], Decimal("0")) / Decimal(period)
    for tr in trs[period:]:
        atr_val = (atr_val * Decimal(period - 1) + tr) / Decimal(period)
    return atr_val.quantize(Decimal("0.01"))


# ----------------- volume ratio -----------------


def volume_ratio(bars: list[Bar], lookback: int = 20) -> Decimal | None:
    """Current bar's volume divided by the average of the previous `lookback` bars.

    >1.5 typically signals a volume surge. <0.5 means quiet.
    """
    if len(bars) < lookback + 1:
        return None
    recent = bars[-1].volume
    prev = bars[-(lookback + 1):-1]
    avg = sum(b.volume for b in prev) / lookback
    if avg <= 0:
        return None
    return (Decimal(recent) / Decimal(str(avg))).quantize(Decimal("0.01"))


# ----------------- bundle -----------------


def snapshot(bars: list[Bar]) -> dict:
    """Compute every indicator for the given bar sequence.

    Returns a dict of plain types (Decimal/None) ready to be serialized
    into the LLM prompt.
    """
    if not bars:
        return {"bars": 0}
    last = bars[-1]
    return {
        "bars": len(bars),
        "last_close": str(last.close),
        "last_volume": last.volume,
        "vwap": str(vwap(bars)) if vwap(bars) is not None else None,
        "rsi14": str(rsi(bars, 14)) if rsi(bars, 14) is not None else None,
        "ema20": str(ema(bars, 20)) if ema(bars, 20) is not None else None,
        "ema50": str(ema(bars, 50)) if ema(bars, 50) is not None else None,
        "atr14": str(atr(bars, 14)) if atr(bars, 14) is not None else None,
        "volume_ratio_20": (
            str(volume_ratio(bars, 20))
            if volume_ratio(bars, 20) is not None
            else None
        ),
    }
