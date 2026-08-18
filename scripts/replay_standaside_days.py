"""What the condor WOULD have returned on the two days the ADX trend filter made it
stand aside (2026-08-11, 2026-08-12). Replicates the engine EXACTLY: strikes at
0.5x VIX1D expected move, $5 wings, 1.5x-credit intraday stop, BS-modeled credit,
settled on the REAL SPY intraday path/close. 28 contracts (the live size).
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W, STOP = 0.5, 5.0, 1.5           # winning condor config (= condor_vs_actual.py)
LEG_SPREAD, N_CROSS = 0.04, 6
COST = N_CROSS * (LEG_SPREAD / 2)    # round-trip 4-leg crossing cost, per share
CONTRACTS = 28

# prior_adx / VIX1D the live engine logged at 10:00 ET each day (ground truth).
DAYS = {
    "2026-08-11": {"vix1d": 0.165, "prior_adx": 30.0},
    "2026-08-12": {"vix1d": 0.164, "prior_adx": 28.3},
}


def condor_mark(spot, Kp, Kpl, Kc, Kcl, T, sig):
    return (bs(spot, Kp, T, False, sig) - bs(spot, Kpl, T, False, sig)
            + bs(spot, Kc, T, True, sig) - bs(spot, Kcl, T, True, sig))


def rth_bars(day: str):
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 13, 55, tzinfo=timezone.utc)   # ~09:55 ET
    end = datetime(d.year, d.month, d.day, 20, 5, tzinfo=timezone.utc)      # ~16:05 ET
    cl = ac._stock_client()
    bars = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start, end=end, feed=DataFeed.IEX)).data.get("SPY", [])
    return [(b.timestamp.astimezone(ET), float(b.close)) for b in bars]


def simulate(day, cfg):
    bars = rth_bars(day)
    if not bars:
        return None
    # entry at first bar >= 10:00 ET
    entry = next(((t, c) for t, c in bars if t.hour > 10 or (t.hour == 10 and t.minute >= 0)), bars[0])
    t_entry, spot = entry
    sig = cfg["vix1d"]
    # year-fraction from entry to 16:00 ET close (bs() expects T in YEARS)
    close_dt = t_entry.replace(hour=16, minute=0, second=0, microsecond=0)
    T0 = max((close_dt - t_entry).total_seconds() / 60.0, 1.0) / YEAR_MIN

    em = spot * sig * math.sqrt(T0)
    Kp = round(spot - K * em); Kc = round(spot + K * em)
    Kpl = Kp - W; Kcl = Kc + W
    credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig) - COST
    risk = W - credit

    # walk the intraday path from entry; 1.5x-credit stop, else settle at close
    stop_level = credit + STOP * credit
    path = [(t, c) for t, c in bars if t >= t_entry]
    stopped_at = None; pnl_share = None
    for t, s in path:
        rem = max((close_dt - t).total_seconds() / 60.0, 0.5) / YEAR_MIN
        mark = condor_mark(s, Kp, Kpl, Kc, Kcl, rem, sig)
        if mark >= stop_level:
            pnl_share = credit - mark - COST
            stopped_at = (t, s, mark)
            break
    close_px = path[-1][1]
    if pnl_share is None:  # never stopped → expiry settlement
        put_itm = min(max(Kp - close_px, 0.0), W)
        call_itm = min(max(close_px - Kc, 0.0), W)
        debit = put_itm + call_itm
        pnl_share = credit - debit - COST

    lo = min(c for _, c in path); hi = max(c for _, c in path)
    return {
        "day": day, "spot": spot, "close": close_px, "lo": lo, "hi": hi,
        "Kp": Kp, "Kc": Kc, "Kpl": Kpl, "Kcl": Kcl, "em": em,
        "credit": credit, "risk": risk, "stopped": stopped_at,
        "pnl_share": pnl_share, "pnl_usd": pnl_share * 100 * CONTRACTS,
        "prior_adx": cfg["prior_adx"],
    }


# Real condor days this month → calibrate the BS model's credit to actual fills.
# vix1d logged by the engine; real_credit_sh = DB entry_price(/contract) / 100.
REAL_DAYS = {
    "2026-08-03": {"vix1d": 0.141, "real_credit_sh": 0.28},
    "2026-08-04": {"vix1d": 0.175, "real_credit_sh": 0.4657},
    "2026-08-05": {"vix1d": 0.301, "real_credit_sh": 0.54},
    "2026-08-06": {"vix1d": 0.198, "real_credit_sh": 0.49},
    "2026-08-07": {"vix1d": 0.197, "real_credit_sh": 0.48},
    "2026-08-10": {"vix1d": 0.137, "real_credit_sh": 0.25},
}


def calibration_factor():
    """Mean ratio of real fill credit to BS-modeled credit across the real days."""
    ratios = []
    for day, cfg in REAL_DAYS.items():
        r = simulate(day, {"vix1d": cfg["vix1d"], "prior_adx": 0})
        if r and r["credit"] > 0:
            ratios.append(cfg["real_credit_sh"] / r["credit"])
    return sum(ratios) / len(ratios) if ratios else 1.0


def main():
    fac = calibration_factor()
    print(f"Condor replay on the 2 stand-aside days  ({CONTRACTS} ct, cost {COST:.2f}/sh)")
    print(f"BS→real credit calibration factor: {fac:.2f}  (model overstates credit ~{1/fac:.1f}x)\n")
    total = 0.0
    for day, cfg in DAYS.items():
        r = simulate(day, cfg)
        if not r:
            print(f"{day}: no bars"); continue
        breach = "BREACHED short" if (r["lo"] < r["Kp"] or r["hi"] > r["Kc"]) else "stayed inside"
        # rescale credit to the empirical fill level, recompute settlement P&L
        cal_credit = r["credit"] * fac
        put_itm = min(max(r["Kp"] - r["close"], 0.0), W)
        call_itm = min(max(r["close"] - r["Kc"], 0.0), W)
        cal_pnl_sh = cal_credit - (put_itm + call_itm) - COST
        cal_usd = cal_pnl_sh * 100 * CONTRACTS
        print(f"=== {day}  (prior_adx {r['prior_adx']}, filter said STAND ASIDE) ===")
        print(f"  entry spot {r['spot']:.2f}  EM ${r['em']:.2f}  short {r['Kp']}/{r['Kc']}  wings {r['Kpl']}/{r['Kcl']}")
        print(f"  intraday {r['lo']:.2f}–{r['hi']:.2f}  close {r['close']:.2f}  → {breach}")
        print(f"  credit: model ${r['credit']:.2f} → calibrated ${cal_credit:.2f}/sh")
        print(f"  settlement debit ${put_itm+call_itm:.2f}/sh")
        print(f"  calibrated P&L: ${cal_pnl_sh:+.2f}/sh  →  ${cal_usd:+,.0f}  ({CONTRACTS} ct)\n")
        total += cal_usd
    print(f"TOTAL over the 2 skipped days (calibrated): ${total:+,.0f}")
    print(f"→ the ADX filter {'COST us' if total > 0 else 'SAVED us'} ~${abs(total):,.0f} on n=2 activations")


if __name__ == "__main__":
    main()
