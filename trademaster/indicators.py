"""Pure-Python technical indicators for the directional intraday agent.

No numpy/pandas dependencies — all bars-in, scalar-out. Inputs are
sequences of `alpaca_client.Bar` objects (oldest-first). Outputs are
Decimals (or None when there's insufficient data).

Indicator choices informed by expert research on intraday 5-min options trading:
- VWAP: primary institutional reference — algos benchmark against it all day
- RSI-9: faster response on 5-min bars than RSI-14 (covers 45 min vs 70 min of history)
- EMA-20/50: trend confirmation; EMA-50 needs 50 bars (~2.5h RTH to become available)
- MACD(6-13-4): momentum divergence signal; 6-13-4 is the intraday-optimised setting
- ATR-10: current volatility — used as entry quality filter and for S/R context
- Volume ratio (20-bar): RVOL; SMB Capital calls it "the single most important variable"
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from integrations.alpaca_client import Bar
from trademaster.timeutils import to_et


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


def adx(bars: list[Bar], period: int = 14) -> Decimal | None:
    """Average Directional Index (Wilder) — trend STRENGTH, 0–100.

    Direction-agnostic: it says how strongly price is trending, not which way.
    Roughly: <20 = choppy/no trend (momentum breakouts fail), >25 = trending.
    Built from +DM/−DM and True Range with Wilder smoothing. Needs ≥ 2×period+1
    bars (warmup bars from the prior session count). Returns None if too few.
    """
    if len(bars) < 2 * period + 1:
        return None

    trs: list[Decimal] = []
    plus_dms: list[Decimal] = []
    minus_dms: list[Decimal] = []
    for i in range(1, len(bars)):
        high, low = bars[i].high, bars[i].low
        prev_high, prev_low, prev_close = bars[i - 1].high, bars[i - 1].low, bars[i - 1].close
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dms.append(up_move if (up_move > down_move and up_move > 0) else Decimal("0"))
        minus_dms.append(down_move if (down_move > up_move and down_move > 0) else Decimal("0"))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    def _wilder(vals: list[Decimal]) -> list[Decimal]:
        # Seed = sum of first `period`, then sm = sm − sm/period + current.
        sm = sum(vals[:period], Decimal("0"))
        out = [sm]
        for v in vals[period:]:
            sm = sm - sm / Decimal(period) + v
            out.append(sm)
        return out

    sm_tr, sm_plus, sm_minus = _wilder(trs), _wilder(plus_dms), _wilder(minus_dms)
    dxs: list[Decimal] = []
    for tr_s, p_s, m_s in zip(sm_tr, sm_plus, sm_minus, strict=True):
        if tr_s == 0:
            dxs.append(Decimal("0"))
            continue
        plus_di = Decimal("100") * p_s / tr_s
        minus_di = Decimal("100") * m_s / tr_s
        denom = plus_di + minus_di
        dxs.append(Decimal("0") if denom == 0 else Decimal("100") * abs(plus_di - minus_di) / denom)

    if len(dxs) < period:
        return None
    adx_val = sum(dxs[:period], Decimal("0")) / Decimal(period)
    for dx in dxs[period:]:
        adx_val = (adx_val * Decimal(period - 1) + dx) / Decimal(period)
    return adx_val.quantize(Decimal("0.01"))


# ----------------- MACD -----------------


def macd(bars: list[Bar], fast: int = 6, slow: int = 13, signal: int = 4) -> tuple[Decimal | None, Decimal | None]:
    """MACD line and signal line using EMA-fast minus EMA-slow.

    Returns (macd_line, signal_line). Both None when insufficient data.
    Default settings 6-13-4 are the intraday-optimised parameters recommended
    for 5-minute charts — faster than the standard 12-26-9.

    Use divergence (price making new highs while MACD makes lower highs) as the
    primary signal, not crossovers (which lag too much on intraday bars).
    """
    if len(bars) < slow + signal:
        return None, None
    macd_line = ema(bars, fast)
    slow_ema = ema(bars, slow)
    if macd_line is None or slow_ema is None:
        return None, None
    macd_val = (macd_line - slow_ema).quantize(Decimal("0.01"))

    # Signal line = EMA of the MACD values over the last `slow+signal` bars.
    # We approximate by computing MACD on each sub-window.
    macd_series: list[Decimal] = []
    for i in range(signal, len(bars) + 1):
        sub = bars[:i] if i >= slow else []
        if len(sub) >= slow:
            f = ema(sub, fast)
            s = ema(sub, slow)
            if f is not None and s is not None:
                macd_series.append(f - s)

    if len(macd_series) < signal:
        return macd_val, None

    # EMA of the last `signal` MACD values as the signal line
    alpha = Decimal(2) / Decimal(signal + 1)
    sig = sum(macd_series[:signal], Decimal("0")) / Decimal(signal)
    for m in macd_series[signal:]:
        sig = (m * alpha) + (sig * (Decimal(1) - alpha))

    return macd_val, sig.quantize(Decimal("0.01"))


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


def snapshot(
    bars: list[Bar],
    *,
    session_start_et: datetime | None = None,
) -> dict:
    """Compute every indicator for the given bar sequence.

    Returns a dict of plain types (Decimal/None) ready to be serialized
    into the LLM prompt.

    `bars` may include prior-session warmup bars so trend indicators (EMA,
    RSI, volume_ratio) have valid values at today's market open. Pass
    `session_start_et` so VWAP is scoped to today's bars only — VWAP across
    sessions is mathematically wrong (institutional algos benchmark against
    same-session VWAP). Other indicators (EMA, RSI, MACD, ATR, vol_ratio_20)
    use the full bar history; combining sessions is fine for rolling smoothers
    and means today's first 5-min bar already has valid trend context.

    `session_start_et` should be today's RTH open in ET (e.g.,
    `datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)`).
    If omitted, VWAP uses all supplied bars (preserves pre-warmup behavior).

    RSI uses period 9 (not 14) — the professional choice for 5-minute intraday bars.
    RSI-14 looks back 70 minutes; RSI-9 covers 45 minutes and captures momentum shifts
    2-3 candles earlier. ATR uses period 10 for the same reason (more responsive).
    """
    if not bars:
        return {"bars": 0}
    last = bars[-1]

    if session_start_et is not None:
        vwap_bars = [b for b in bars if to_et(b.timestamp) >= session_start_et]
    else:
        vwap_bars = bars

    rsi_val = rsi(bars, 9)
    atr_val = atr(bars, 10)
    adx_val = adx(bars, 14)
    macd_val, macd_sig = macd(bars, fast=6, slow=13, signal=4)
    vwap_val = vwap(vwap_bars)
    ema20_val = ema(bars, 20)
    ema50_val = ema(bars, 50)
    vol_ratio_val = volume_ratio(bars, 20)

    return {
        "bars": len(bars),
        "last_close": str(last.close),
        "last_volume": last.volume,
        "vwap": str(vwap_val) if vwap_val is not None else None,
        "rsi9": str(rsi_val) if rsi_val is not None else None,
        "ema20": str(ema20_val) if ema20_val is not None else None,
        "ema50": str(ema50_val) if ema50_val is not None else None,
        "atr10": str(atr_val) if atr_val is not None else None,
        "adx": str(adx_val) if adx_val is not None else None,
        "macd": str(macd_val) if macd_val is not None else None,
        "macd_signal": str(macd_sig) if macd_sig is not None else None,
        "volume_ratio_20": str(vol_ratio_val) if vol_ratio_val is not None else None,
    }
