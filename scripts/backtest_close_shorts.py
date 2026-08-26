"""Does "close the SHORT legs at 15:45" beat "let the condor settle at expiry"?

Compares two end-of-day policies on the historical near-strike days, at the live
config (VIX1D<35 & ADX<27, 0.5xEM strikes, $5 wings). For each traded day:

  SETTLE (baseline = what actually happens now, since the 4-leg close mostly
    fails to fill): hold to expiry. P&L = credit - condor intrinsic at close.
    On days a short finishes ITM but its long wing is OTM (the assignment-residue
    zone), the trader is assigned shares and holds OVERNIGHT -> add the realized
    overnight gap (sell at next open) as an extra, zero-mean-but-tail term.

  CLOSE-SHORTS (only on near-strike days; comfortably-inside days expire free):
    buy back the two short legs at their 15:45 BS mark (+ 2-leg buyback spread),
    let the long wings expire. No assignment, no overnight gamble -- but you pay
    the shorts' remaining TIME VALUE, which would have decayed to ~0 on the many
    near-strike days that recover into the range.

Swept over trigger tightness (near-strike band, plus ITM-only). P&L per contract
in dollars; scale by contracts for account impact.

Usage: .venv/bin/python -m scripts.backtest_close_shorts
"""
from __future__ import annotations
import csv, math, statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0
ADX_MAX, VMAX = 27.0, 35.0
LEG_SPREAD = 0.04
ENTRY_COST = 6 * (LEG_SPREAD / 2)     # 4 legs in + ~2 to close, per the other backtests
BUYBACK_COST = 2 * (LEG_SPREAD / 2)   # close-shorts crosses only 2 legs
BANDS = [0.0, 0.0015, 0.003, 0.005]   # 0.0 = ITM-only trigger; else near-strike % band
CONTRACTS = 28


def leg(spot, Kk, T, is_call, sig):
    return bs(spot, Kk, T, is_call, sig)


