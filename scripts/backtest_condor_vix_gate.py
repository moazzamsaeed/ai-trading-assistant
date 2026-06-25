"""CONDOR GATE A/B: prior-day DAILY ADX (lagging) vs forward VIX1D (same-day).

Motivation (2026-06-25): the live condor gates on prior-day DAILY ADX < 25, which
shut it out 4 days running this week even though several days were DEAD CALM intraday
(e.g. intraday ADX ~10) — exactly the rangebound condition a 0DTE condor wants. The
daily-ADX gate is a LAGGING proxy for "will today be calm" and has false negatives.
VIX1D (1-day implied vol, the OPEN print, known at the open → no lookahead) is a
FORWARD same-day measure. Q: if we gate on VIX1D INSTEAD of prior-day daily ADX —
trading the calm-implied-vol days the daily gate skips — does the strategy still
clear the gate (DSR>95%, positive OOS Sharpe after costs), or do those extra days
break it?

Identical condor mechanics / costs / walk-forward + Deflated-Sharpe gate as
backtest_wide_condor.py; ONLY the per-day entry filter changes. Runs every gate
mode on the SAME data and prints a side-by-side.

Usage: .venv/bin/python -m scripts.backtest_condor_vix_gate [LEG_SPREAD] [N_CROSS]
"""
from __future__ import annotations
import csv, math, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, sharpe, _ncdf, _nppf, YEAR_MIN

ET = ZoneInfo("America/New_York")
LEG_SPREAD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.04
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES = 20
COST = N_CROSS * (LEG_SPREAD / 2)

GRID = [{"k": k, "W": W, "stop": stop}
        for k in (0.5, 1.0) for W in (5.0, 10.0, 20.0) for stop in (1.5, 99.0)]


def condor_mark(spot, Kp, Kpl, Kc, Kcl, T, sig):
    put_sp = bs(spot, Kp, T, False, sig) - bs(spot, Kpl, T, False, sig)
    call_sp = bs(spot, Kc, T, True, sig) - bs(spot, Kcl, T, True, sig)
    return put_sp + call_sp


def simulate(cfg, days, byday, vix, gate):
    trades = []
    for d in days:
        if not gate(d):
            continue
        sig = vix[d]
        spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0)
        if em <= 0: continue
        Kp = round(spot - cfg["k"] * em); Kpl = Kp - cfg["W"]
        Kc = round(spot + cfg["k"] * em); Kcl = Kc + cfg["W"]
        credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig)
        risk = cfg["W"] - credit
        if credit <= 0.05 or risk <= 0.05: continue
        sl = credit + cfg["stop"] * credit; stopped = False
        for sp, m2c in byday[d]["path"][1:]:
            Tt = max(m2c, 0) / YEAR_MIN
            mark = condor_mark(sp, Kp, Kpl, Kc, Kcl, Tt, sig)
            if mark >= sl:
                pnl = credit - mark - COST; stopped = True; break
        if not stopped:
            put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
            call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
            pnl = credit - put_s - call_s - COST
        trades.append((d, pnl / risk))
    return trades


