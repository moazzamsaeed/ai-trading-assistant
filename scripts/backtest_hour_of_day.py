"""Hour-of-day validation for SPY 0DTE trend signals (H6).

Live data (n~25/tier) suggested the 12:00–13:00 ET window is structurally weak
for the directional engine — but small-n and outlier-driven. This tests the same
mechanical trend-follow signal as backtest_trend_0dte across 2023→2026 and buckets
each signal's forward outcome by ENTRY HOUR (ET), so we can see whether a midday
weakness holds out-of-sample at thousands of signals.

Model-free: measures SPY's own continuation (favorable move in the trade's
direction) over a 60-min 0DTE hold — NOT option $ (theta/spread excluded). The
question here is purely "is the directional thesis worse at this hour?".

Signal (same as backtest_trend_0dte, 15-min RTH bars):
  CALL  close > VWAP_session AND close > EMA(9) AND ADX(14) >= 25
  PUT   close < VWAP_session AND close < EMA(9) AND ADX(14) >= 25

Usage:  uv run python -m scripts.backtest_hour_of_day [SYMBOL] [tf] [ADX_MIN] [HOLD_BARS]
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
SYMBOL = (sys.argv[1] if len(sys.argv) > 1 else "SPY").upper()
TF = int(sys.argv[2]) if len(sys.argv) > 2 else 15
ADX_MIN = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
HOLD_BARS = int(sys.argv[4]) if len(sys.argv) > 4 else 4  # 4×15m = 60m hold
EMA_LEN = 9


def is_rth(ts):
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def ema(xs, p):
    a = 2.0 / (p + 1.0)
    o = [xs[0]]
    for x in xs[1:]:
        o.append(a * x + (1 - a) * o[-1])
    return o


def adx_series(high, low, close, period=14):
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

    def wil(v):
        s = sum(v[:period]); r = [s]
        for x in v[period:]:
            s = s - s / period + x; r.append(s)
        return r

    st, sp, sm = wil(tr), wil(pdm), wil(mdm)
    dxs = []
    for t, p, m in zip(st, sp, sm):
        den = (100 * p / t) + (100 * m / t) if t else 0.0
        dxs.append(0.0 if not den else 100 * abs((100 * p / t) - (100 * m / t)) / den)
    if len(dxs) < period:
        return out
    a = sum(dxs[:period]) / period; seq = [a]
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period; seq.append(a)
    base = 2 * period - 1
    for m, v in enumerate(seq):
        if base + m < n:
            out[base + m] = v
    return out


def main():
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
                           start=datetime(2023, 1, 1, tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)
    raw = ac._stock_client().get_stock_bars(req).data.get(SYMBOL, [])
    bars = [b for b in raw if is_rth(b.timestamp)]
    close = [float(b.close) for b in bars]
    high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]
    vol = [float(b.volume) for b in bars]
    day = [b.timestamp.astimezone(ET).date() for b in bars]
    hour = [b.timestamp.astimezone(ET).hour for b in bars]
    n = len(close)

    vwap = [0.0] * n
    cpv = cv = 0.0
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cpv = cv = 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        cpv += tp * vol[i]; cv += vol[i]
        vwap[i] = cpv / cv if cv else close[i]

    em = ema(close, EMA_LEN)
    adx = adx_series(high, low, close, 14)

    # signals: (bar index, is_call)
    signals = []
    for i in range(200, n):
        if adx[i] is None or adx[i] < ADX_MIN:
            continue
        if close[i] > vwap[i] and close[i] > em[i]:
            signals.append((i, True))
        elif close[i] < vwap[i] and close[i] < em[i]:
            signals.append((i, False))

    def fwd_favor(i, up):
        """Favorable move (% in the trade's direction) over HOLD_BARS, same day."""
        j = i + HOLD_BARS
        if j >= n or day[j] != day[i]:
            return None
        r = close[j] / close[i] - 1.0
        return r if up else -r

    # bucket by entry hour
    by_hour: dict[int, list[float]] = {}
    for i, up in signals:
        f = fwd_favor(i, up)
        if f is not None:
            by_hour.setdefault(hour[i], []).append(f)

    # baseline: unconditional favorable-up move by hour (any bar), to compare
    def baseline_hour(h):
        rs = []
        for i in range(200, n):
            if hour[i] != h:
                continue
            j = i + HOLD_BARS
            if j >= n or day[j] != day[i]:
                continue
            rs.append(close[j] / close[i] - 1.0)  # raw up-move
        return (sum(rs) / len(rs) * 100) if rs else 0.0

    hold_min = HOLD_BARS * TF
    print(f"{SYMBOL} {TF}min RTH  {day[0]} → {day[-1]}  ({n} bars)  "
          f"TREND-FOLLOW ADX≥{ADX_MIN:.0f} EMA{EMA_LEN}  hold {hold_min}m  (model-free favorable move)")
    print(f"Total signals with a {hold_min}m same-day forward: "
          f"{sum(len(v) for v in by_hour.values())}\n")
    print(f"{'hour ET':>8} | {'n':>5} | {'hit-rate':>9} | {'avg favorable move':>18} | {'baseline up-move':>16}")
    print("-" * 70)
    for h in sorted(by_hour):
        fs = by_hour[h]
        hit = sum(1 for f in fs if f > 0) / len(fs) * 100
        avg = sum(fs) / len(fs) * 100
        flag = "  ← weak" if avg < 0 else ""
        print(f"{h:>5}ET  | {len(fs):>5} | {hit:>7.1f}% | {avg:>16.4f}% | "
              f"{baseline_hour(h):>14.4f}%{flag}")
    print("-" * 70)
    allf = [f for fs in by_hour.values() for f in fs]
    print(f"{'ALL':>8} | {len(allf):>5} | "
          f"{sum(1 for f in allf if f > 0) / len(allf) * 100:>7.1f}% | "
          f"{sum(allf) / len(allf) * 100:>16.4f}% |")
    print("\nRead: a NEGATIVE avg favorable move at an hour = trend signals entered "
          "then tend to go AGAINST the trade over the next "
          f"{hold_min}m — a structurally weak entry hour. Compare across hours; "
          "midday (12–13 ET) is the live-data suspect.")


if __name__ == "__main__":
    main()
