"""Weekly-expiry counterfactual on the bot's ACTUAL directional entries.

We have no historical option prices (Alpaca returns 0 OPRA bars on this plan),
so option P&L is MODELED with Black-Scholes from SPY's real price path. The
directional layer (does SPY move the trade's way over N days?) uses only real
SPY data — no modeling.

For each real directional entry (date + call/put from data/trademaster.db):
  1. Directional test: SPY forward return over the hold, in the trade's favor.
  2. Modeled weekly: enter an ATM SPY option DTE_ENTRY days out, hold HOLD
     trading days, exit at Black-Scholes value with reduced DTE. Size at
     BUDGET (default $1000 = 20% of $5k) → floor(BUDGET / premium) contracts.

Assumptions (stated, tunable): fixed IV, r=0. The $ numbers are only as good as
the IV assumption — treat them as a first-order estimate, not a real backtest.

Usage:  uv run python -m scripts.backtest_weekly [IV] [HOLD_DAYS] [BUDGET]
"""
from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime, timezone

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

IV = float(sys.argv[1]) if len(sys.argv) > 1 else 0.14      # annualized implied vol
HOLD = int(sys.argv[2]) if len(sys.argv) > 2 else 3          # trading days held
BUDGET = float(sys.argv[3]) if len(sys.argv) > 3 else 1000.0  # 20% of $5k
DTE_ENTRY = 5  # weekly: enter ~1 trading week to expiry


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, call, sigma=IV):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def main():
    # SPY daily closes by date
    req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Day),
                           start=datetime(2018, 1, 1, tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX, limit=5000)
    bars = ac._stock_client().get_stock_bars(req).data.get("SPY", [])
    dates = [b.timestamp.date() for b in bars]
    close = [float(b.close) for b in bars]
    idx = {d: i for i, d in enumerate(dates)}

    c = sqlite3.connect("data/trademaster.db")
    trades = []
    for oid, oa, ex in c.execute(
        "select id, opened_at, extra from trades where strategy in "
        "('directional_call','directional_put') order by opened_at"
    ):
        import json
        e = json.loads(ex) if ex else {}
        d = datetime.strptime(oa[:10], "%Y-%m-%d").date()
        # map to the nearest trading day <= entry date that exists in SPY series
        i = idx.get(d)
        if i is None:
            # walk back to last available trading day
            cand = [k for k, dd in enumerate(dates) if dd <= d]
            if not cand:
                continue
            i = cand[-1]
        trades.append((oid, i, d, e.get("action"), e.get("conviction")))

    def _dir(rows, h):
        hits = tot = 0
        for _, i, _, act, _ in rows:
            j = i + h
            if j >= len(close):
                continue
            r = close[j] / close[i] - 1.0
            tot += 1
            hits += 1 if (r > 0) == (act == "BUY_CALL") else 0
        return hits, tot

    def _pnl(rows):
        net = 0.0
        per = []
        for _, i, _, act, _ in rows:
            j = i + HOLD
            if j >= len(close):
                continue
            S0, S1 = close[i], close[j]
            call = act == "BUY_CALL"
            prem0 = bs(S0, S0, DTE_ENTRY / 252, call)
            prem1 = bs(S1, S0, max(DTE_ENTRY - HOLD, 0.5) / 252, call)
            qty = max(1, math.floor(BUDGET / (prem0 * 100)))
            per.append(qty * (prem1 - prem0) * 100)
        return per

    def analyze(rows, label):
        print(f"\n=== {label}  ({len(rows)} entries) ===")
        calls = [r for r in rows if r[3] == "BUY_CALL"]
        puts = [r for r in rows if r[3] == "BUY_PUT"]
        for name, sub in (("ALL", rows), ("CALLS", calls), ("PUTS", puts)):
            if not sub:
                continue
            dirs = []
            for h in (1, 2, 3, 5):
                hh, tt = _dir(sub, h)
                dirs.append(f"{h}d {hh/tt*100:.0f}%" if tt else f"{h}d -")
            per = _pnl(sub)
            if per:
                wins = sum(1 for p in per if p > 0)
                money = f"net=${sum(per):+.0f} per-trade=${sum(per)/len(per):+.0f} win={wins/len(per)*100:.0f}%"
            else:
                money = "no $ (insufficient fwd data)"
            tag = "(want SPY up)" if name == "CALLS" else "(want SPY down)" if name == "PUTS" else ""
            print(f"  {name:6} n={len(sub):3} {tag:16} dir-right[{'  '.join(dirs)}]  | modeled {money}")

    all_rows = trades
    recent = [t for t in trades if t[2] >= datetime.strptime("2026-06-01", "%Y-%m-%d").date()]
    newcfg = [t for t in trades if t[2] >= datetime.strptime("2026-06-15", "%Y-%m-%d").date()]
    analyze(all_rows, "ALL directional entries 2025→now")
    analyze(recent, "June 2026 (recent regime)")
    analyze(newcfg, "New-config era (Sonnet+ADX, 06-15→now)")
    print(f"\n(SPY last close ${close[-1]:.2f}; option $ are MODELED, sensitive to IV={IV:.0%}.)")


if __name__ == "__main__":
    main()
