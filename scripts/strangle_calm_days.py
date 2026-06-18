"""The fair mirror image: run the deterministic short strangle on EVERY market day
in the span of our directional trading (2026-05-12 -> 06-18), and show what it did
on the CALM days its regime filter actually wants — most of which we sat out.

Strangle's edge lives on range-bound/low-ADX days; our directional engine fired on
trending days. This shows the other side of the coin: real VIX1D, real SPY path,
winning config, realistic cost, 1 lot.
"""
from __future__ import annotations
import csv, math, sqlite3
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, STOP, ADX_MAX, VMAX = 0.5, 1.5, 25.0, 40.0
LEG_SPREAD, N_CROSS = 0.04, 3
COST = N_CROSS * (LEG_SPREAD / 2)
SPAN_LO, SPAN_HI = datetime(2026, 1, 1).date(), datetime(2026, 6, 18).date()
ONLY_TRADED = True   # suppress ASIDE rows to spotlight the calm days it actually traded


def main():
    c = sqlite3.connect("data/trademaster.db")
    our_days = {datetime.fromisoformat(o).date() for (o,) in
                c.execute("SELECT opened_at FROM trades WHERE strategy LIKE 'directional%'")}

    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"], "%m/%d/%Y").date()] = float(r["OPEN"]) / 100.0
        except Exception: pass
    cl = ac._stock_client()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc); end = datetime(2026, 6, 18, tzinfo=timezone.utc)
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=start, end=end, feed=DataFeed.IEX
            )).data.get("SPY", []) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry": None, "close": None, "path": []})
        rec["close"] = float(b.close); m2c = (16 - et.hour) * 60 - et.minute
        if rec["entry"] is None and et.hour == 10 and et.minute == 0: rec["entry"] = (float(b.close), m2c)
        if rec["entry"] is not None: rec["path"].append((float(b.close), max(m2c, 0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
            timeframe=TimeFrame(1, TimeFrameUnit.Day), start=datetime(2025, 6, 1, tzinfo=timezone.utc),
            end=end, feed=DataFeed.IEX)).data.get("SPY", []), key=lambda b: b.timestamp)
    daily = [{"d": b.timestamp.astimezone(ET).date(), "h": float(b.high), "l": float(b.low), "c": float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl = [x["d"] for x in daily]
    prior_adx = {dl[i]: adx[dl[i-1]] for i in range(1, len(dl)) if dl[i-1] in adx}

    def strangle(d):
        sig = vix.get(d); rec = byday.get(d)
        if sig is None or not rec or not rec["entry"]: return None
        padx = prior_adx.get(d)
        if sig * 100 > VMAX or (padx is not None and padx >= ADX_MAX): return ("ASIDE", padx, sig, 0.0, "")
        spot, tmin = rec["entry"]; Sc = rec["close"]; T0 = max(tmin, 1) / YEAR_MIN
        em = spot * sig * math.sqrt(T0); Kp, Kc = round(spot - K * em), round(spot + K * em)
        credit = bs(spot, Kp, T0, False, sig) + bs(spot, Kc, T0, True, sig)
        if credit <= 0.05: return ("ASIDE", padx, sig, 0.0, "")
        risk = STOP * credit; stop_level = credit + risk
        for sp, m2c in rec["path"][1:]:
            Tt = max(m2c, 0) / YEAR_MIN
            if bs(sp, Kp, Tt, False, sig) + bs(sp, Kc, Tt, True, sig) >= stop_level:
                mark = bs(sp, Kp, Tt, False, sig) + bs(sp, Kc, Tt, True, sig)
                return ("STOPPED", padx, sig, (credit - mark - COST) * 100, f"K{Kp:.0f}/{Kc:.0f} cr${credit:.2f}")
        settle = max(0.0, Kp - Sc) + max(0.0, Sc - Kc)
        return ("EXPIRED", padx, sig, (credit - settle - COST) * 100, f"K{Kp:.0f}/{Kc:.0f} cr${credit:.2f}")

    days = sorted(d for d in byday if SPAN_LO <= d <= SPAN_HI and byday[d]["entry"])
    print(f"Strangle config k={K} stop{STOP}x ADX<{ADX_MAX:.0f} VIX1D<{VMAX:.0f} | cost ${COST:.3f}/sh | 1 lot")
    print(f"Span {SPAN_LO} -> {SPAN_HI}  ({len(days)} market days)\n")
    print(f"{'date':11} {'us?':5} {'pADX':>5} {'VIX1D':>6}  {'strangle':9} {'P&L':>7}  detail")
    print("-" * 72)
    tot = wins = traded = 0
    calm_sat_pnl = calm_sat_n = 0
    for d in days:
        s = strangle(d)
        us = "TRADE" if d in our_days else "sat"
        if s is None: continue
        st, padx, sig, pnl, det = s
        padx_s = f"{padx:.0f}" if padx is not None else "—"
        sig_s = f"{sig*100:.0f}" if sig else "—"
        if st in ("STOPPED", "EXPIRED"):
            tot += pnl; traded += 1; wins += 1 if pnl > 0 else 0
            if d not in our_days: calm_sat_pnl += pnl; calm_sat_n += 1
            print(f"{str(d):11} {us:5} {padx_s:>5} {sig_s:>6}  {st:9} {pnl:>+7.0f}  {det}")
        elif not ONLY_TRADED:
            print(f"{str(d):11} {us:5} {padx_s:>5} {sig_s:>6}  {'ASIDE':9} {'—':>7}")
    print("-" * 72)
    print(f"\nStrangle TRADED {traded}/{len(days)} days in the span (filter let it in on the calm ones).")
    print(f"  total {tot:+.0f}, win {wins}/{traded} ({wins/max(traded,1)*100:.0f}%)")
    print(f"Of those, the CALM days WE SAT OUT: {calm_sat_n} days, strangle P&L {calm_sat_pnl:+.0f}")
    print(f"\nContext: our directional engine over this same span = -4,846 (20 trade-days, the trending ones).")


if __name__ == "__main__":
    main()
