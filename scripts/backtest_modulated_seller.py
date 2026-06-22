"""REGIME-MODULATED SELLER test: on high-prior-day-ADX (trend) days — the days the
base strangle AVOIDS — is a DEFENSIVE short strangle better than flat CASH?

The base strangle (backtest_strangle.py) sells on quiet days and stands aside on
trend days. The long-gamma trend leg FAILED the gate (backtest_trend_leg.py: OOS
-2.14, DSR 0%). Selling premium has edge in ALL regimes (the VRP is everywhere),
just with more realized-vol risk when trending. So the only un-tested question:
on trend days, is the right move FLAT, or a dialed-down short seller?

This leg, on prior-day ADX >= adx_min days only:
  • WIDER strikes (k = 1.0–2.0 expected moves vs 0.5 quiet) → lower credit, higher PoP.
  • TIGHTER stop (1.0–1.5x credit) to cut losers fast in a trend.
  • DIRECTIONAL SKEW: recenter the strangle in the prior-day trend direction
    (center = spot + dir·skew·EM) — give the trend more room, the legitimate use of
    the ADX/trend signal (modulate the seller, NOT switch to a buyer).
  • Same real-VIX1D pricing, intraday stop, close settle, and the SAME gate.

Bar to BEAT FLAT: positive OOS Sharpe after costs AND DSR>95%. Else trend days = cash.

Usage: uv run python -m scripts.backtest_modulated_seller [LEG_SPREAD] [N_CROSS]
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
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES, ADX_MIN_REGIME, VMAX = 20, 25.0, 40.0


def main():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass
    cl = ac._stock_client()
    end = datetime(2026, 6, 18, tzinfo=timezone.utc)
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
    # prior-day trend direction = sign of 3-day close momentum (uses data through prior day only)
    closes = [x["c"] for x in daily]
    pdir = {dl[i]: (1.0 if closes[i-1] > closes[max(i-4, 0)] else -1.0) for i in range(4, len(dl))}

    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx and d in pdir
                  and prior_adx[d] >= ADX_MIN_REGIME and vix[d] * 100 <= VMAX)

    grid = []
    for k in (1.0, 1.5, 2.0):            # wider than quiet-day 0.5
        for stop in (1.0, 1.5):          # tighter stop on trend days
            for skew in (0.0, 0.5, 1.0):  # recenter toward trend (× dir)
                grid.append({"k": k, "stop": stop, "skew": skew})
    cost = N_CROSS * (LEG_SPREAD / 2)

    def simulate(cfg):
        trades = []
        for d in days:
            sig = vix[d]; spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]
            T0 = max(tmin, 1) / YEAR_MIN; em = spot * sig * math.sqrt(T0)
            center = spot + pdir[d] * cfg["skew"] * em
            Kp, Kc = round(center - cfg["k"] * em), round(center + cfg["k"] * em)
            credit = bs(spot, Kp, T0, False, sig) + bs(spot, Kc, T0, True, sig)
            if credit <= 0.05: continue
            risk = cfg["stop"] * credit; sl = credit + risk; stopped = False
            for sp, m2c in byday[d]["path"][1:]:
                Tt = max(m2c, 0) / YEAR_MIN
                mark = bs(sp, Kp, Tt, False, sig) + bs(sp, Kc, Tt, True, sig)
                if mark >= sl:
                    pnl = credit - mark - cost; stopped = True; break
            if not stopped:
                pnl = credit - (max(0.0, Kp - Sc) + max(0.0, Sc - Kc)) - cost
            trades.append((d, pnl / risk))
        return trades

    print(f"REGIME-MODULATED SELLER (defensive strangle on ADX≥{ADX_MIN_REGIME:.0f} days) — cost ${cost:.3f}/sh")
    print(f"High-ADX trend days available: {len(days)}  ({days[0]} → {days[-1]})\n")
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
        print("Insufficient OOS trades — too few high-ADX days for a 252d train. Trend days ≈ CASH by default."); return
    m = sum(oos) / T; sd = (sum((r - m) ** 2 for r in oos) / T) ** 0.5
    skew = sum((r - m) ** 3 for r in oos) / (T * sd ** 3); kurt = sum((r - m) ** 4 for r in oos) / (T * sd ** 4)
    N = len(full_srs); vsr = sum((s - sum(full_srs) / N) ** 2 for s in full_srs) / N
    g = 0.5772156649
    sr0 = math.sqrt(vsr) * ((1 - g) * _nppf(1 - 1.0 / N) + g * _nppf(1 - 1.0 / (N * math.e)))
    den = math.sqrt(max(1e-9, 1 - skew * obs + ((kurt - 1) / 4) * obs ** 2))
    dsr = _ncdf((obs - sr0) * math.sqrt(T - 1) / den)
    yrs = (days[-1] - days[0]).days / 365.25; tpy = T / max(yrs, 0.1)
    ann = obs * math.sqrt(tpy); is_ann = (sum(is_best) / len(is_best) * math.sqrt(tpy)) if is_best else 0.0
    wins = sum(1 for r in oos if r > 0); worst = min(oos); avg = sum(oos) / T

    print(f"configs tried (N): {N}   folds: {len(is_best)}   OOS trades: {T}   ~{tpy:.0f}/yr")
    print(f"most-picked config: {max(picks, key=picks.get) if picks else 'n/a'}")
    print(f"\nIN-SAMPLE  best annualized Sharpe (avg/fold): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):     {ann:+.2f}   ← the honest number")
    print(f"  decay {is_ann - ann:+.2f}  |  win {wins/T*100:.0f}%  avg ret/trade {avg*100:+.1f}% of risk  worst {worst*100:.0f}%  skew {skew:+.2f}")
    print(f"DEFLATED SHARPE: SR0 {sr0:+.3f}  observed {obs:+.3f}  ➤ DSR = {dsr*100:.1f}%  (>95% ⇒ real)")
    edge_ok = dsr > 0.95 and ann > 0
    print(f"\nVERDICT (trend-day seller vs CASH):")
    if edge_ok:
        print(f"  PASS ✅ — defensive short premium BEATS flat on trend days (positive OOS + DSR>95%).")
    else:
        better = "positive but not gate-strong" if ann > 0 else "negative"
        print(f"  FAIL ❌ — OOS edge is {better}; does NOT clear the bar. Trend days should stay CASH.")


if __name__ == "__main__":
    main()
