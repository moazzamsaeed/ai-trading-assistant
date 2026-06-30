"""Model 0DTE option P&L on SPY trend-following SWEET-SPOT signals.

Settles the cost question: the trend-follow signal has a real but tiny edge —
does it survive 0DTE option spread + theta? We model each signal as buying an
ATM 0DTE SPY option at the signal bar and selling HOLD_MIN later, priced by
Black-Scholes with REAL intraday time-to-expiry (so theta is correctly brutal),
minus a round-trip bid/ask spread.

Signals (15-min RTH SPY): trend-follow CALL = close>VWAP & close>EMA9 & ADX>=25.
SWEET-SPOT = the best conviction buckets found earlier: ADX in [30,40) AND
|close-VWAP| in [0.1, 0.25]% of price.

Everything except the option pricing is real SPY data. The pricing is MODELED;
results depend on IV and the assumed spread — both are reported with sensitivity.

Usage: uv run python -m scripts.model_0dte_pnl [IV] [SPREAD_RT$] [HOLD_MIN]
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

ET = ZoneInfo("America/New_York")
TF = 15
IV = float(sys.argv[1]) if len(sys.argv) > 1 else 0.13
SPREAD_RT = float(sys.argv[2]) if len(sys.argv) > 2 else 0.04   # round-trip $/share
HOLD_MIN = int(sys.argv[3]) if len(sys.argv) > 3 else 60
YEAR_MIN = 365 * 24 * 60


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
    b = 2 * period - 1
    for m, v in enumerate(seq):
        if b + m < n:
            out[b + m] = v
    return out


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, call, sig=IV):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def main():
    req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
                           start=datetime(2023, 1, 1, tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)
    raw = ac._stock_client().get_stock_bars(req).data.get("SPY", [])
    bars = [b for b in raw if is_rth(b.timestamp)]
    close = [float(b.close) for b in bars]
    high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]
    vol = [float(b.volume) for b in bars]
    day = [b.timestamp.astimezone(ET).date() for b in bars]
    tmin = [(16 - b.timestamp.astimezone(ET).hour) * 60 - b.timestamp.astimezone(ET).minute for b in bars]  # min to 16:00
    n = len(close)

    vwap = [0.0] * n; cpv = cv = 0.0
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cpv = cv = 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        cpv += tp * vol[i]; cv += vol[i]
        vwap[i] = cpv / cv if cv else close[i]
    em = ema(close, 9)
    adx = adx_series(high, low, close, 14)
    hbars = HOLD_MIN // TF

    def signals(sweet):
        out = []
        for i in range(200, n):
            if adx[i] is None or adx[i] < 25:
                continue
            if not (close[i] > vwap[i] and close[i] > em[i]):   # CALL trend only (the edge side)
                continue
            if tmin[i] < HOLD_MIN + 5:           # need time to hold before close
                continue
            if sweet:
                d = abs(close[i] - vwap[i]) / close[i] * 100
                if not (30 <= adx[i] < 40 and 0.1 <= d <= 0.25):
                    continue
            out.append(i)
        return out

    def model(idxs, label):
        gross = net = 0.0
        wins = 0; trades = 0; prem_sum = 0.0
        for i in idxs:
            j = i + hbars
            if j >= n or day[j] != day[i]:
                continue
            S0, S1 = close[i], close[j]
            T0 = tmin[i] / YEAR_MIN
            T1 = max(tmin[j], 1) / YEAR_MIN
            p0 = bs(S0, S0, T0, True)
            p1 = bs(S1, S0, T1, True)
            g = (p1 - p0) * 100
            net_pnl = g - SPREAD_RT * 100
            gross += g; net += net_pnl; prem_sum += p0 * 100
            wins += 1 if net_pnl > 0 else 0; trades += 1
        if not trades:
            print(f"  {label}: no trades"); return
        print(f"  {label}:  n={trades}  avg premium=${prem_sum/trades:.0f}/contract")
        print(f"     GROSS (no spread): ${gross/trades:+.1f}/trade   net total ${gross:+.0f}")
        print(f"     NET   (−${SPREAD_RT:.2f} spread): ${net/trades:+.1f}/trade   win={wins/trades*100:.0f}%   total ${net:+.0f}")

    print(f"SPY 0DTE CALL model — IV={IV:.0%}, hold {HOLD_MIN}m, spread ${SPREAD_RT:.2f} RT, "
          f"{day[0]}→{day[-1]}\n")
    model(signals(False), "ALL trend calls")
    model(signals(True), "SWEET-SPOT (ADX 30-40 & VWAP-dist 0.1-0.25%)")
    print("\n  Sensitivity — SWEET-SPOT net $/trade at other spreads:")
    ss = signals(True)
    for sr in (0.0, 0.02, 0.04, 0.06):
        tot = 0.0; tr = 0
        for i in ss:
            j = i + hbars
            if j >= n or day[j] != day[i]:
                continue
            p0 = bs(close[i], close[i], tmin[i] / YEAR_MIN, True)
            p1 = bs(close[j], close[i], max(tmin[j], 1) / YEAR_MIN, True)
            tot += (p1 - p0) * 100 - sr * 100; tr += 1
        print(f"     spread ${sr:.2f}: ${tot/tr:+.1f}/trade  (total ${tot:+.0f}, n={tr})")


if __name__ == "__main__":
    main()
