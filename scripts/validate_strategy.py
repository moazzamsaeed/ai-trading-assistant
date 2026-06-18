"""Trading-grade validation harness: embargoed walk-forward + Deflated Sharpe +
costed fills. The gate the research says MUST pass before risking real capital.

What it does, and why each piece exists (refs in docs/STRATEGY_REVIEW_2026-06-18):
  • WALK-FORWARD with an embargo gap — on each rolling 252-day TRAIN window pick the
    best parameter config, then score it ONLY on the next (embargoed) 21-day TEST
    window. The concatenated TEST results are true out-of-sample. The IS-vs-OOS
    Sharpe decay measures overfitting (López de Prado).
  • DEFLATED SHARPE RATIO (Bailey & López de Prado, SSRN 2460551) — deflates the
    OOS Sharpe by the expected-max Sharpe under the null given the NUMBER OF
    CONFIGS TRIED (multiple-testing) plus return skew/kurtosis. DSR > 0.95 ≈ the
    edge is statistically real after accounting for the search. "A backtest that
    does not control for the number of trials is worthless."
  • COSTED FILLS — every trade pays the bid/ask spread + move-scaled slippage on a
    Black-Scholes 0DTE option with real intraday time-to-expiry. The thing our
    paper harness ignores and which dominates a tiny 0DTE taker's costs.

This is honest by construction: paper fills can't be gamed because costs are
modeled, and the param search is penalised by the DSR. Verdict at the end.

Usage: uv run python -m scripts.validate_strategy [IV] [SPREAD] [SLIP_COEF]
"""
from __future__ import annotations

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
YEAR_MIN = 365 * 24 * 60
# "Realistic" fill regime from the fill-stress grid (not the optimistic paper one)
IV = float(sys.argv[1]) if len(sys.argv) > 1 else 0.18
SPREAD = float(sys.argv[2]) if len(sys.argv) > 2 else 0.06
SLIP_COEF = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05
# Walk-forward windows (trading days)
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES = 10  # don't select a config that barely traded in-sample


# ---- indicators / pricing ----
def is_rth(ts):
    et = ts.astimezone(ET)
    return et.weekday() < 5 and dtime(9, 30) <= et.time() <= dtime(16, 0)


def ema(xs, p):
    a = 2.0 / (p + 1.0); o = [xs[0]]
    for x in xs[1:]:
        o.append(a * x + (1 - a) * o[-1])
    return o


def adx_series(high, low, close, period=14):
    n = len(close); out = [None] * n
    if n < 2 * period + 1:
        return out
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        up, dn = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))

    def wil(v):
        s = sum(v[:period]); r = [s]
        for x in v[period:]:
            s = s - s / period + x; r.append(s)
        return r
    st, sp, sm = wil(tr), wil(pdm), wil(mdm)
    dxs = []
    for t, p, m in zip(st, sp, sm):
        den = (100 * p / t) + (100 * m / t) if t else 0.0
        dxs.append(0.0 if not den else 100 * abs((100 * p / t) - (100 * m / t)) / den)
    if len(dxs) < period:
        return out
    a = sum(dxs[:period]) / period; seq = [a]
    for dx in dxs[period:]:
        a = (a * (period - 1) + dx) / period; seq.append(a)
    b = 2 * period - 1
    for m, v in enumerate(seq):
        if b + m < n:
            out[b + m] = v
    return out


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _nppf(p):  # Acklam inverse-normal
    a = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2, 1.38357751867269e2, -3.066479806614716e1, 2.506628277459239]
    b = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2, 6.680131188771972e1, -1.328068155288572e1]
    c = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783]
    d = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= ph:
        q = p - 0.5; r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def bs(S, K, T, call):
    if T <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + 0.5 * IV * IV * T) / (IV * math.sqrt(T))
    d2 = d1 - IV * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def sharpe(rets):
    n = len(rets)
    if n < 2:
        return None
    m = sum(rets) / n
    sd = (sum((r - m) ** 2 for r in rets) / n) ** 0.5
    return m / sd if sd > 0 else None


