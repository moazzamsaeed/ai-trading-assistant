"""HIGH-RISK strangle/straddle backtest + validation gate: deterministic SPY 0DTE
SHORT strangles (sell OTM put + call, NO wings), priced on REAL VIX1D implied
vol, defended by a MECHANICAL INTRADAY STOP, run through walk-forward + Deflated
Sharpe — the same bar that PASSED the iron condor (backtest_vrp.py) and FAILED
the trend engine (validate_strategy.py).

WHY this strategy (the open thread from the 06-18 strategy review):
  • The condor had a REAL, generalizing VRP edge (OOS Sharpe +3.18, DSR 99.8%)
    that DIED on 4-leg execution cost. The highest-leverage fix is FEWER LEGS.
  • A short strangle is 2 legs (half the crossing cost that killed the condor) and
    keeps the FULL credit (no premium paid for wings), ~70% PoP vs ~60% condor.
  • The price: UNDEFINED tail risk. High-risk-tolerance answer = SELF-INSURE with
    a mechanical stop + sizing instead of paying for wings.

The strategy (deterministic, LLM-free):
  • Once/day, ~10:00 ET, sell a SPY 0DTE strangle: short put @ spot−k·EM,
    short call @ spot+k·EM (k=0 ⇒ ATM straddle = max credit, tighter zone).
  • Credit priced via Black-Scholes at the day's REAL VIX1D (so the VRP, IV>realized,
    is captured honestly incl. vol spikes).
  • MECHANICAL STOP: mark the strangle on every 15-min bar (constant entry vol,
    decaying T). If buy-back cost − credit ≥ stop_mult·credit, STOP OUT at that
    bar's actual mark (gaps can realize MORE than the nominal stop — that's the
    modeled tail/slippage). Else hold to expiry, settle at SPY close intrinsic.
  • REGIME FILTER (repurposes our trend/ADX work — the one place direction signal
    is USEFUL): only sell when prior-day Wilder ADX < adx_max (range-bound) AND
    VIX1D < vmax. Stand aside in trends/vol spikes where realized vol blows up the
    short premium.
  • RISK (for sizing/return normalization) = stop_mult·credit (the intended max
    loss). return-on-risk = pnl / risk. Sizing a fraction of capital = that stop.

Validation: embargoed walk-forward (252/5/21) + Deflated Sharpe (deflated by
#configs × skew/kurt) + survivability (negative-skew tail is the whole risk).

Data: data/vix1d.csv (Cboe), SPY 15-min + daily from Alpaca.
Usage: uv run python -m scripts.backtest_strangle [LEG_SPREAD] [N_CROSS]
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
YEAR_MIN = 252 * 390   # trading-time annualization — MUST match VIX1D (trading-time)
LEG_SPREAD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.03   # per-leg bid/ask ($/share)
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 3           # 2 legs in + ~1 leg out (the other expires worthless)
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


def wilder_adx(daily, period=14):
    """Wilder's ADX from daily OHLC bars -> {date: adx} (value as of that day's close)."""
    if len(daily) < period * 2 + 1:
        return {}
    out = {}
    tr = atr = pdi = mdi = None
    sm_tr = sm_pdm = sm_mdm = 0.0
    dxs = []
    for i in range(1, len(daily)):
        h, l, pc = daily[i]["h"], daily[i]["l"], daily[i - 1]["c"]
        ph, pl = daily[i - 1]["h"], daily[i - 1]["l"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        if i <= period:
            sm_tr += tr; sm_pdm += pdm; sm_mdm += mdm
            if i == period:
                atr, s_pdm, s_mdm = sm_tr, sm_pdm, sm_mdm
            continue
        atr = atr - atr / period + tr
        s_pdm = s_pdm - s_pdm / period + pdm
        s_mdm = s_mdm - s_mdm / period + mdm
        if atr <= 0:
            continue
        pdi = 100 * s_pdm / atr; mdi = 100 * s_mdm / atr
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
        dxs.append((daily[i]["d"], dx))
        if len(dxs) == period:
            adx = sum(x for _, x in dxs) / period
            out[daily[i]["d"]] = adx
        elif len(dxs) > period:
            adx = (adx * (period - 1) + dx) / period
            out[daily[i]["d"]] = adx
    return out


def main():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass

    client = ac._stock_client()
    start_dt = datetime(2023, 1, 1, tzinfo=timezone.utc)
    end_dt = datetime(2026, 6, 18, tzinfo=timezone.utc)

    # intraday 15-min for entry spot + intraday stop marking + close
    req = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(TF, TimeFrameUnit.Minute),
                           start=start_dt, end=end_dt, feed=DataFeed.IEX)
    bars = [b for b in client.get_stock_bars(req).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": []})
        rec["close"] = float(b.close)
        mins_to_close = (16 - et.hour) * 60 - et.minute
        if rec["entry"] is None and et.hour == 10 and et.minute == 0:
            rec["entry"] = (float(b.close), mins_to_close)
        if rec["entry"] is not None:   # bars at/after 10:00 -> stop-monitoring path
            rec["path"].append((float(b.close), max(mins_to_close, 0)))

    # daily bars for Wilder ADX regime filter
    dreq = StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Day),
                            start=datetime(2022, 1, 1, tzinfo=timezone.utc), end=end_dt, feed=DataFeed.IEX)
    dbars = sorted(client.get_stock_bars(dreq).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx_by_day = wilder_adx(daily)
    # prior-day ADX (known at entry, no lookahead)
    dlist = [x["d"] for x in daily]
    prior_adx = {}
    for i in range(1, len(dlist)):
        if dlist[i - 1] in adx_by_day:
            prior_adx[dlist[i]] = adx_by_day[dlist[i - 1]]

    days = sorted(d for d, r in byday.items() if r["entry"] and d in vix)

    grid = []
    for k in (0.0, 0.5, 1.0):                # 0 = ATM straddle (max credit), else OTM strangle width
        for stop in (1.5, 2.0, 3.0):         # stop at stop_mult × credit loss
            for adx_max in (100.0, 25.0):    # 100 = filter off; 25 = sell only when range-bound
                for vmax in (40.0, 25.0):    # VIX1D regime cap
                    grid.append({"k": k, "stop": stop, "adx_max": adx_max, "vmax": vmax})

    cost = N_CROSS * (LEG_SPREAD / 2)        # $/share round-trip transaction cost per strangle

    def simulate(cfg):
        trades = []
        for d in days:
            sig = vix[d]
            if sig * 100 > cfg["vmax"]:
                continue
            if cfg["adx_max"] < 100 and prior_adx.get(d, 0.0) >= cfg["adx_max"]:
                continue
            spot, tmin = byday[d]["entry"]; Sclose = byday[d]["close"]
            T0 = max(tmin, 1) / YEAR_MIN
            em = spot * sig * math.sqrt(T0)
            if em <= 0: continue
            Kp = round(spot - cfg["k"] * em); Kc = round(spot + cfg["k"] * em)
            credit = bs(spot, Kp, T0, False, sig) + bs(spot, Kc, T0, True, sig)
            if credit <= 0.05:
                continue
            risk = cfg["stop"] * credit          # intended max loss (the stop)
            stop_level = credit + risk           # buy-back cost that triggers the stop
            # walk intraday bars; stop if mark crosses, marking with constant entry vol
            stopped = False
            for spath, m2c in byday[d]["path"][1:]:   # skip the entry bar itself
                Tt = max(m2c, 0) / YEAR_MIN
                mark = bs(spath, Kp, Tt, False, sig) + bs(spath, Kc, Tt, True, sig)
                if mark >= stop_level:
                    pnl = credit - mark - cost       # realize actual mark (gap can exceed nominal stop)
                    stopped = True
                    break
            if not stopped:
                settle = max(0.0, Kp - Sclose) + max(0.0, Sclose - Kc)
                pnl = credit - settle - cost
            trades.append((d, pnl / risk))
        return trades

    print(f"SHORT-STRANGLE backtest — real VIX1D, 2 legs, cost={N_CROSS}×${LEG_SPREAD/2:.3f}=${cost:.3f}/share, intraday stop")
    print(f"SPY days with VIX1D + 10:00 entry: {len(days)}  ({days[0]} → {days[-1]})  | ADX-filtered days available: {len(prior_adx)}\n")
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
    print(f"\n── THE EDGE (sizing-independent) ──")
    print(f"IN-SAMPLE  best annualized Sharpe (avg/fold): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):     {ann:+.2f}   ← the honest number")
    print(f"  overfitting decay (IS→OOS): {is_ann - ann:+.2f}   |   win {wins/T*100:.0f}%   skew {skew:+.2f}  kurt {kurt:.1f}")
    print(f"DEFLATED SHARPE: SR0(null max) {sr0:+.3f}  observed {obs:+.3f}  ➤ DSR = {dsr*100:.1f}%  (>95% ⇒ real)")
    print(f"\n── SURVIVABILITY (per-trade risk sizing; worst single trade = {worst*100:.0f}% of nominal risk — >100% = gap past stop) ──")
    for f in (0.02, 0.03, 0.05):
        tot, mdd = equity_at(f)
        print(f"  risk {f*100:.0f}%/trade: total {tot*100:+.0f}%  maxDD {mdd*100:.0f}%")
    edge_ok = dsr > 0.95 and ann > 0
    _, mdd3 = equity_at(0.03)
    surv = mdd3 > -0.35
    print(f"\nVERDICT:")
    print(f"  EDGE: {'PASS ✅' if edge_ok else 'FAIL ❌'}  (DSR>95% AND OOS Sharpe>0)")
    print(f"  SURVIVABLE at 3%/trade sizing: {'YES ✅' if surv else 'NO ⚠️ — undefined-tail strangle, gap risk past the stop'}")


if __name__ == "__main__":
    main()
