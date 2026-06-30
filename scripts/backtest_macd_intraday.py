"""Intraday version of the trend-aligned MACD-pullback backtest.

Same rule as backtest_macd_200ema.py but on intraday bars (default 15-min),
RTH-only, with SAME-DAY forward returns at intraday horizons — so it tests the
rule the way TradeMaster would actually use it (intraday, not multi-day).

  CALL  close > EMA200  AND  bullish MACD cross while MACD < 0  (below zero)
  PUT   close < EMA200  AND  bearish MACD cross while MACD > 0  (above zero)

200-EMA on 15-min ≈ 200 bars ≈ ~7.7 RTH days (a ~1.5-week trend filter).
Forward horizons are in BARS; a forward window is only counted if it stays
within the same trading day (no overnight gaps).

Usage:  uv run python -m scripts.backtest_macd_intraday NVDA 15
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

ET = ZoneInfo("America/New_York")


def is_rth(ts):
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def ema(xs, period):
    a = 2.0 / (period + 1.0)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(a * x + (1 - a) * out[-1])
    return out


def adx_series(high, low, close, period=14):
    """Per-bar Wilder ADX(14), mirroring trademaster.indicators.adx (the gate the
    bot uses). Returns a list aligned to bar index; None before warmup."""
    n = len(close)
    out = [None] * n
    if n < 2 * period + 1:
        return out
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        up, dn = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))

    def wilder(v):
        sm = sum(v[:period])
        res = [sm]
        for x in v[period:]:
            sm = sm - sm / period + x
            res.append(sm)
        return res

    str_, sp, sm_ = wilder(tr), wilder(pdm), wilder(mdm)
    dxs = []
    for t, p, m in zip(str_, sp, sm_):
        den = (100 * p / t) + (100 * m / t) if t else 0.0
        dxs.append(0.0 if not den else 100 * abs((100 * p / t) - (100 * m / t)) / den)
    if len(dxs) < period:
        return out
    adxv = sum(dxs[:period]) / period
    seq = [adxv]
    for dx in dxs[period:]:
        adxv = (adxv * (period - 1) + dx) / period
        seq.append(adxv)
    base = 2 * period - 1  # first ADX value lands at bar index 2*period-1
    for m, val in enumerate(seq):
        if base + m < n:
            out[base + m] = val
    return out


def main():
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()
    tf = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(tf, TimeFrameUnit.Minute),
        start=datetime(2023, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 15, tzinfo=timezone.utc),
        feed=DataFeed.IEX,  # auto-paginates the full range (no limit)
    )
    raw = ac._stock_client().get_stock_bars(req).data.get(symbol, [])
    bars = [b for b in raw if is_rth(b.timestamp)]  # RTH only
    close = [float(b.close) for b in bars]
    high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]
    day = [b.timestamp.astimezone(ET).date() for b in bars]
    n = len(close)

    ema200 = ema(close, 200)
    ema12, ema26 = ema(close, 12), ema(close, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = ema(macd, 9)
    adx = adx_series(high, low, close, 14)

    ADX_MIN = 25.0  # only take signals when the trend is strong (matches the bot's gate)
    HORIZONS = [1, 2, 4, 8]  # bars: 15m, 30m, 1h, 2h on a 15-min chart
    WARMUP = 200

    calls, puts = [], []
    raw_calls = raw_puts = 0
    for i in range(WARMUP, n):
        bull = macd[i - 1] <= signal[i - 1] and macd[i] > signal[i]
        bear = macd[i - 1] >= signal[i - 1] and macd[i] < signal[i]
        strong = adx[i] is not None and adx[i] >= ADX_MIN
        if close[i] > ema200[i] and bull and macd[i] < 0:
            raw_calls += 1
            if strong:
                calls.append(i)
        if close[i] < ema200[i] and bear and macd[i] > 0:
            raw_puts += 1
            if strong:
                puts.append(i)

    def fwd(i, h):
        j = i + h
        if j >= n or day[j] != day[i]:  # same trading day only
            return None
        return close[j] / close[i] - 1.0

    def stats(idxs, h, want_up):
        rets = [r for r in (fwd(i, h) for i in idxs) if r is not None]
        if not rets:
            return None
        hits = sum(1 for r in rets if (r > 0) == want_up)
        favor = [r if want_up else -r for r in rets]
        return len(rets), hits / len(rets), sum(favor) / len(favor)

    def baseline(h, want_up):
        rets = [r for r in (fwd(i, h) for i in range(WARMUP, n)) if r is not None]
        hits = sum(1 for r in rets if (r > 0) == want_up)
        return hits / len(rets), sum((r if want_up else -r) for r in rets) / len(rets)

    print(f"{symbol} {tf}min RTH  {day[0]} → {day[-1]}  ({n} bars)  [ADX≥{ADX_MIN:.0f} filter]")
    print(f"Signals:  CALL={len(calls)}/{raw_calls} survived ADX filter   "
          f"PUT={len(puts)}/{raw_puts} survived\n")
    for label, sig, want_up in [("CALL (expect UP)", calls, True), ("PUT  (expect DOWN)", puts, False)]:
        print(f"=== {label} — {len(sig)} signals ===")
        print(f"{'horizon':>8} | {'n':>4} | {'hit-rate':>9} | {'avg fwd (favor)':>15} || baseline hit / avg")
        for h in HORIZONS:
            s = stats(sig, h, want_up)
            b_hit, b_avg = baseline(h, want_up)
            mins = h * tf
            if s:
                n_s, hit, avg = s
                print(f"{mins:>5}m   | {n_s:>4} | {hit*100:>7.1f}% | {avg*100:>13.3f}% || {b_hit*100:>5.1f}% / {b_avg*100:+.3f}%")
        print()


if __name__ == "__main__":
    main()
