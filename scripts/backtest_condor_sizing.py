"""1- vs 2-contract condor sizing over the validated history.

Defined-risk P&L is LINEAR in contracts, so the EDGE (win rate, Sharpe, DSR) is
identical at any size — already validated in backtest_condor_vix_gate.py (VIX1D<35,
DSR 99%, OOS Sharpe +2.54). The ONLY things that differ between 1 and 2 contracts
are absolute return and DRAWDOWN. So this doesn't re-litigate the edge; it answers
the real sizing question: would the doubled drawdown have breached the daily (15%)
or weekly (25%) loss limits?

Runs the DEPLOYED condor config (short strikes at spot ∓ 0.5×VIX1D-move, $5 wings,
1.5× stop, gate VIX1D<35) over 2023→2026, every gated day (not walk-forward folds —
this is the dollar/drawdown picture of what the live engine actually trades), and
reports per size: total P&L, max drawdown, worst trade / day / week, and limit breaches.

Usage: .venv/bin/python -m scripts.backtest_condor_sizing [CAPITAL] [SIZES e.g. 1,2,3]
"""
from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

# backtest_strangle / backtest_condor_vix_gate parse sys.argv at IMPORT time, so
# shield argv (our "1,2,3" arg would crash their int() parse) while importing.
_argv = sys.argv
sys.argv = [_argv[0]]
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN  # noqa: E402
from scripts.backtest_condor_vix_gate import condor_mark, COST  # noqa: E402
sys.argv = _argv

ET = ZoneInfo("America/New_York")
CAPITAL = float(sys.argv[1]) if len(sys.argv) > 1 else 10_000.0
SIZES = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["1", "2"])]
DAILY_LIMIT = CAPITAL * 0.15
WEEKLY_LIMIT = CAPITAL * 0.25
# Deployed condor config (condor_engine v2): k=0.5 EM short strikes, $5 wings, 1.5× stop.
CFG = {"k": 0.5, "W": 5.0, "stop": 1.5}


def sim_dollars(days, byday, vix, gate):
    """Per-trade P&L in $/contract for the deployed config (not normalized)."""
    out = []
    for d in days:
        if not gate(d):
            continue
        sig = vix[d]
        spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0)
        if em <= 0:
            continue
        Kp = round(spot - CFG["k"] * em); Kpl = Kp - CFG["W"]
        Kc = round(spot + CFG["k"] * em); Kcl = Kc + CFG["W"]
        credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig)
        risk = CFG["W"] - credit
        if credit <= 0.05 or risk <= 0.05:
            continue
        sl = credit + CFG["stop"] * credit; stopped = False
        for sp, m2c in byday[d]["path"][1:]:
            mark = condor_mark(sp, Kp, Kpl, Kc, Kcl, max(m2c, 0) / YEAR_MIN, sig)
            if mark >= sl:
                pnl = credit - mark - COST; stopped = True; break
        if not stopped:
            put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
            call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
            pnl = credit - put_s - call_s - COST
        out.append((d, pnl * 100.0))  # $/contract (per-share × 100)
    return out


def _max_drawdown(seq):
    peak = cum = mdd = 0.0
    for x in seq:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def _load():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try:
            vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception:
            pass
    cl = ac._stock_client(); end = datetime(2026, 6, 18, tzinfo=timezone.utc)
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=datetime(2023, 1, 1, tzinfo=timezone.utc), end=end, feed=DataFeed.IEX
    )).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": []})
        rec["close"] = float(b.close); m2c = (16 - et.hour) * 60 - et.minute
        if rec["entry"] is None and et.hour == 10 and et.minute == 0:
            rec["entry"] = (float(b.close), m2c)
        if rec["entry"] is not None:
            rec["path"].append((float(b.close), max(m2c, 0)))
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix)
    return days, byday, vix


def main():
    days, byday, vix = _load()
    trades = sim_dollars(days, byday, vix, lambda d: vix[d] * 100 < 35)
    n = len(trades)
    if not n:
        print("no trades"); return
    per_ct = [p for _, p in trades]
    wins = sum(1 for p in per_ct if p > 0)
    yrs = (trades[-1][0] - trades[0][0]).days / 365.25

    print(f"Deployed condor (VIX1D<35, $5 wings, 1.5× stop)  {trades[0][0]} → {trades[-1][0]}")
    print(f"{n} trades over {yrs:.1f}y · win {wins/n*100:.0f}% · "
          f"avg ${sum(per_ct)/n:+.0f}/ct · capital ${CAPITAL:,.0f} "
          f"(daily limit ${DAILY_LIMIT:,.0f} / weekly ${WEEKLY_LIMIT:,.0f})\n")

    print(f"{'size':>4} {'total$':>9} {'%ofcap/yr':>10} {'maxDD$':>9} {'worstTrade':>11} "
          f"{'worstWk$':>9} {'day>15%':>8} {'wk>25%':>7}")
    for size in SIZES:
        scaled = [(d, p * size) for d, p in trades]
        sc = [p for _, p in scaled]
        total = sum(sc)
        mdd = _max_drawdown(sc)
        worst_trade = min(sc)
        byweek = defaultdict(float)
        byday_pnl = defaultdict(float)
        for d, p in scaled:
            byweek[(d.isocalendar().year, d.isocalendar().week)] += p
            byday_pnl[d] += p
        worst_wk = min(byweek.values())
        day_breaches = sum(1 for v in byday_pnl.values() if -v >= DAILY_LIMIT)
        wk_breaches = sum(1 for v in byweek.values() if -v >= WEEKLY_LIMIT)
        pct_yr = (total / CAPITAL * 100) / max(yrs, 0.1)
        print(f"{size:>4} {total:>+9.0f} {pct_yr:>+9.1f}% {mdd:>+9.0f} {worst_trade:>+11.0f} "
              f"{worst_wk:>+9.0f} {day_breaches:>8} {wk_breaches:>7}")

    print("\nEdge (win%, Sharpe, DSR) is IDENTICAL at every size — defined-risk P&L is")
    print("linear. The decision is purely whether the doubled drawdown stays inside the")
    print("loss limits: 'day>15%' / 'wk>25%' count historical breaches that would HALT")
    print("trading (and break the linear scaling). 0 = the size never tripped a limit.")


if __name__ == "__main__":
    main()
