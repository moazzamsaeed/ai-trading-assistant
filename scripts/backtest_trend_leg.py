"""TREND-LEG test: is there a real, gate-passing edge in being LONG gamma on
high-prior-day-ADX days? — the direction-agnostic complement to the short strangle.

The dual-strategy hypothesis: route by prior-day ADX. Quiet day (ADX<25) -> SHORT
strangle (backtest_strangle.py, PASSES the gate). Trending day (ADX>=threshold) ->
this leg. Instead of BETTING DIRECTION (buy call OR put — the coin-flip that made
the trend engine FAIL the gate: OOS -0.25, DSR 1.3%), we buy a LONG STRADDLE/strangle:
long gamma, profits if the day moves big EITHER way, no direction call.

Same machinery + same bar as backtest_strangle.py:
  • ~10:00 ET on high-ADX days, BUY straddle (k=0 ATM) or strangle (k>0): pay
    both legs' premium, priced via BS at real VIX1D.
  • Optional intraday PROFIT TARGET (sell when mark >= target x premium), else hold
    to expiry; settle at SPY close intrinsic. Max loss = premium paid.
  • Costs = leg crossings. Risk (for return normalization) = premium paid.
  • Run through embargoed walk-forward + Deflated Sharpe.

PASS here => the dual-strategy architecture is validated on evidence (long-gamma on
trend days has edge). FAIL => the trending regime isn't harvestable and the correct
second "strategy" is CASH (strangle on quiet days, flat on trend days).

Usage: uv run python -m scripts.backtest_trend_leg [LEG_SPREAD] [N_CROSS]
"""
from __future__ import annotations
import csv, math, sys
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, sharpe, _ncdf, _nppf, YEAR_MIN

ET = ZoneInfo("America/New_York")
LEG_SPREAD = float(sys.argv[1]) if len(sys.argv) > 1 else 0.04
N_CROSS = int(sys.argv[2]) if len(sys.argv) > 2 else 3   # 2 legs in + ~1 leg out (sell the winner)
TRAIN_DAYS, EMBARGO_DAYS, TEST_DAYS = 252, 5, 21
MIN_IS_TRADES = 20


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
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior_adx)

    grid = []
    for k in (0.0, 0.5):                  # 0=ATM straddle, 0.5=OTM strangle
        for adx_min in (20.0, 25.0, 30.0):  # only trade when prior-day ADX >= this (trend regime)
            for target in (1.5, 2.5, 99.0):  # profit target x premium (99 = hold to close)
                grid.append({"k": k, "adx_min": adx_min, "target": target})
    cost = N_CROSS * (LEG_SPREAD / 2)

    def simulate(cfg):
        trades = []
        for d in days:
            if prior_adx[d] < cfg["adx_min"]:
                continue
            sig = vix[d]; spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]
            T0 = max(tmin, 1) / YEAR_MIN; em = spot * sig * math.sqrt(T0)
            Kp, Kc = round(spot - cfg["k"] * em), round(spot + cfg["k"] * em)
            prem = bs(spot, Kp, T0, False, sig) + bs(spot, Kc, T0, True, sig)
            if prem <= 0.05: continue
            tgt = cfg["target"] * prem; hit = False
            for sp, m2c in byday[d]["path"][1:]:
                Tt = max(m2c, 0) / YEAR_MIN
                mark = bs(sp, Kp, Tt, False, sig) + bs(sp, Kc, Tt, True, sig)
                if mark >= tgt:
                    pnl = mark - prem - cost; hit = True; break
            if not hit:
                settle = max(0.0, Kp - Sc) + max(0.0, Sc - Kc)
                pnl = settle - prem - cost
            trades.append((d, pnl / prem))   # return on premium-at-risk
        return trades

    print(f"TREND LEG (long straddle on high-ADX days) — real VIX1D, cost ${cost:.3f}/sh ({N_CROSS} legs)")
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
    wins = sum(1 for r in oos if r > 0)

    print(f"configs tried (N): {N}   folds: {len(is_best)}   OOS trades: {T}   ~{tpy:.0f}/yr")
    print(f"most-picked config: {max(picks, key=picks.get) if picks else 'n/a'}")
    print(f"\nIN-SAMPLE  best annualized Sharpe (avg/fold): {is_ann:+.2f}")
    print(f"OUT-OF-SAMPLE annualized Sharpe (costed):     {ann:+.2f}   ← the honest number")
    print(f"  overfitting decay (IS→OOS): {is_ann - ann:+.2f}   |   win {wins/T*100:.0f}%   skew {skew:+.2f}  kurt {kurt:.1f}")
    print(f"DEFLATED SHARPE: SR0(null max) {sr0:+.3f}  observed {obs:+.3f}  ➤ DSR = {dsr*100:.1f}%  (>95% ⇒ real)")
    edge_ok = dsr > 0.95 and ann > 0
    print(f"\nVERDICT (trend leg): {'PASS ✅ — long-gamma on trend days has edge; dual-strategy validated' if edge_ok else 'FAIL ❌ — trend regime not harvestable; second strategy should be CASH'}")


if __name__ == "__main__":
    main()
