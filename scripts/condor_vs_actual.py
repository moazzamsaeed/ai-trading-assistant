"""How the WIDE IRON CONDOR (winning config) would have done on the exact days we
placed directional trades — real DB trades vs condor on the same day (real VIX1D,
real SPY path). 1 condor lot, realistic cost. Plus the calm-day mirror (days we
sat out, where the condor actually trades).
"""
from __future__ import annotations
import csv, math, sqlite3, json
from datetime import datetime, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W, STOP, ADX_MAX, VMAX = 0.5, 5.0, 1.5, 25.0, 40.0   # winning condor config
LEG_SPREAD, N_CROSS = 0.04, 6
COST = N_CROSS * (LEG_SPREAD / 2)


def condor_mark(spot, Kp, Kpl, Kc, Kcl, T, sig):
    return (bs(spot, Kp, T, False, sig) - bs(spot, Kpl, T, False, sig)
            + bs(spot, Kc, T, True, sig) - bs(spot, Kcl, T, True, sig))


def main():
    c = sqlite3.connect("data/trademaster.db")
    actual = defaultdict(lambda: {"pnl": 0.0, "n": 0, "sides": []})
    for opened, pnl, side, extra in c.execute(
        "SELECT opened_at, realized_pnl_usd, side, extra FROM trades WHERE strategy LIKE 'directional%'"):
        d = datetime.fromisoformat(opened).date()
        actual[d]["pnl"] += (pnl or 0.0); actual[d]["n"] += 1
        try: actual[d]["sides"].append(json.loads(extra).get("action", side))
        except Exception: actual[d]["sides"].append(side)
    trade_days = sorted(actual)

    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass
    cl = ac._stock_client()
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": []})
        rec["close"] = float(b.close); m2c = (16 - et.hour) * 60 - et.minute
        if rec["entry"] is None and et.hour == 10 and et.minute == 0: rec["entry"] = (float(b.close), m2c)
        if rec["entry"] is not None: rec["path"].append((float(b.close), max(m2c, 0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=datetime(2025, 6, 1, tzinfo=timezone.utc),
            end=datetime(2026, 6, 18, tzinfo=timezone.utc), feed=DataFeed.IEX)).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl = [x["d"] for x in daily]
    prior_adx = {dl[i]: adx[dl[i-1]] for i in range(1, len(dl)) if dl[i-1] in adx}

    def condor(d, use_filter=True):
        sig = vix.get(d); rec = byday.get(d)
        if sig is None or not rec or not rec["entry"]: return None
        pa = prior_adx.get(d)
        if use_filter and (sig * 100 > VMAX or (pa is not None and pa >= ADX_MAX)):
            why = []
            if sig * 100 > VMAX: why.append(f"VIX1D {sig*100:.0f}")
            if pa is not None and pa >= ADX_MAX: why.append(f"ADX {pa:.0f}")
            return ("ASIDE", 0.0, "; ".join(why))
        spot, tmin = rec["entry"]; Sc = rec["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0)
        Kp = round(spot - K * em); Kpl = Kp - W; Kc = round(spot + K * em); Kcl = Kc + W
        credit = condor_mark(spot, Kp, Kpl, Kc, Kcl, T0, sig); risk = W - credit
        if credit <= 0.05 or risk <= 0.05: return ("no-trade", 0.0, "")
        sl = credit + STOP * credit
        for sp, m2c in rec["path"][1:]:
            Tt = max(m2c, 0) / YEAR_MIN
            mk = condor_mark(sp, Kp, Kpl, Kc, Kcl, Tt, sig)
            if mk >= sl:
                return ("STOPPED", (credit - mk - COST) * 100, f"K{Kp:.0f}/{Kc:.0f} W{W:.0f} cr${credit:.2f}")
        put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc); call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
        return ("EXPIRED", (credit - put_s - call_s - COST) * 100, f"K{Kp:.0f}/{Kc:.0f} W{W:.0f} cr${credit:.2f}")

    print(f"Condor config: k={K} wings ${W:.0f} stop {STOP}x ADX<{ADX_MAX:.0f} VIX1D<{VMAX:.0f} | cost ${COST:.2f}/sh (6 legs) | 1 lot\n")
    print(f"{'date':11} {'our trades':26} {'our P&L':>8} | {'condor(filtered)':17} {'P&L':>6} | {'no-filter':9} {'P&L':>6}")
    print("-" * 100)
    tot_a = tot_f = tot_u = nf = nu = wf = wu = 0
    for d in trade_days:
        a = actual[d]; sides = ",".join(s.replace("BUY_", "") for s in a["sides"]); tot_a += a["pnl"]
        f = condor(d, True); u = condor(d, False)
        if f is None: fcol = f"{'no data':17} {'':>6}"
        elif f[0] in ("STOPPED", "EXPIRED"):
            tot_f += f[1]; nf += 1; wf += 1 if f[1] > 0 else 0; fcol = f"{f[0]:17} {f[1]:>+6.0f}"
        else: fcol = f"{'STAND ASIDE':17} {'—':>6}"
        if u is None: ucol = f"{'no data':9} {'':>6}"
        elif u[0] in ("STOPPED", "EXPIRED"):
            tot_u += u[1]; nu += 1; wu += 1 if u[1] > 0 else 0; ucol = f"{u[0]:9} {u[1]:>+6.0f}"
        else: ucol = f"{u[0]:9} {'—':>6}"
        print(f"{str(d):11} {sides[:26]:26} {a['pnl']:>+8.0f} | {fcol} | {ucol}")
    print("-" * 100)
    print(f"\nOUR directional:        {len(trade_days)} trade-days, total {tot_a:+.0f}")
    print(f"CONDOR (filtered):      traded {nf}/{len(trade_days)} (stood aside {len(trade_days)-nf}), total {tot_f:+.0f}, win {wf}/{nf}")
    print(f"CONDOR (no filter):     forced {nu}/{len(trade_days)}, total {tot_u:+.0f}, win {wu}/{nu} ({wu/max(nu,1)*100:.0f}%)")

    # calm-day mirror: every 2026 day, condor where its filter trades, split by whether we traded
    our = set(trade_days)
    tot = wins = n = sat_pnl = sat_n = 0
    for d in sorted(byday):
        r = condor(d, True)
        if r is None or r[0] not in ("STOPPED", "EXPIRED"): continue
        tot += r[1]; n += 1; wins += 1 if r[1] > 0 else 0
        if d not in our: sat_pnl += r[1]; sat_n += 1
    print(f"\n— MIRROR (where the condor actually trades, 2026 YTD) —")
    print(f"CONDOR traded {n} calm days, total {tot:+.0f}, win {wins}/{n} ({wins/max(n,1)*100:.0f}%)")
    print(f"  of which CALM DAYS WE SAT OUT: {sat_n} days, {sat_pnl:+.0f}")


if __name__ == "__main__":
    main()