def main():
    req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
                           start=datetime(2023, 1, 1, tzinfo=timezone.utc),
                           end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)
    raw = ac._stock_client().get_stock_bars(req).data.get("SPY", [])
    bars = [b for b in raw if is_rth(b.timestamp)]
    close = [float(b.close) for b in bars]; high = [float(b.high) for b in bars]
    low = [float(b.low) for b in bars]; vol = [float(b.volume) for b in bars]
    day = [b.timestamp.astimezone(ET).date() for b in bars]
    tmin = [(16 - b.timestamp.astimezone(ET).hour) * 60 - b.timestamp.astimezone(ET).minute for b in bars]
    n = len(close)
    vwap = [0.0] * n; cpv = cv = 0.0
    for i in range(n):
        if i == 0 or day[i] != day[i - 1]:
            cpv = cv = 0.0
        tp = (high[i] + low[i] + close[i]) / 3
        cpv += tp * vol[i]; cv += vol[i]
        vwap[i] = cpv / cv if cv else close[i]
    em = ema(close, 20); adx = adx_series(high, low, close, 14)
    days = sorted(set(day))

    # ---- parameter grid (the SEARCH the DSR must penalise) ----
    grid = []
    for adx_min in (20.0, 25.0, 30.0):
        for dist_min in (0.05, 0.10, 0.15):
            for hold in (30, 60, 120):
                for side in ("both", "puts"):
                    grid.append({"adx_min": adx_min, "dist_min": dist_min, "hold": hold, "side": side})

    def simulate(p):
        """Event-driven sim over the FULL series → list of (entry_day, costed_return).
        One position at a time, 30-min cooldown, 0DTE same-day exit, modeled costs."""
        hbars = p["hold"] // TF; cool = 30 // TF
        trades = []; pos = None; cd = -1
        for i in range(200, n):
            if pos is not None:
                ei, call, S0, p0, eday = pos
                eod = (i + 1 >= n) or (day[i + 1] != day[i])
                if (i - ei) * TF >= p["hold"] or eod or tmin[i] <= TF:
                    p1 = bs(close[i], S0, max(tmin[i], 1) / YEAR_MIN, call)
                    slip = SLIP_COEF * abs(close[i] / S0 - 1) * 100
                    entry = p0 + SPREAD / 2
                    exitp = p1 - SPREAD / 2 - slip
                    trades.append((eday, (exitp - entry) / entry if entry > 0 else 0.0))
                    pos = None; cd = i + cool
                continue
            if i < cd or tmin[i] < p["hold"] + 5 or adx[i] is None:
                continue
            up = close[i] > vwap[i] and close[i] > em[i]
            down = close[i] < vwap[i] and close[i] < em[i]
            if not (up or down):
                continue
            if p["side"] == "puts" and up:
                continue
            d = abs(close[i] - vwap[i]) / close[i] * 100
            if adx[i] < p["adx_min"] or adx[i] >= 50 or d < p["dist_min"] or d > 0.5:
                continue
            call = up
            p0 = bs(close[i], close[i], tmin[i] / YEAR_MIN, call)
            pos = (i, call, close[i], p0, day[i])
        return trades

    print(f"Simulating {len(grid)} configs (IV={IV:.0%}, spread=${SPREAD:.2f}, slip={SLIP_COEF}/1%)...")
    sims = [simulate(p) for p in grid]                       # one full sim per config
    full_srs = [sharpe([r for _, r in s]) or 0.0 for s in sims]  # trial SRs for DSR

    # ---- embargoed walk-forward ----
    oos = []          # concatenated out-of-sample per-trade returns
    is_best_srs = []  # the in-sample Sharpe of the config picked each fold
    picks = {}
    di = {d: k for k, d in enumerate(days)}
    start = TRAIN_DAYS
    while start + EMBARGO_DAYS + TEST_DAYS <= len(days):
        train_set = set(days[start - TRAIN_DAYS:start])
        test_set = set(days[start + EMBARGO_DAYS:start + EMBARGO_DAYS + TEST_DAYS])
        best_sr, best_k = None, None
        for k, s in enumerate(sims):
            isr = [r for d, r in s if d in train_set]
            if len(isr) < MIN_IS_TRADES:
                continue
            sr = sharpe(isr)
            if sr is not None and (best_sr is None or sr > best_sr):
                best_sr, best_k = sr, k
        if best_k is not None:
            is_best_srs.append(best_sr)
            oos += [r for d, r in sims[best_k] if d in test_set]
            picks[str(grid[best_k])] = picks.get(str(grid[best_k]), 0) + 1
        start += TEST_DAYS

    # ---- Deflated Sharpe on the OOS series ----
    T = len(oos)
    obs_sr = sharpe(oos)
    if T < 2 or obs_sr is None:
        print("Not enough OOS trades to evaluate."); return
    m = sum(oos) / T; sd = (sum((r - m) ** 2 for r in oos) / T) ** 0.5
    skew = sum((r - m) ** 3 for r in oos) / (T * sd ** 3)
    kurt = sum((r - m) ** 4 for r in oos) / (T * sd ** 4)
    N = len(full_srs)
    var_sr = sum((s - sum(full_srs) / N) ** 2 for s in full_srs) / N
    gamma = 0.5772156649
    sr0 = math.sqrt(var_sr) * ((1 - gamma) * _nppf(1 - 1.0 / N) + gamma * _nppf(1 - 1.0 / (N * math.e)))
    denom = math.sqrt(max(1e-9, 1 - skew * obs_sr + ((kurt - 1) / 4) * obs_sr ** 2))
    dsr = _ncdf((obs_sr - sr0) * math.sqrt(T - 1) / denom)

    # annualization + drawdown
    yrs = (days[-1] - days[0]).days / 365.25
    tpy = T / max(yrs, 0.1)
    ann = obs_sr * math.sqrt(tpy)
    eq = 1.0; peak = 1.0; mdd = 0.0
    for r in oos:
        eq *= (1 + r); peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    wins = sum(1 for r in oos if r > 0)
    is_ann = (sum(is_best_srs) / len(is_best_srs) * math.sqrt(tpy)) if is_best_srs else 0.0

    print(f"\n═══ WALK-FORWARD VALIDATION (SPY {days[0]} → {days[-1]}) ═══")
    print(f"configs tried (N): {N}   folds: {len(is_best_srs)}   OOS trades: {T}")
    print(f"most-picked config: {max(picks, key=picks.get) if picks else 'n/a'}")
    print(f"\nIN-SAMPLE  best annualized Sharpe (avg over folds): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):          {ann:+.2f}   ← the honest number")
    print(f"  overfitting decay (IS→OOS): {is_ann - ann:+.2f} Sharpe")
    print(f"OOS: total return {(eq-1)*100:+.1f}%  ·  win {wins/T*100:.0f}%  ·  maxDD {mdd*100:.1f}%  ·  ~{tpy:.0f} trades/yr")
    print(f"\nDEFLATED SHARPE (multiple-testing + skew/kurt corrected):")
    print(f"  expected max Sharpe under NULL (SR0): {sr0:+.3f}   observed OOS SR: {obs_sr:+.3f}")
    print(f"  skew {skew:+.2f}  kurtosis {kurt:.1f}")
    print(f"  ➤ DSR = {dsr*100:.1f}%   (>95% ⇒ edge is statistically real after the search)")
    verdict = "PASS ✅" if (dsr > 0.95 and ann > 0) else "FAIL ❌"
    print(f"\nVERDICT: {verdict}")
    if verdict.startswith("FAIL"):
        print("  → Do NOT risk real capital. The edge does not survive walk-forward + "
              "multiple-testing + realistic costs. (Consistent with the strategy review.)")


if __name__ == "__main__":
    main()
