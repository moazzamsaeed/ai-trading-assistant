"""Event-driven backtest of the DEPLOYED deterministic engine (signal_engine.decide).

Walks SPY 15-min RTH bars chronologically and simulates the live trade sequence:
one position at a time, a re-entry cooldown, and a 0DTE same-day exit (fixed hold
or EOD force-close). Entries use the EXACT engine.decide() we're about to ship, so
this is a faithful backtest of the shipped logic. Option P&L is MODELED (Black-
Scholes, intraday time-to-expiry so theta is real, minus the bid/ask spread).

The kill variable is IV: 0DTE options carry a vol-risk-premium (implied > realized),
so results are shown across a realistic IV range. Per-contract P&L plus totals at
$1000/trade (20% of $5k) sizing. Split by conviction.

Usage: uv run python -m scripts.backtest_engine [IV] [SPREAD_RT$] [HOLD_MIN] [COOLDOWN_MIN]
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from agents.directional.signal_engine import decide

ET = ZoneInfo("America/New_York")
TF = 15
# Underlying via env var (keeps the positional IV/spread/... args non-breaking):
#   SYMBOL=QQQ .venv/bin/python -m scripts.backtest_engine
SYMBOL = os.environ.get("SYMBOL", "SPY").upper()
IV = float(sys.argv[1]) if len(sys.argv) > 1 else 0.18
SPREAD = float(sys.argv[2]) if len(sys.argv) > 2 else 0.04
HOLD_MIN = int(sys.argv[3]) if len(sys.argv) > 3 else 60
COOLDOWN_MIN = int(sys.argv[4]) if len(sys.argv) > 4 else 30
# Fill realism: extra exit slippage that SCALES with the move (you chase a fast
# market — hits the convex winners hardest), plus an EOD-exit penalty.
SLIP_COEF = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0   # $/share per 1% underlying move over the hold
EOD_PEN = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0     # $/share extra on EOD-held exits
BUDGET = 1000.0
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


def bs(S, K, T, call):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * IV * IV * T) / (IV * math.sqrt(T))
    d2 = d1 - IV * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


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
    tmin = [(16 - b.timestamp.astimezone(ET).hour) * 60 - b.timestamp.astimezone(ET).minute for b in bars]
    n = len(close)

    vwap = [0.0] * n; cpv = cv = 0.0
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cpv = cv = 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        cpv += tp * vol[i]; cv += vol[i]
        vwap[i] = cpv / cv if cv else close[i]
    em = ema(close, 20)
    adx = adx_series(high, low, close, 14)

    hbars = HOLD_MIN // TF
    cool_bars = COOLDOWN_MIN // TF
    trades = []   # (conviction, call, pnl_per_contract, premium)
    pos = None    # (entry_i, call, S0, prem0, conviction)
    cooldown_until = -1

    for i in range(200, n):
        # ---- manage open position ----
        if pos is not None:
            ei, call, S0, prem0, conv = pos
            held = (i - ei) * TF
            eod = (i + 1 >= n) or (day[i + 1] != day[i])
            if held >= HOLD_MIN or eod or tmin[i] <= TF:
                T1 = max(tmin[i], 1) / YEAR_MIN
                prem1 = bs(close[i], S0, T1, call)
                move_pct = abs(close[i] / S0 - 1) * 100
                exit_slip = SLIP_COEF * move_pct + (EOD_PEN if eod else 0.0)
                pnl = (prem1 - prem0 - SPREAD - exit_slip) * 100
                trades.append((conv, call, pnl, prem0 * 100))
                pos = None
                cooldown_until = i + cool_bars
            continue
        if i < cooldown_until or tmin[i] < HOLD_MIN + 5:
            continue
        # ---- look for entry via the SHIPPED engine ----
        snap = {"last_close": close[i], "vwap": vwap[i], "ema20": em[i], "adx": adx[i], "rsi9": 50}
        d = decide(SYMBOL, snap)
        if d.action in ("BUY_CALL", "BUY_PUT"):
            call = d.action == "BUY_CALL"
            T0 = tmin[i] / YEAR_MIN
            prem0 = bs(close[i], close[i], T0, call)
            pos = (i, call, close[i], prem0, d.conviction)

    def report(rows, label):
        if not rows:
            print(f"  {label}: no trades"); return
        net = sum(p for _, _, p, _ in rows)
        wins = sum(1 for _, _, p, _ in rows if p > 0)
        avgprem = sum(pr for _, _, _, pr in rows) / len(rows)
        contracts = max(1, math.floor(BUDGET / avgprem))
        print(f"  {label:16} n={len(rows):4}  win={wins/len(rows)*100:4.0f}%  "
              f"${net/len(rows):+6.1f}/contract  | @${BUDGET:.0f}/trade(~{contracts}x): "
              f"${net/len(rows)*contracts:+7.0f}/trade  total ${net*contracts:+.0f}")

    print(f"{SYMBOL} engine backtest — IV={IV:.0%}, hold {HOLD_MIN}m, spread ${SPREAD:.2f}, "
          f"cooldown {COOLDOWN_MIN}m, {day[0]}→{day[-1]}\n")
    report(trades, "ALL")
    report([t for t in trades if t[0] == "HIGH"], "HIGH conviction")
    report([t for t in trades if t[0] == "MEDIUM"], "MEDIUM conviction")
    report([t for t in trades if t[1]], "CALLS")
    report([t for t in trades if not t[1]], "PUTS")


if __name__ == "__main__":
    main()