def load():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass
    cl = ac._stock_client(); end = datetime(2026, 6, 18, tzinfo=timezone.utc)
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=datetime(2023, 1, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": [], "open": None})
        m2c = (16 - et.hour) * 60 - et.minute
        c = float(b.close)
        if rec["open"] is None: rec["open"] = c
        rec["close"] = c
        if rec["entry"] is None and et.hour == 10 and et.minute == 0: rec["entry"] = (c, m2c)
        if rec["entry"] is not None: rec["path"].append((c, max(m2c, 0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=datetime(2022, 1, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl = [x["d"] for x in daily]
    prior_adx = {dl[i]: adx[dl[i-1]] for i in range(1, len(dl)) if dl[i-1] in adx}
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx)
    nextopen = {days[i]: byday[days[i+1]]["open"] for i in range(len(days)-1)}
    return byday, vix, prior_adx, days, nextopen


def simulate(band, byday, vix, prior_adx, days, nextopen):
    """Return per-day (settle_pnl, closeshorts_pnl, was_near, assigned) in $/contract."""
    out = []
    for d in days:
        sig = vix[d]
        if sig * 100 > VMAX or prior_adx[d] >= ADX_MAX:
            continue
        spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0)
        if em <= 0: continue
        Kp = round(spot - K * em); Kpl = Kp - W
        Kc = round(spot + K * em); Kcl = Kc + W
        credit = (leg(spot, Kp, T0, False, sig) - leg(spot, Kpl, T0, False, sig)
                  + leg(spot, Kc, T0, True, sig) - leg(spot, Kcl, T0, True, sig))
        risk = W - credit
        if credit <= 0.05 or risk <= 0.05: continue

        # 15:45 decision point: bar with m2c closest to 15
        s1545, t1545 = min(byday[d]["path"], key=lambda pr: abs(pr[1] - 15))
        T15 = max(t1545, 1) / YEAR_MIN

        # near-strike trigger: ITM on either short (band==0) OR within `band` of a short
        near = (s1545 <= Kp or s1545 >= Kc) or (
            band > 0 and (s1545 <= Kp * (1 + band) or s1545 >= Kc * (1 - band))
        )

        # SETTLE: condor intrinsic at close
        put_int = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
        call_int = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
        settle = credit - put_int - call_int - ENTRY_COST
        assigned = False
        # assignment-residue zone: a short ITM while its long wing is OTM -> hold shares overnight
        no = nextopen.get(d)
        if no is not None:
            if Kpl < Sc < Kp:       # short put assigned (long shares), long put OTM
                assigned = True
                settle += (no - Sc)   # overnight gap on the long shares, per share
            elif Kc < Sc < Kcl:     # short call assigned (short shares), long call OTM
                assigned = True
                settle += (Sc - no)   # overnight gap on the short shares

        # CLOSE-SHORTS at 15:45 on near-strike days; else expire free (== settle when inside)
        if near:
            sp_mark = leg(s1545, Kp, T15, False, sig)
            sc_mark = leg(s1545, Kc, T15, True, sig)
            long_put_int = max(0.0, Kpl - Sc)
            long_call_int = max(0.0, Sc - Kcl)
            closeshorts = (credit - sp_mark - sc_mark + long_put_int + long_call_int
                           - ENTRY_COST - BUYBACK_COST)
        else:
            closeshorts = credit - put_int - call_int - ENTRY_COST  # untouched -> same as settle (no assignment when OTM)

        out.append((settle * 100, closeshorts * 100, near, assigned))  # $/contract
    return out


def stats(vals):
    n = len(vals); tot = sum(vals); mean = tot / n if n else 0
    sd = statistics.pstdev(vals) if n > 1 else 0
    return n, tot, mean, sd, (min(vals) if vals else 0)


def main():
    byday, vix, prior_adx, days, nextopen = load()
    base = simulate(0.0, byday, vix, prior_adx, days, nextopen)
    n_all = len(base)
    n_assigned = sum(1 for _, _, _, a in base if a)
    print(f"SPY {days[0]} → {days[-1]}  |  gate VIX1D<{VMAX:.0f} & ADX<{ADX_MAX:.0f}, 0.5xEM, ${W:.0f} wings")
    print(f"traded days: {n_all}  |  assignment-residue-zone days: {n_assigned} "
          f"({100*n_assigned/n_all:.0f}%)  |  P&L in $/contract\n")
    hdr = f"{'trigger':>12} | {'near days':>9} | {'SETTLE total':>12} | {'CLOSE-SH total':>14} | {'Δ total':>9} | {'SETTLE worst':>12} | {'CLOSE-SH worst':>14}"
    print(hdr); print("-" * len(hdr))
    for band in BANDS:
        res = simulate(band, byday, vix, prior_adx, days, nextopen)
        settle = [r[0] for r in res]; cs = [r[1] for r in res]
        n_near = sum(1 for r in res if r[2])
        _, s_tot, _, _, s_worst = stats(settle)
        _, c_tot, _, _, c_worst = stats(cs)
        label = "ITM-only" if band == 0 else f"near {band*100:.2f}%"
        d_tot = c_tot - s_tot
        print(f"{label:>12} | {n_near:9d} | {s_tot*CONTRACTS:12,.0f} | {c_tot*CONTRACTS:14,.0f} | "
              f"{d_tot*CONTRACTS:9,.0f} | {s_worst*CONTRACTS:12,.0f} | {c_worst*CONTRACTS:14,.0f}")
    print(f"\n(totals scaled to {CONTRACTS} contracts. Δ total = CLOSE-SHORTS − SETTLE; negative = close-shorts LOSES money overall.)")
    print("SETTLE worst includes the overnight assignment gap; CLOSE-SHORTS worst does not (it flattens at 15:45).")
    print("Read: if Δ total is negative, the time-value tax on recover-days exceeds the tail")
    print("protection — close-shorts costs more than it saves (like the stop-tightening result).")
    print("If CLOSE-SHORTS 'worst' is much shallower than SETTLE 'worst', it's buying tail/variance")
    print("reduction at that cost — a risk tradeoff, not a profit improvement.")


if __name__ == "__main__":
    main()