def evaluate(days, byday, vix, gate):
    """Full walk-forward + Deflated Sharpe for one gate. Returns a result dict."""
    sims = [simulate(c, days, byday, vix, gate) for c in GRID]
    full_srs = [sharpe([r for _, r in s]) or 0.0 for s in sims]
    n_traded = len(set(d for s in sims for d, _ in s))  # distinct days any config traded

    oos, is_best, picks = [], [], {}
    start = TRAIN_DAYS
    while start + EMBARGO_DAYS + TEST_DAYS <= len(days):
        train = set(days[start - TRAIN_DAYS:start])
        test = set(days[start + EMBARGO_DAYS:start + EMBARGO_DAYS + TEST_DAYS])
        bsr, bk = None, None
        for k, s in enumerate(sims):
            isr = [r for d, r in s if d in train]
            if len(isr) < MIN_IS_TRADES: continue
            sr = sharpe(isr)
            if sr is not None and (bsr is None or sr > bsr): bsr, bk = sr, k
        if bk is not None:
            is_best.append(bsr); oos += [r for d, r in sims[bk] if d in test]
            picks[str(GRID[bk])] = picks.get(str(GRID[bk]), 0) + 1
        start += TEST_DAYS

    T = len(oos); obs = sharpe(oos)
    if T < 2 or obs is None:
        return {"n_traded": n_traded, "T": T, "dsr": 0.0, "ann": 0.0, "win": 0.0,
                "worst": 0.0, "pass": False, "note": "insufficient OOS"}
    m = sum(oos) / T; sd = (sum((r - m) ** 2 for r in oos) / T) ** 0.5
    skew = sum((r - m) ** 3 for r in oos) / (T * sd ** 3)
    kurt = sum((r - m) ** 4 for r in oos) / (T * sd ** 4)
    N = len(full_srs); vsr = sum((s - sum(full_srs) / N) ** 2 for s in full_srs) / N
    g = 0.5772156649
    sr0 = math.sqrt(vsr) * ((1 - g) * _nppf(1 - 1.0 / N) + g * _nppf(1 - 1.0 / (N * math.e)))
    den = math.sqrt(max(1e-9, 1 - skew * obs + ((kurt - 1) / 4) * obs ** 2))
    dsr = _ncdf((obs - sr0) * math.sqrt(T - 1) / den)
    yrs = (days[-1] - days[0]).days / 365.25; tpy = T / max(yrs, 0.1)
    ann = obs * math.sqrt(tpy)
    wins = sum(1 for r in oos if r > 0); worst = min(oos)
    return {"n_traded": n_traded, "T": T, "dsr": dsr * 100, "ann": ann,
            "win": wins / T * 100, "worst": worst * 100, "pass": dsr > 0.95 and ann > 0,
            "pick": max(picks, key=picks.get) if picks else "n/a"}


def main():
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
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": []})
        rec["close"] = float(b.close); m2c = (16 - et.hour) * 60 - et.minute
        if rec["entry"] is None and et.hour == 10 and et.minute == 0: rec["entry"] = (float(b.close), m2c)
        if rec["entry"] is not None: rec["path"].append((float(b.close), max(m2c, 0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=datetime(2022, 1, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl = [x["d"] for x in daily]
    prior_adx = {dl[i]: adx[dl[i - 1]] for i in range(1, len(dl)) if dl[i - 1] in adx}
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx)

    n_blocked = sum(1 for d in days if prior_adx[d] >= 25.0 and vix[d] * 100 < 40)
    print(f"SPY {days[0]} → {days[-1]}  ({len(days)} candidate days)  4-leg cost ${COST:.3f}/sh")
    print(f"days the BASELINE skips on daily-ADX≥25 alone (VIX1D was <40): {n_blocked}  "
          f"← the days in question\n")

    gates = [
        ("BASELINE  prior-ADX<25 & VIX1D<40", lambda d: prior_adx[d] < 25.0 and vix[d] * 100 < 40),
        ("VIX1D<40 only  (drop daily ADX)",    lambda d: vix[d] * 100 < 40),
        ("VIX1D<35 only",                       lambda d: vix[d] * 100 < 35),
        ("VIX1D<30 only",                       lambda d: vix[d] * 100 < 30),
        ("VIX1D<25 only",                       lambda d: vix[d] * 100 < 25),
    ]
    hdr = f"{'gate':<36} | {'days':>4} | {'OOS n':>5} | {'OOS Shrp':>8} | {'DSR%':>5} | {'win%':>4} | {'worst':>6} | verdict"
    print(hdr); print("-" * len(hdr))
    for name, gate in gates:
        r = evaluate(days, byday, vix, gate)
        verdict = "PASS ✅" if r["pass"] else "FAIL ❌"
        print(f"{name:<36} | {r['n_traded']:>4} | {r['T']:>5} | {r['ann']:>+8.2f} | "
              f"{r['dsr']:>5.0f} | {r['win']:>3.0f}% | {r['worst']:>5.0f}% | {verdict}")
    print("\nGATE = PASS needs DSR>95% AND positive OOS annualized Sharpe (after 4-leg costs).")
    print("Read: if a VIX1D-only gate trades MORE days yet still PASSES, the daily-ADX")
    print("filter is over-conservative (leaving +EV calm days on the table). If the")
    print("VIX1D-only gates FAIL, the daily-ADX filter is doing real protective work.")


if __name__ == "__main__":
    main()
