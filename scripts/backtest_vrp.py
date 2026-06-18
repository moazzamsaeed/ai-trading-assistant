"""VRP strategy backtest + validation gate: deterministic SPY 0DTE iron condors,
priced on REAL VIX1D implied vol, run through walk-forward + Deflated Sharpe.

The strategy (defined-risk premium SELLING — harvest the vol-risk-premium):
  • Once/day, enter an iron condor at ~10:00 ET on SPY 0DTE.
  • Entry credit priced with Black-Scholes at the day's REAL VIX1D (the 1-day
    implied-vol index — so the VRP, IV>realized, is captured honestly, including
    vol spikes). Short strikes at k×(expected move); fixed-width long wings.
  • Hold to expiry; settle at SPY's actual close (intrinsic). Costs = bid/ask
    crossing on the legs. Return = P&L / max-loss (risk).

Validation (same bar as validate_strategy.py): embargoed walk-forward picks the
best config in-sample, scores OOS; Deflated Sharpe deflates by the #configs tried
+ skew/kurtosis. PLUS a tail check (max drawdown / worst day) because a premium
seller's whole risk is the negative-skew tail.

Data: data/vix1d.csv (Cboe), SPY 15-min from Alpaca.
Usage: uv run python -m scripts.backtest_vrp [LEG_SPREAD] [N_CROSS]
"""
from __future__ import annotations

import csv
import math
import sys
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac

ET = ZoneInfo("America/New_York")
TF = 15
# Trading-time annualization (252 trading days × 390 min/day) — MUST match VIX1D,
# which is annualized on trading time. Using calendar time here placed strikes
# ~2.3× too tight and broke the condor's win rate.
YEAR_MIN = 252 * 390
LEG_SPREAD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.03   # per-leg bid/ask ($/share)
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 6           # leg crossings (4 in + ~2 ITM out)
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES = 20


def is_rth(ts):
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _nppf(p):
    a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239]
    b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1]
    c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416]
    pl, ph = 0.02425, 0.97575
    if p < pl:
        q = math.sqrt(-2 * math.log(p)); return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= ph:
        q = p - 0.5; r = q*q; return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q/(((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p)); return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5])/((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def bs(S, K, T, call, sig):
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T)); d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def sharpe(rs):
    n = len(rs)
    if n < 2: return None
    m = sum(rs) / n; sd = (sum((r - m) ** 2 for r in rs) / n) ** 0.5
    return m / sd if sd > 0 else None


def main():
    # VIX1D: date -> open (entry-time implied vol)
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass

    req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
                           start=datetime(2023, 1, 1, tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)
    bars = [b for b in ac._stock_client().get_stock_bars(req).data.get("SPY", []) if is_rth(b.timestamp)]
    # per day: entry (10:00 ET) spot + minutes-to-close, and the day's close
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None})
        rec["close"] = float(b.close)
        if rec["entry"] is None and et.hour == 10 and et.minute == 0:
            rec["entry"] = (float(b.close), (16 - et.hour) * 60 - et.minute)
    days = sorted(d for d, r in byday.items() if r["entry"] and d in vix)

    grid = []
    for k in (0.75, 1.0, 1.25):          # short strikes at k expected-moves
        for wing in (3.0, 5.0):          # long-wing width ($)
            for vmax in (99.0, 25.0, 40.0):  # skip days with VIX1D above this (regime filter)
                grid.append({"k": k, "wing": wing, "vmax": vmax})

    cost = N_CROSS * (LEG_SPREAD / 2)    # $/share total transaction cost per condor

    def simulate(cfg):
        trades = []
        for d in days:
            sig = vix[d]
            if sig * 100 > cfg["vmax"]:
                continue
            spot, tmin = byday[d]["entry"]; S = byday[d]["close"]
            T = max(tmin, 1) / YEAR_MIN
            em = spot * sig * math.sqrt(T)
            if em <= 0: continue
            spS = round(spot - cfg["k"] * em); spL = spS - cfg["wing"]
            scS = round(spot + cfg["k"] * em); scL = scS + cfg["wing"]
            put_cr = bs(spot, spS, T, False, sig) - bs(spot, spL, T, False, sig)
            call_cr = bs(spot, scS, T, True, sig) - bs(spot, scL, T, True, sig)
            credit = put_cr + call_cr
            risk = cfg["wing"] - credit
            if risk <= 0.05:  # degenerate (strikes too tight) — not a real defined-risk condor
                continue
            put_settle = max(0.0, spS - S) - max(0.0, spL - S)
            call_settle = max(0.0, S - scS) - max(0.0, S - scL)
            pnl = credit - cost - put_settle - call_settle
            trades.append((d, pnl / risk))
        return trades

    print(f"VRP iron-condor backtest — real VIX1D, cost={N_CROSS}×${LEG_SPREAD/2:.3f}=${cost:.3f}/share/condor")
    print(f"SPY days with VIX1D + 10:00 entry: {len(days)}  ({days[0]} → {days[-1]})\n")
    sims = [simulate(c) for c in grid]
    full_srs = [sharpe([r for _, r in s]) or 0.0 for s in sims]

    # ---- embargoed walk-forward ----
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
        """Risk `frac` of capital per trade (account return = frac × return-on-risk).
        Sharpe/DSR are sizing-independent; only the equity curve / maxDD scale."""
        eq = 1.0; peak = 1.0; mdd = 0.0
        for r in oos:
            eq *= (1 + frac * r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
        return eq - 1, mdd

    print(f"configs tried (N): {N}   folds: {len(is_best)}   OOS trades: {T}   ~{tpy:.0f}/yr")
    print(f"most-picked config: {max(picks, key=picks.get) if picks else 'n/a'}")
    print(f"\n── THE EDGE (sizing-independent) ──")
    print(f"IN-SAMPLE  best annualized Sharpe (avg/fold): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):     {ann:+.2f}   ← the honest number")
    print(f"  overfitting decay (IS→OOS): {is_ann - ann:+.2f}   |   win {wins/T*100:.0f}%   skew {skew:+.2f}  kurt {kurt:.1f}")
    print(f"DEFLATED SHARPE: SR0(null max) {sr0:+.3f}  observed {obs:+.3f}  ➤ DSR = {dsr*100:.1f}%  (>95% ⇒ real)")
    print(f"\n── SURVIVABILITY (per-trade risk sizing; worst single trade = {worst*100:.0f}% of risk) ──")
    for f in (0.02, 0.03, 0.05):
        tot, mdd = equity_at(f)
        note = "  (~1 condor needs this at $5k)" if f == 0.05 else ("  (needs ~$12-15k for 1 condor)" if f == 0.02 else "")
        print(f"  risk {f*100:.0f}%/trade: total {tot*100:+.0f}%  maxDD {mdd*100:.0f}%{note}")
    edge_ok = dsr > 0.95 and ann > 0
    _, mdd5 = equity_at(0.03)
    surv = mdd5 > -0.35
    print(f"\nVERDICT:")
    print(f"  EDGE: {'PASS ✅' if edge_ok else 'FAIL ❌'}  (DSR>95% AND OOS Sharpe>0)")
    print(f"  SURVIVABLE at 3%/trade sizing: {'YES ✅' if surv else 'NO ⚠️ — negative-skew tail, needs more capital / tighter risk'}")


if __name__ == "__main__":
    main()
