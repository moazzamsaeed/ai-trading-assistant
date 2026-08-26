"""Would an event-day blackout (skip condor entry on FOMC/CPI/NFP) catch the
assignment tail days — and what would it cost in foregone premium?

Compares the live condor (gate VIX1D<35 & ADX<27) WITH vs WITHOUT an event-day
entry blackout, 2023–2026. Reports: how many of the tail days (loss > wing cap)
land on event days, and the net P&L effect of the blackout (premium given up on
the good event days you'd skip, minus the tail days you'd dodge).

NOTE: historical FOMC dates are the published schedule; CPI dates are reconstructed
(BLS releases, ~mid-month) and may be ±1 day — treat the CPI matches as indicative.
NFP is computed deterministically (first Friday of the month).

Usage: .venv/bin/python -m scripts.backtest_event_blackout
"""
from __future__ import annotations
import csv, math, calendar
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0
ADX_MAX, VMAX = 27.0, 35.0
COST = 6 * (0.04 / 2)
REAL_MAXLOSS_CT = 452.0
CONTRACTS = 28

FOMC = {  # published decision days
    date(2023,2,1),date(2023,3,22),date(2023,5,3),date(2023,6,14),date(2023,7,26),date(2023,9,20),date(2023,11,1),date(2023,12,13),
    date(2024,1,31),date(2024,3,20),date(2024,5,1),date(2024,6,12),date(2024,7,31),date(2024,9,18),date(2024,11,7),date(2024,12,18),
    date(2025,1,29),date(2025,3,19),date(2025,5,7),date(2025,6,18),date(2025,7,30),date(2025,9,17),date(2025,10,29),date(2025,12,10),
    date(2026,1,28),date(2026,3,18),date(2026,5,6),date(2026,6,17),
}
CPI = {  # BLS ~mid-month — RECONSTRUCTED, may be +-1 day
    date(2023,1,12),date(2023,2,14),date(2023,3,14),date(2023,4,12),date(2023,5,10),date(2023,6,13),date(2023,7,12),date(2023,8,10),date(2023,9,13),date(2023,10,12),date(2023,11,14),date(2023,12,12),
    date(2024,1,11),date(2024,2,13),date(2024,3,12),date(2024,4,10),date(2024,5,15),date(2024,6,12),date(2024,7,11),date(2024,8,14),date(2024,9,11),date(2024,10,10),date(2024,11,13),date(2024,12,11),
    date(2025,1,15),date(2025,2,12),date(2025,3,12),date(2025,4,10),date(2025,5,13),date(2025,6,11),date(2025,7,15),date(2025,8,12),date(2025,9,11),date(2025,10,15),date(2025,11,13),date(2025,12,10),
    date(2026,1,14),date(2026,2,11),date(2026,3,11),date(2026,4,10),date(2026,5,13),date(2026,6,11),
}
ELECTION = {date(2024,11,5)}


def nfp_days(y1, y2):
    out = set()
    for y in range(y1, y2 + 1):
        for m in range(1, 13):
            # first Friday
            d = date(y, m, 1)
            while d.weekday() != 4:
                d += timedelta(days=1)
            out.add(d)
    return out

NFP = nfp_days(2023, 2026)


def event_name(d):
    if d in FOMC: return "FOMC"
    if d in CPI: return "CPI"
    if d in NFP: return "NFP"
    if d in ELECTION: return "Election"
    return None


def cmark(s, Kp, Kpl, Kc, Kcl, T, sig):
    return bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig)


