"""How would the $25k / 28-contract condor have survived the 2025 turbulence?

Runs the LIVE config (gate VIX1D<35 & prior-ADX<27, strikes 0.5xEM, $5 wings,
1.5x-credit intraday stop, 15:45 settle) isolated to stress vs calm windows.
Reports normalized R (P&L / defined-risk-per-trade; -1.0 = a full max-loss day,
calibration-independent) PLUS a realistic-dollar translation using real fill
economics (avg credit ~$35/ct, real max loss ~$452/ct -> $12,656 on 28ct).

Includes the overnight assignment gap (real next-day open) so tail days aren't
understated. Usage: .venv/bin/python -m scripts.backtest_condor_2025_stress
"""
from __future__ import annotations
import csv, math, statistics
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0
ADX_MAX, VMAX = 27.0, 35.0
STOP = 1.5
LEG_SPREAD = 0.04
COST = 6 * (LEG_SPREAD / 2)
CONTRACTS = 28
# real-economics translation (from live fills #142-#154): avg credit ~$0.35/sh,
# real max loss ~$4.52/sh -> per-contract $452, worst day on 28ct ~ -$12,656.
REAL_MAXLOSS_CT = 452.0
REAL_CREDIT_CT = 35.0


def condor_mark(spot, Kp, Kpl, Kc, Kcl, T, sig):
    return (bs(spot, Kp, T, False, sig) - bs(spot, Kpl, T, False, sig)
            + bs(spot, Kc, T, True, sig) - bs(spot, Kcl, T, True, sig))


def load():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass
    cl = ac._stock_client(); end = datetime(2026, 6, 18, tzinfo=timezone.utc)
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": [], "open": None})
        c = float(b.close); m2c = (16 - et.hour) * 60 - et.minute
        if rec["open"] is None: rec["open"] = c
        rec["close"] = c
        if rec["entry"] is None and et.hour == 10 and et.minute == 0: rec["entry"] = (c, m2c)
        if rec["entry"] is not None: rec["path"].append((c, max(m2c, 0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=datetime(2023, 1, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl = [x["d"] for x in daily]
    prior_adx = {dl[i]: adx[dl[i-1]] for i in range(1, len(dl)) if dl[i-1] in adx}
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx)
    nextopen = {days[i]: byday[days[i+1]]["open"] for i in range(len(days)-1)}
    return byday, vix, prior_adx, days, nextopen


def run_window(name, a, b, byday, vix, prior_adx, days, nextopen):
    Rs = []; blocked = 0; traded_days = []
    for d in days:
        if not (a <= d <= b): continue
        sig = vix[d]
        if sig * 100 > VMAX or prior_adx[d] >= ADX_MAX:
            blocked += 1; continue
        spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0)
        if em <= 0: continue
        Kp = round(spot - K * em); Kpl = Kp - W
        Kc = round(spot + K * em); Kcl = Kc + W
        credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig)
        risk = W - credit
        if credit <= 0.05 or risk <= 0.05: continue
        sl = credit + STOP * credit; stopped = False; pnl = None
        for sp, m2c in byday[d]["path"][1:]:
            Tt = max(m2c, 0) / YEAR_MIN
            mark = condor_mark(sp, Kp, Kpl, Kc, Kcl, Tt, sig)
            if mark >= sl:
                pnl = credit - mark - COST; stopped = True; break
        if not stopped:
            put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
            call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
            pnl = credit - put_s - call_s - COST
            no = nextopen.get(d)  # overnight assignment gap on residue-zone days
            if no is not None:
                if Kpl < Sc < Kp: pnl += (no - Sc)
                elif Kc < Sc < Kcl: pnl += (Sc - no)
        Rs.append(pnl / risk); traded_days.append((d, pnl / risk))
    if not Rs:
        print(f"{name}: no traded days"); return
    n = len(Rs); wins = sum(1 for r in Rs if r > 0)
    # worst rolling 5-trade window
    worst5 = min(sum(Rs[i:i+5]) for i in range(max(1, n-4))) if n >= 1 else 0
    # max drawdown on the equity curve (in R)
    eq = 0.0; peak = 0.0; mdd = 0.0
    for r in Rs:
        eq += r; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    def dollars(R):  # translate R to realistic $ on 28ct: R * real_maxloss_ct * contracts
        return R * REAL_MAXLOSS_CT * CONTRACTS
    print(f"=== {name}  ({a} → {b}) ===")
    print(f"  traded {n} | gate blocked {blocked} ({100*blocked/(n+blocked):.0f}% NO-GO) | win {100*wins/n:.0f}%")
    print(f"  total {sum(Rs):+.1f}R  (~${dollars(sum(Rs)):+,.0f})  | avg/trade {statistics.mean(Rs):+.3f}R")
    print(f"  WORST DAY {min(Rs):+.2f}R  (~${dollars(min(Rs)):+,.0f})")
    print(f"  worst 5-trade stretch {worst5:+.2f}R  (~${dollars(worst5):+,.0f})")
    print(f"  max drawdown {mdd:+.2f}R  (~${dollars(mdd):+,.0f})")
    print()


def main():
    byday, vix, prior_adx, days, nextopen = load()
    print("Condor stress test — LIVE config, 28ct/$25k, overnight-gap included.")
    print("R = P&L / defined-risk-per-trade (−1.0 = full max-loss day). $ = R × $452/ct × 28.\n")
    W_ = [
        ("2025 FULL YEAR", date(2025,1,1), date(2025,12,31)),
        ("2025 Mar–May (crash+VIX)", date(2025,3,1), date(2025,5,31)),
        ("2025 Apr (worst)", date(2025,4,1), date(2025,4,30)),
        ("2024 H2 (calm, contrast)", date(2024,7,1), date(2024,12,31)),
        ("2026 YTD (calm, contrast)", date(2026,1,1), date(2026,6,17)),
    ]
    for name, a, b in W_:
        run_window(name, a, b, byday, vix, prior_adx, days, nextopen)


if __name__ == "__main__":
    main()
