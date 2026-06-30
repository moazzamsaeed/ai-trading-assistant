"""Rules-only TREND-FOLLOWING 0DTE test on SPY (no LLM, no modeling).

Tests the hypothesis: when SPY is in an established, strong intraday trend, does
it CONTINUE in that direction over the next 30-120 min (the window a 0DTE trade
would hold)? This is trend-FOLLOWING (trade with the trend), the opposite of the
mean-reversion/pullback entries that have failed.

Mechanical signal on `tf`-min RTH bars (default 15):
  CALL  close > VWAP_session  AND  close > EMA(EMA_LEN)  AND  ADX(14) >= ADX_MIN
  PUT   close < VWAP_session  AND  close < EMA(EMA_LEN)  AND  ADX(14) >= ADX_MIN

Forward return is SAME trading day only (0DTE). Directional layer is model-free —
just SPY's own continuation. We compare hit-rate vs the unconditional baseline;
an edge has to clear it by enough to beat 0DTE spread+theta to be real.

Usage:  uv run python -m scripts.backtest_trend_0dte [SYMBOL] [tf] [ADX_MIN] [EMA_LEN]
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
EMA_LEN = int(sys.argv[4]) if len(sys.argv) > 4 else 9


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
    n = len(close)

    # session-anchored VWAP
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

    calls, puts = [], []
    for i in range(200, n):
        if adx[i] is None or adx[i] < ADX_MIN:
            continue
        if close[i] > vwap[i] and close[i] > em[i]:
            calls.append(i)
        elif close[i] < vwap[i] and close[i] < em[i]:
            puts.append(i)

    def fwd(i, h):
        j = i + h
        if j >= n or day[j] != day[i]:
            return None
        return close[j] / close[i] - 1.0

    def stat(idxs, h, up):
        rs = [r for r in (fwd(i, h) for i in idxs) if r is not None]
        if not rs:
            return None
        hits = sum(1 for r in rs if (r > 0) == up)
        favor = [r if up else -r for r in rs]
        return len(rs), hits / len(rs), sum(favor) / len(favor)

    def base(h, up):
        rs = [r for r in (fwd(i, h) for i in range(200, n)) if r is not None]
        hits = sum(1 for r in rs if (r > 0) == up)
        return hits / len(rs), sum((r if up else -r) for r in rs) / len(rs)

    print(f"{SYMBOL} {TF}min RTH  {day[0]} → {day[-1]}  ({n} bars)  TREND-FOLLOW  ADX≥{ADX_MIN:.0f}  EMA{EMA_LEN}")
    print(f"Signals (per in-trend bar):  CALL={len(calls)}  PUT={len(puts)}\n")
    for label, idxs, up in [("CALL (want ↑)", calls, True), ("PUT  (want ↓)", puts, False)]:
        print(f"=== {label} — {len(idxs)} signals ===")
        print(f"{'fwd':>6} | {'n':>5} | {'hit-rate':>9} | {'avg move(favor)':>15} || baseline")
        for h in (2, 4, 8):
            s = stat(idxs, h, up); b_hit, b_avg = base(h, up)
            if s:
                ns, hit, avg = s
                edge = (hit - b_hit) * 100
                print(f"{h*TF:>4}m  | {ns:>5} | {hit*100:>7.1f}% | {avg*100:>13.3f}% || {b_hit*100:.1f}% / {b_avg*100:+.3f}%  (edge {edge:+.1f}pp)")
        print()

    # ── Rule-based CONVICTION test: does the edge scale with trend strength (ADX)?
    # and with distance-from-VWAP? If yes, a mechanical score can size HIGH/MED.
    print("################ CONVICTION STRATIFICATION (60m horizon) ################")
    H = 4  # 60m on 15-min
    buckets = [(25, 30), (30, 40), (40, 50), (50, 999)]
    for label, idxs, up in [("CALL", calls, True), ("PUT", puts, False)]:
        print(f"\n  {label} — by ADX bucket:")
        for lo, hi in buckets:
            sub = [i for i in idxs if lo <= adx[i] < hi]
            s = stat(sub, H, up)
            if s:
                ns, hit, avg = s
                print(f"    ADX {lo:>2}-{hi if hi<999 else '+':<3} n={ns:>5}  hit={hit*100:5.1f}%  avg move(favor)={avg*100:+.4f}%")
        print(f"  {label} — by |close−VWAP| (% of price):")
        dist = lambda i: abs(close[i] - vwap[i]) / close[i] * 100
        dbuckets = [(0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 9)]
        for lo, hi in dbuckets:
            sub = [i for i in idxs if lo <= dist(i) < hi]
            s = stat(sub, H, up)
            if s:
                ns, hit, avg = s
                print(f"    dist {lo:>4}-{hi if hi<9 else '+':<4}% n={ns:>5}  hit={hit*100:5.1f}%  avg move(favor)={avg*100:+.4f}%")


if __name__ == "__main__":
    main()
