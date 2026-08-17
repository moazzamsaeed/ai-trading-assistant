"""Does a TIGHTER intraday stop help the condor cut trend days faster, or does
whipsaw (stopping days that would have recovered) cost more than it saves?

Holds the LIVE config fixed — gate VIX1D<35 & prior-day ADX<27, strikes at 0.5x
the VIX1D expected move, $5 wings — and sweeps ONLY the stop multiplier
(buy-back >= (1+stop)*credit). Same mechanics/costs as backtest_wide_condor.py.
Full-sample (in-sample) comparison; P&L is normalised as return/risk ("R", where
-1.0 = the full defined-risk max loss on that trade).

Usage: .venv/bin/python -m scripts.backtest_condor_stop_sweep
"""
from __future__ import annotations
import csv, math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, sharpe, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0                 # live strikes: 0.5x EM, $5 wings
ADX_MAX, VMAX = 27.0, 35.0      # live gate
LEG_SPREAD, N_CROSS = 0.04, 6
COST = N_CROSS * (LEG_SPREAD / 2)
STOPS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 99.0]   # 99 = no stop (rely on wings)


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
    return byday, vix, prior_adx, days


def simulate(stop, byday, vix, prior_adx, days):
    """Return list of (return/risk, was_stopped) for each traded day."""
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
        credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig)
        risk = W - credit
        if credit <= 0.05 or risk <= 0.05: continue
        sl = credit + stop * credit; stopped = False; pnl = None
        for sp, m2c in byday[d]["path"][1:]:
            Tt = max(m2c, 0) / YEAR_MIN
            mark = condor_mark(sp, Kp, Kpl, Kc, Kcl, Tt, sig)
            if mark >= sl:
                pnl = credit - mark - COST; stopped = True; break
        if not stopped:
            put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc)
            call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
            pnl = credit - put_s - call_s - COST
        out.append((pnl / risk, stopped))
    return out


def main():
    byday, vix, prior_adx, days = load()
    print(f"SPY {days[0]} → {days[-1]}  |  gate VIX1D<{VMAX:.0f} & ADX<{ADX_MAX:.0f}, 0.5xEM, ${W:.0f} wings")
    print(f"cost ${COST:.3f}/sh  |  P&L in R = return/risk (-1.0 = full max loss)\n")
    hdr = f"{'stop x':>7} | {'trades':>6} | {'win%':>5} | {'Sharpe':>7} | {'total R':>8} | {'avg R':>7} | {'worst':>6} | {'avg loss(losers)':>16} | {'stopped%':>8}"
    print(hdr); print("-" * len(hdr))
    base = None
    for stop in STOPS:
        res = simulate(stop, byday, vix, prior_adx, days)
        rs = [r for r, _ in res]
        n = len(rs); wins = sum(1 for r in rs if r > 0)
        losers = [r for r in rs if r <= 0]
        shp = sharpe(rs) or 0.0
        tot = sum(rs); avg = tot / n if n else 0
        worst = min(rs) if rs else 0
        avg_loss = sum(losers) / len(losers) if losers else 0
        stopped_pct = 100 * sum(1 for _, s in res if s) / n if n else 0
        label = f"{stop:>6.2f}" if stop < 90 else "  none"
        mark = "  <- LIVE" if abs(stop - 1.5) < 1e-9 else ""
        print(f"{label} | {n:6d} | {100*wins/n:4.0f}% | {shp:+7.2f} | {tot:+8.1f} | {avg:+7.3f} | {worst:+6.2f} | {avg_loss:+16.3f} | {stopped_pct:7.0f}%{mark}")
    print("\nReading: 'total R' is the sum of return/risk across all trades (higher = more")
    print("profit per unit risk). A tighter stop helps ONLY if total R and Sharpe rise as")
    print("the stop tightens; if they fall, whipsaw (cutting recoverable days) costs more")
    print("than the trend-day protection saves.")


if __name__ == "__main__":
    main()