def load():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()] = float(r["OPEN"])/100.0
        except Exception: pass
    cl = ac._stock_client(); end = datetime(2026,6,18,tzinfo=timezone.utc)
    bars=[b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
        timeframe=TimeFrame(15,TimeFrameUnit.Minute), start=datetime(2023,1,1,tzinfo=timezone.utc),
        end=end, feed=DataFeed.IEX)).data.get("SPY",[]) if is_rth(b.timestamp)]
    byday={}
    for b in bars:
        et=b.timestamp.astimezone(ET); d=et.date(); rec=byday.setdefault(d,{"entry":None,"close":None,"open":None,"path":[]})
        c=float(b.close); m2c=(16-et.hour)*60-et.minute
        if rec["open"] is None: rec["open"]=c
        rec["close"]=c
        if rec["entry"] is None and et.hour==10 and et.minute==0: rec["entry"]=(c,m2c)
        if rec["entry"] is not None: rec["path"].append((c,max(m2c,0)))
    dbars=sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
        timeframe=TimeFrame(1,TimeFrameUnit.Day), start=datetime(2022,1,1,tzinfo=timezone.utc),
        end=end, feed=DataFeed.IEX)).data.get("SPY",[]), key=lambda b:b.timestamp)
    daily=[{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx=wilder_adx(daily); dl=[x["d"] for x in daily]
    prior={dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    days=sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior)
    nextopen={days[i]:byday[days[i+1]]["open"] for i in range(len(days)-1)}
    return byday, vix, prior, days, nextopen


def trade_R(d, byday, vix, prior, nextopen):
    """Return (R, is_tail) for a gate-passing day, or None if gate blocks/no-trade."""
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    credit=cmark(spot,Kp,Kpl,Kc,Kcl,T0,sig); risk=W-credit
    if credit<=0.05 or risk<=0.05: return None
    put_s=max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); call_s=max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    pnl=credit-put_s-call_s-COST
    if Kpl<Sc<Kp or Kc<Sc<Kcl:
        no=nextopen.get(d)
        if no is not None:
            pnl += (no-Sc) if Kpl<Sc<Kp else (Sc-no)
    R=pnl/risk
    return R, (R < -1.0)


def main():
    byday, vix, prior, days, nextopen = load()
    traded=[]  # (d, R, is_tail, event)
    for d in days:
        r=trade_R(d, byday, vix, prior, nextopen)
        if r is None: continue
        traded.append((d, r[0], r[1], event_name(d)))
    n=len(traded)
    tails=[t for t in traded if t[2]]
    ev_traded=[t for t in traded if t[3]]
    ev_tails=[t for t in tails if t[3]]
    def dollars(R): return R*REAL_MAXLOSS_CT*CONTRACTS
    print(f"traded {n} days | tail days (R<-1.0) {len(tails)} | on event days: {len(ev_tails)}\n")
    print(f"=== tail days on scheduled events ({len(ev_tails)} of {len(tails)}) ===")
    for d,R,_,ev in sorted(ev_tails, key=lambda x:x[1]):
        print(f"  {d}  {R:+.2f}R (~${dollars(R):+,.0f})  [{ev}]")
    print(f"\n=== the {len(tails)-len(ev_tails)} tail days NOT on any scheduled event (random gaps) ===")
    non=[t for t in tails if not t[3]]
    for d,R,_,_ in sorted(non, key=lambda x:x[1])[:8]:
        print(f"  {d}  {R:+.2f}R (~${dollars(R):+,.0f})  [no event]")
    if len(non)>8: print(f"  ... +{len(non)-8} more")
    # cost/benefit of the blackout
    saved = -sum(dollars(t[1]) for t in ev_tails)               # tail losses dodged (positive = saved)
    given_up = sum(dollars(t[1]) for t in ev_traded if not t[2])# premium on the GOOD event days skipped
    net = saved + given_up   # given_up is + (P&L we forgo, mostly positive) -> subtract; but sum already signed
    print(f"\n=== blackout cost/benefit (skips ALL {len(ev_traded)} event days the condor would trade) ===")
    print(f"  event days traded: {len(ev_traded)}  ({len(ev_tails)} tail, {len(ev_traded)-len(ev_tails)} non-tail)")
    print(f"  tail losses DODGED by blackout:   ~${saved:+,.0f}")
    print(f"  premium GIVEN UP on good event days: ~${sum(dollars(t[1]) for t in ev_traded if not t[2]):+,.0f}")
    print(f"  NET effect of the blackout:        ~${sum(dollars(t[1]) for t in ev_traded)*-1:+,.0f}")
    print(f"    (= −[total P&L on all event days]; positive = blackout HELPS)")
    print(f"\ncoverage: blackout catches {len(ev_tails)}/{len(tails)} tail days "
          f"({100*len(ev_tails)/len(tails):.0f}%); {len(non)} remain as unpredictable random gaps.")


if __name__ == "__main__":
    main()
