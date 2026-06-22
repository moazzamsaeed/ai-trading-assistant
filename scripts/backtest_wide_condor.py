"""WIDE IRON CONDOR test — the Alpaca-executable (Level-3, defined-risk) form of the
gate-passing naked strangle. Same winning recipe (k≈0.5 short strikes, prior-day-ADX
regime filter, real VIX1D pricing, intraday stop) BUT with far-OTM long wings added
so it's defined-risk multi-leg (OrderClass.MLEG) — runnable on the current Alpaca
account without naked/Level-4 approval.

THE QUESTION: the naked strangle's whole edge over the iron condor was FEWER LEGS →
cost-robust (DSR>95% even at pessimistic cost). Adding wings back = 4 legs again =
the cost structure that FAILED the original condor (backtest_vrp.py: degraded to
DSR 56% realistic, negative pessimistic). Does the WIDE condor — wings far enough OTM
to be cheap, so we keep most of the credit — survive, or does it re-break?

Structure per day (high credit, defined risk):
  • Short put @ spot−k·EM, long put @ short−W   (put spread)
  • Short call @ spot+k·EM, long call @ short+W  (call spread)
  • credit = both short legs − both wings;  defined max loss (risk) = W − credit
  • Same regime filter (prior-day ADX<adx_max, VIX1D<vmax) + intraday stop, real VIX1D.
  • 4-leg transaction cost (the load-bearing variable — sweep it).

Same gate: embargoed walk-forward + Deflated Sharpe + survivability.
Usage: uv run python -m scripts.backtest_wide_condor [LEG_SPREAD] [N_CROSS]
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
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 6   # 4 legs in + ~2 to close the tested spread
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES, ADX_MAX, VMAX = 20, 25.0, 40.0


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
    prior_adx = {dl[i]: adx[dl[i-1]] for i in range(1, len(dl)) if dl[i-1] in adx}
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx)

    grid = []
    for k in (0.5, 1.0):                  # short-strike distance (expected moves)
        for W in (5.0, 10.0, 20.0):       # WING width ($) — wider = cheaper wings, keep more credit, but bigger risk
            for stop in (1.5, 99.0):      # intraday stop ×credit (99 = none, rely on wings)
                grid.append({"k": k, "W": W, "stop": stop})
    cost = N_CROSS * (LEG_SPREAD / 2)

    def condor_mark(spot, Kp, Kpl, Kc, Kcl, T, sig):
        put_sp = bs(spot, Kp, T, False, sig) - bs(spot, Kpl, T, False, sig)
        call_sp = bs(spot, Kc, T, True, sig) - bs(spot, Kcl, T, True, sig)
        return put_sp + call_sp

    def simulate(cfg):
        trades = []
        for d in days:
            sig = vix[d]
            if sig * 100 > VMAX or prior_adx[d] >= ADX_MAX:
                continue
            spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
            em = spot * sig * math.sqrt(T0)
            if em <= 0: continue
            Kp = round(spot - cfg["k"] * em); Kpl = Kp - cfg["W"]
            Kc = round(spot + cfg["k"] * em); Kcl = Kc + cfg["W"]
            credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig)
            risk = cfg["W"] - credit                # defined max loss
            if credit <= 0.05 or risk <= 0.05: continue
            sl = credit + cfg["stop"] * credit; stopped = False
            for sp, m2c in byday[d]["path"][1:]:
                Tt = max(m2c, 0) / YEAR_MIN
                mark = condor_mark(sp, Kp, Kpl, Kc, Kcl, Tt, sig)
                if mark >= sl:
                    pnl = credit - mark - cost; stopped = True; break
            if not stopped:
                put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
                call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
                pnl = credit - put_s - call_s - cost
            trades.append((d, pnl / risk))
        return trades

    print(f"WIDE IRON CONDOR (strangle + far wings) — real VIX1D, ADX<{ADX_MAX:.0f}, 4-leg cost ${cost:.3f}/sh ({N_CROSS} legs)")
    print(f"SPY days available: {len(days)}  ({days[0]} → {days[-1]})\n")
    sims = [simulate(c) for c in grid]
    full_srs = [sharpe([r for _, r in s]) or 0.0 for s in sims]

    oos, is_best, picks = [], [], {}
    start = TRAIN_DAYS
    while start + EMBARGO_DAYS + TEST_DAYS <= len(days):
        train = set(days[start - TRAIN_DAYS:start]); test = set(days[start + EMBARGO_DAYS:start + EMBARGO_DAYS + TEST_DAYS])
        bsr, bk = None, None
        for k, s in enumerate(sims):
            isr = [r for d, r in s if d in train]
            if len(isr) < MIN_IS_TRADES: continue
            sr = sharpe(isr)
            if sr is not None and (bsr is None or sr > bsr): bsr, bk = sr, k
        if bk is not None:
            is_best.append(bsr); oos += [r for d, r in sims[bk] if d in test]
            picks[str(grid[bk])] = picks.get(str(grid[bk]), 0) + 1
        start += TEST_DAYS

    T = len(oos); obs = sharpe(oos)
    if T < 2 or obs is None:
        print("Insufficient OOS trades."); return
    m = sum(oos) / T; sd = (sum((r - m) ** 2 for r in oos) / T) ** 0.5
    skew = sum((r - m) ** 3 for r in oos) / (T * sd ** 3); kurt = sum((r - m) ** 4 for r in oos) / (T * sd ** 4)
    N = len(full_srs); vsr = sum((s - sum(full_srs) / N) ** 2 for s in full_srs) / N
    g = 0.5772156649
    sr0 = math.sqrt(vsr) * ((1 - g) * _nppf(1 - 1.0 / N) + g * _nppf(1 - 1.0 / (N * math.e)))
    den = math.sqrt(max(1e-9, 1 - skew * obs + ((kurt - 1) / 4) * obs ** 2))
    dsr = _ncdf((obs - sr0) * math.sqrt(T - 1) / den)
    yrs = (days[-1] - days[0]).days / 365.25; tpy = T / max(yrs, 0.1)
    ann = obs * math.sqrt(tpy); is_ann = (sum(is_best) / len(is_best) * math.sqrt(tpy)) if is_best else 0.0
    wins = sum(1 for r in oos if r > 0); worst = min(oos)

    def equity_at(frac):
        eq = 1.0; peak = 1.0; mdd = 0.0
        for r in oos:
            eq *= (1 + frac * r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        return eq - 1, mdd

    print(f"configs tried (N): {N}   folds: {len(is_best)}   OOS trades: {T}   ~{tpy:.0f}/yr")
    print(f"most-picked config: {max(picks, key=picks.get) if picks else 'n/a'}")
    print(f"\nIN-SAMPLE  best annualized Sharpe (avg/fold): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):     {ann:+.2f}   ← the honest number")
    print(f"  decay {is_ann - ann:+.2f}  |  win {wins/T*100:.0f}%  worst {worst*100:.0f}%  skew {skew:+.2f}  kurt {kurt:.1f}")
    print(f"DEFLATED SHARPE: SR0 {sr0:+.3f}  observed {obs:+.3f}  ➤ DSR = {dsr*100:.1f}%  (>95% ⇒ real)")
    _, mdd3 = equity_at(0.03)
    edge_ok = dsr > 0.95 and ann > 0
    print(f"\nVERDICT (Alpaca-executable wide condor):")
    print(f"  EDGE: {'PASS ✅ — survives the wings; deployable on Alpaca' if edge_ok else 'FAIL ❌ — wings re-broke the cost edge (as feared); needs naked strangle elsewhere'}")
    print(f"  maxDD @3%/trade: {mdd3*100:.0f}%")


if __name__ == "__main__":
    main()
