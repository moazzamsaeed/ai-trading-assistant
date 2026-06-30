"""Backtest a trend-aligned MACD-pullback rule on SPY daily bars.

Rule under test (research):
  CALL  when  close > EMA200  AND  bullish MACD cross (line crosses above signal)
              occurs while MACD line < 0  (i.e. crossover below the zero line)
  PUT   when  close < EMA200  AND  bearish MACD cross (line crosses below signal)
              occurs while MACD line > 0  (i.e. crossover above the zero line)

For each signal we measure SPY's forward return over N trading days. A CALL is a
"hit" if price rose; a PUT is a hit if price fell. We compare hit-rate and mean
forward return against the unconditional baseline over the same window.

MACD = EMA12 - EMA26, signal = EMA9 of MACD (standard 12/26/9, daily).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac


def ema(xs, period):
    a = 2.0 / (period + 1.0)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(a * x + (1 - a) * out[-1])
    return out


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "SPY"
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=datetime(2015, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 6, 15, tzinfo=timezone.utc),
        feed=DataFeed.IEX,
        limit=5000,
    )
    bars = ac._stock_client().get_stock_bars(req).data.get(symbol, [])
    dates = [b.timestamp.date() for b in bars]
    close = [float(b.close) for b in bars]
    n = len(close)

    ema200 = ema(close, 200)
    ema12, ema26 = ema(close, 12), ema(close, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = ema(macd, 9)

    HORIZONS = [1, 3, 5, 10]
    WARMUP = 200  # let EMA200 settle before trusting signals

    calls, puts = [], []  # each: (idx, date)
    for i in range(max(WARMUP, 1), n):
        bull = macd[i - 1] <= signal[i - 1] and macd[i] > signal[i]
        bear = macd[i - 1] >= signal[i - 1] and macd[i] < signal[i]
        if close[i] > ema200[i] and bull and macd[i] < 0:
            calls.append((i, dates[i]))
        if close[i] < ema200[i] and bear and macd[i] > 0:
            puts.append((i, dates[i]))

    def fwd(i, h):
        j = i + h
        return (close[j] / close[i] - 1.0) if j < n else None

    def stats(signals, h, want_up):
        rets = [fwd(i, h) for i, _ in signals]
        rets = [r for r in rets if r is not None]
        if not rets:
            return None
        hits = sum(1 for r in rets if (r > 0) == want_up)
        # signed return in the trade's favor (calls: +ret, puts: -ret)
        favor = [r if want_up else -r for r in rets]
        return len(rets), hits / len(rets), sum(favor) / len(favor), sorted(favor)[len(favor) // 2]

    def baseline(h, want_up):
        rets = [fwd(i, h) for i in range(max(WARMUP, 1), n) if fwd(i, h) is not None]
        hits = sum(1 for r in rets if (r > 0) == want_up)
        favor = [r if want_up else -r for r in rets]
        return len(rets), hits / len(rets), sum(favor) / len(favor)

    print(f"{symbol} daily  {dates[0]} → {dates[-1]}  ({n} bars, {WARMUP} warmup)")
    print(f"Signals found:  CALL={len(calls)}   PUT={len(puts)}\n")

    for label, sig, want_up in [("CALL (expect UP)", calls, True), ("PUT  (expect DOWN)", puts, False)]:
        print(f"=== {label} — {len(sig)} signals ===")
        print(f"{'horizon':>8} | {'n':>4} | {'hit-rate':>9} | {'avg fwd (in favor)':>18} | {'median':>8} || baseline hit / avg")
        for h in HORIZONS:
            s = stats(sig, h, want_up)
            b_n, b_hit, b_avg = baseline(h, want_up)
            if s:
                n_s, hit, avg, med = s
                print(f"{h:>6}d  | {n_s:>4} | {hit*100:>7.1f}% | {avg*100:>16.2f}% | {med*100:>6.2f}% || {b_hit*100:>5.1f}% / {b_avg*100:+.2f}%")
        print()

    # Show the actual signals so it's transparent / auditable
    print("CALL signal dates:", ", ".join(str(d) for _, d in calls))
    print("\nPUT signal dates:", ", ".join(str(d) for _, d in puts))


if __name__ == "__main__":
    main()
