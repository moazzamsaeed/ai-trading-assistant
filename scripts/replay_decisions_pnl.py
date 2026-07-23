"""Convert replayed LLM entry decisions (replay_model_comparison output) to P&L.

Two views per replay JSONL:

1. VETO view — at each real-entry point ("kind": "real_entry"), did the model
   agree with the trade the engine actually took? Sums the real P&L of trades
   the model would have taken vs vetoed (HOLD/opposite-side ⇒ veto).

2. COUNTERFACTUAL view — walk the grid points in time order and simulate the
   model as the only trader: on BUY_CALL/BUY_PUT with no open position, buy 1
   ATM 0DTE SPY contract at the decision bar (Black-Scholes, real intraday
   time-to-expiry), exit after HOLD_MIN minutes or at 15:50, minus a round-trip
   spread. One position at a time, long-only — mirrors the engine's shape.
   Pricing is MODELED (same caveats as scripts/model_0dte_pnl.py); IV/spread
   sensitivity is reported.

Usage: .venv/bin/python -m scripts.replay_decisions_pnl data/replays/fable5_*.jsonl
"""
from __future__ import annotations

import glob
import json
import math
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
YEAR_MIN = 365 * 24 * 60
IV = 0.13
SPREAD_RT = 0.04          # $/share round trip (same default as model_0dte_pnl)
HOLD_MIN = 30             # sim hold; real directional trades averaged ~15-35 min
QTY = 5                   # scale per-contract P&L to the real book's typical size
MODEL_KEY = "claude-fable-5"


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, call, sig=IV):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def load_rows(patterns):
    rows = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path) as f:
                rows += [json.loads(l) for l in f if l.strip()]
    rows.sort(key=lambda r: r["t"])
    return rows


def fetch_day_bars(day_et):
    start = datetime.combine(day_et, dtime(9, 30), tzinfo=ET)
    end = datetime.combine(day_et, dtime(16, 0), tzinfo=ET)
    req = StockBarsRequest(symbol_or_symbols="SPY",
                           timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                           start=start.astimezone(UTC), end=end.astimezone(UTC),
                           feed=DataFeed.IEX)
    raw = ac._stock_client().get_stock_bars(req).data.get("SPY", [])
    return {b.timestamp.astimezone(ET).replace(second=0, microsecond=0): float(b.close)
            for b in raw}


def px_at(bars, ts_et):
    """Close at-or-before ts (walk back ≤10 min for sparse IEX minutes)."""
    t = ts_et.replace(second=0, microsecond=0)
    for _ in range(11):
        if t in bars:
            return bars[t]
        t -= timedelta(minutes=1)
    return None


def min_to_close(ts_et):
    close = ts_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return max((close - ts_et).total_seconds() / 60.0, 0.0)


def sim_trade(bars, ts_et, is_call, iv=IV, spread=SPREAD_RT):
    """1-contract ATM 0DTE buy at ts, exit at ts+HOLD_MIN or 15:50."""
    S0 = px_at(bars, ts_et)
    if S0 is None:
        return None
    force = ts_et.replace(hour=15, minute=50, second=0, microsecond=0)
    exit_ts = min(ts_et + timedelta(minutes=HOLD_MIN), force)
    if exit_ts <= ts_et:
        return None
    S1 = px_at(bars, exit_ts)
    if S1 is None:
        return None
    p0 = bs(S0, S0, min_to_close(ts_et) / YEAR_MIN, is_call, iv)
    p1 = bs(S1, S0, max(min_to_close(exit_ts), 1) / YEAR_MIN, is_call, iv)
    return {"entry_prem": p0 * 100, "pnl_1ct": (p1 - p0) * 100 - spread * 100}


def main():
    rows = load_rows(sys.argv[1:] or ["data/replays/fable5_*.jsonl"])
    if not rows:
        sys.exit("no replay rows found")

    # ---- 1. VETO view ------------------------------------------------------
    real = [r for r in rows if r.get("kind") == "real_entry" and MODEL_KEY in r]
    taken = vetoed = 0
    taken_pnl = vetoed_pnl = 0.0
    print(f"=== VETO view — {MODEL_KEY} at {len(real)} real entry points ===")
    for r in real:
        d = r[MODEL_KEY]
        agree = (d.get("action") or "").startswith("BUY") and \
            d["action"].split("_")[1][0].upper() == (r.get("real_action") or "?")[4:5].upper()
        pnl = r.get("real_pnl") or 0.0
        tag = "TAKE" if agree else "VETO"
        if agree:
            taken += 1; taken_pnl += pnl
        else:
            vetoed += 1; vetoed_pnl += pnl
        print(f"  {r['day']} {r['t_et']}  real={r.get('real_action')}({pnl:+.0f})  "
              f"fable={d.get('action')}/{d.get('conviction') or '-'} -> {tag}")
    if real:
        print(f"  → takes {taken} (real P&L {taken_pnl:+.0f})  vetoes {vetoed} "
              f"(avoided {vetoed_pnl:+.0f})")

    # ---- 2. COUNTERFACTUAL view -------------------------------------------
    grid = [r for r in rows if r.get("kind") == "grid" and MODEL_KEY in r]
    print(f"\n=== COUNTERFACTUAL — {MODEL_KEY} trades the grid "
          f"(1 pos at a time, hold {HOLD_MIN}m, IV={IV:.0%}, spread ${SPREAD_RT:.2f}) ===")
    bars_cache: dict = {}
    open_until: datetime | None = None
    sims = []
    for r in grid:
        d = r[MODEL_KEY]
        action = d.get("action") or ""
        if not action.startswith("BUY"):
            continue
        ts = datetime.fromisoformat(r["t"]).astimezone(ET)
        if open_until and ts < open_until:
            continue  # still in a position
        day = ts.date()
        if day not in bars_cache:
            bars_cache[day] = fetch_day_bars(day)
        res = sim_trade(bars_cache[day], ts, is_call=("CALL" in action))
        if res is None:
            continue
        open_until = ts + timedelta(minutes=HOLD_MIN)
        sims.append((r["day"], r["t_et"], action, d.get("conviction"), res))
        print(f"  {r['day']} {r['t_et']}  {action}/{d.get('conviction') or '-'}  "
              f"prem=${res['entry_prem']:.0f}  pnl(1ct)={res['pnl_1ct']:+.0f}")
    if sims:
        pnls = [s[4]["pnl_1ct"] for s in sims]
        n = len(pnls); wins = sum(1 for p in pnls if p > 0)
        tot1 = sum(pnls)
        print(f"  → n={n}  win={wins/n*100:.0f}%  net(1ct)={tot1:+.0f}  "
              f"scaled x{QTY}ct={tot1*QTY:+.0f}")
        # sensitivity
        for iv, sp in [(0.10, 0.04), (0.16, 0.04), (0.13, 0.02), (0.13, 0.06)]:
            tot = 0.0
            for dstr, tet, action, conv, _ in sims:
                ts = datetime.strptime(f"{dstr} {tet[:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
                r2 = sim_trade(bars_cache[ts.date()], ts, "CALL" in action, iv=iv, spread=sp)
                if r2:
                    tot += r2["pnl_1ct"]
            print(f"     sensitivity IV={iv:.0%} spread=${sp:.2f}: net(1ct)={tot:+.0f}")
    else:
        print("  → model never entered on the grid")


if __name__ == "__main__":
    main()
