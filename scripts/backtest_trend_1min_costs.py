"""1-MINUTE trend-follow 0DTE backtest WITH realistic option costs (theta + spread).

Unlike backtest_trend_0dte.py (which only measures SPY's directional hit-rate),
this prices the actual 0DTE ATM option via Black-Scholes at entry (pay the ASK)
and exit (receive the BID) after a fixed hold, so the three things that kill a
fast 0DTE buyer are all modeled:

  1. DELTA gain      — the directional edge (what the hit-rate test measured)
  2. THETA decay     — BS reprices at (T - hold), so time-decay is subtracted
  3. BID/ASK SPREAD  — paid on every round trip; the dominant cost at high freq

IV is estimated from TRAILING REALIZED VOL (favorable-to-the-buyer: real 0DTE IV
runs higher than realized due to the vol-risk-premium, so this is a LOWER BOUND
on cost — if buying loses here it loses worse in reality). A --vrp multiplier
lets us layer the realistic VRP markup back on.

Signal (same as the live engine, on 1-min bars):
  CALL  close > VWAP_session AND close > EMA(EMA_LEN) AND ADX(14) >= ADX_MIN
  PUT   close < VWAP_session AND close < EMA(EMA_LEN) AND ADX(14) >= ADX_MIN

Usage: .venv/bin/python -m scripts.backtest_trend_1min_costs [start_YYYY-MM-DD] [spread] [vrp]
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, time as dtime, timezone
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

ET = ZoneInfo("America/New_York")
SYMBOL = "SPY"
ADX_MIN = float(sys.argv[4]) if len(sys.argv) > 4 else 25.0
EMA_LEN = 9
YEAR_MIN = 252 * 390           # trading-minute annualization (matches VIX1D convention)
START = sys.argv[1] if len(sys.argv) > 1 else "2025-01-01"
SPREAD = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03   # full bid/ask, $/share
VRP = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0       # IV = realized * VRP
HOLDS = (5, 15, 30)            # minutes
IV_WIN = 120                   # trailing 1-min bars for realized-vol IV
MIN_TTC = 35                   # don't enter with < this many minutes to close


def is_rth(ts):
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def ema(xs, p):
    a = 2.0 / (p + 1.0); o = [xs[0]]
    for x in xs[1:]:
        o.append(a * x + (1 - a) * o[-1])
    return o


def adx_series(high, low, close, period=14):
    n = len(close); out = [None] * n
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


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(call, S, K, T, sigma):
    """Black-Scholes price, r=0 (negligible for 0DTE). T in years, sigma annual."""
    if T <= 0 or sigma <= 0:
        intr = max(0.0, S - K) if call else max(0.0, K - S)
        return intr
    srt = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / srt
    d2 = d1 - srt
    if call:
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


def sharpe(rs):
    if len(rs) < 2:
        return 0.0
    sd = pstdev(rs)
    return (mean(rs) / sd * math.sqrt(252)) if sd else 0.0


def main():
    print(f"Fetching SPY 1-min bars {START} → 2026-06-18 (IEX)…", flush=True)
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                           start=datetime.fromisoformat(START).replace(tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)
    raw = ac._stock_client().get_stock_bars(req).data.get(SYMBOL, [])
    bars = [b for b in raw if is_rth(b.timestamp)]
    close = [float(b.close) for b in bars]
    high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]
    vol = [float(b.volume) for b in bars]
    ts = [b.timestamp.astimezone(ET) for b in bars]
    day = [t.date() for t in ts]
    n = len(close)
    print(f"{n} 1-min RTH bars  {day[0]} → {day[-1]}\n", flush=True)

    vwap = [0.0] * n; cpv = cv = 0.0
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cpv = cv = 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        cpv += tp * vol[i]; cv += vol[i]
        vwap[i] = cpv / cv if cv else close[i]

    em = ema(close, EMA_LEN)
    adx = adx_series(high, low, close, 14)

    # trailing realized-vol (annualized, trading-minute) → IV proxy
    logret = [0.0] * n
    for i in range(1, n):
        logret[i] = math.log(close[i] / close[i - 1]) if close[i - 1] > 0 else 0.0

    def iv_at(i):
        lo = max(1, i - IV_WIN + 1)
        seg = [logret[k] for k in range(lo, i + 1) if day[k] == day[i]]
        if len(seg) < 10:
            return None
        sd = pstdev(seg)
        return max(0.05, min(1.5, sd * math.sqrt(YEAR_MIN) * VRP))

    def mins_to_close(i):
        close_t = ts[i].replace(hour=16, minute=0, second=0, microsecond=0)
        return (close_t - ts[i]).total_seconds() / 60.0

    sigs = []
    for i in range(200, n):
        if adx[i] is None or adx[i] < ADX_MIN:
            continue
        if close[i] > vwap[i] and close[i] > em[i]:
            sigs.append((i, True))
        elif close[i] < vwap[i] and close[i] < em[i]:
            sigs.append((i, False))

    print(f"signals: {len(sigs)}  (spread=${SPREAD:.2f}/sh  IV=realized×{VRP})\n")
    half = SPREAD / 2.0
    hdr = f"{'hold':>5} | {'n':>5} | {'win%':>5} | {'gross/ct':>9} | {'+theta':>9} | {'NET/ct':>9} | {'tot$':>9} | {'shrp':>5}"
    print(hdr); print("-" * len(hdr))

    for h in HOLDS:
        gross_l, theta_l, net_l, daily = [], [], [], {}
        for (i, up) in sigs:
            M = mins_to_close(i)
            if M < MIN_TTC or (i + h) >= n or day[i + h] != day[i]:
                continue
            sig = iv_at(i)
            if sig is None:
                continue
            S0, S1 = close[i], close[i + h]
            K = round(S0)
            T0, T1 = M / YEAR_MIN, (M - h) / YEAR_MIN
            call = up
            mid0 = bs(call, S0, K, T0, sig)
            mid1 = bs(call, S1, K, T1, sig)
            if mid0 <= 0.01:
                continue
            # gross = delta+gamma move at constant time (no theta); net adds theta+spread
            mid1_notheta = bs(call, S1, K, T0, sig)
            gross = (mid1_notheta - mid0) * 100
            net_pre_spread = (mid1 - mid0) * 100
            net = (max(0.0, mid1 - half) - (mid0 + half)) * 100   # sell bid, buy ask
            gross_l.append(gross); theta_l.append(net_pre_spread); net_l.append(net)
            daily.setdefault(day[i], 0.0)
            daily[day[i]] += net
        if not net_l:
            continue
        win = sum(1 for x in net_l if x > 0) / len(net_l) * 100
        shrp = sharpe(list(daily.values()))
        print(f"{h:>4}m | {len(net_l):>5} | {win:>4.0f}% | {mean(gross_l):>+8.1f} | "
              f"{mean(theta_l):>+8.1f} | {mean(net_l):>+8.1f} | {sum(net_l):>+8.0f} | {shrp:>5.2f}")

    print("\nLegend: gross/ct = delta P&L only (no theta); +theta = after theta; "
          "NET/ct = after theta AND spread, per 1-lot. tot$ = sum over all trades.")
    print(f"NOTE: IV from realized vol (×{VRP}) is a LOWER bound on premium paid "
          "(real 0DTE IV ≳ realized via VRP). Fixed-hold exits = no stop whipsaw "
          "(optimistic). IEX 1-min bars (~2% of volume) are noisy. Costs not modeled: "
          "exchange/reg fees (~$0.03/ct), partial fills, latency.")


if __name__ == "__main__":
    main()
