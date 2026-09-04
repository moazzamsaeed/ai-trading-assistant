"""Side-by-side full-sample backtest: wider 2.5x stop vs a DISTANCE-AWARE stop,
against HOLD and the current 1.5x. Quantifies the whipsaw-vs-tail trade.

Unified model so all variants are apples-to-apples:
  - per-day THIN credit c = 0.58*bscred (realized fills sit below BS-fair; keeps the
    thin-day = early-trigger whipsaw mechanism, and varies calm vs rich days).
  - P&L normalized to 0 at entry: loss(t) = BS-mark(t) - BS-mark(entry). A stop that
    fires deeper books a bigger loss -> captures the trade-off honestly.
  - credit stops (1.5x / 2.5x): fire when loss >= stop * c  (location-independent).
  - distance-aware: fire when SPY reaches/through a short strike (location), booking
    the loss at that bar. Fires deeper than the 1.5x approach -> bigger per-fire loss
    but far fewer whipsaws.

Absolute $ BS-inflated -> read variants RELATIVE to each other (esp. worst-day).
Usage: .venv/bin/python -m scripts.backtest_condor_stop_compare
"""
from __future__ import annotations
import csv, math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0
ADX_MAX, VMAX = 27.0, 35.0
CT = 28
EM_CAL = 1.45
SHRINK = 0.58        # realized credit / BS-fair credit
POOL = 25000.0


def cm(s,Kp,Kpl,Kc,Kcl,T,sig):
    return bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig)


def load():
    vix={}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()]=float(r["OPEN"])/100.0
        except Exception: pass
    cl=ac._stock_client(); end=datetime(2026,6,18,tzinfo=timezone.utc)
    bars=[b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(5,TimeFrameUnit.Minute),
        start=datetime(2023,1,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]) if is_rth(b.timestamp)]
    byday={}
    for b in bars:
        et=b.timestamp.astimezone(ET); d=et.date(); rec=byday.setdefault(d,{"entry":None,"close":None,"path":[]})
        c=float(b.close); m2c=(16-et.hour)*60-et.minute
        rec["close"]=c
        if rec["entry"] is None and et.hour==10 and et.minute==0: rec["entry"]=(c,m2c)
        if rec["entry"] is not None: rec["path"].append((c,max(m2c,0)))
    dbars=sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(1,TimeFrameUnit.Day),
        start=datetime(2022,1,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]),key=lambda b:b.timestamp)
    daily=[{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx=wilder_adx(daily); dl=[x["d"] for x in daily]
    prior={dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    return byday,vix,prior


def sim_day(d,byday,vix,prior,kind,param):
    """kind: 'hold' | 'credit' (param=stop mult) | 'dist' (param=trigger frac, <0=through).
    Returns (pnl$, fired, hold$)."""
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)*EM_CAL
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    bscred=cm(spot,Kp,Kpl,Kc,Kcl,T0,sig)
    if bscred<=0.05: return None
    c=SHRINK*bscred                       # per-day thin realized credit
    m0=bscred                             # BS mark at entry (normalization base)
    put_s=max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); call_s=max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    hold=(c-put_s-call_s)*100*CT
    if kind=="hold":
        return (hold,False,hold)
    for sp,m2c in byday[d]["path"][1:]:
        mk=cm(sp,Kp,Kpl,Kc,Kcl,max(m2c,0)/YEAR_MIN,sig)
        rise=mk-m0                        # loss-to-date (normalized)
        if kind=="credit":
            fire = rise>=param*c
        else:  # dist
            fire = (sp>=Kc*(1-param) or sp<=Kp*(1+param))
        if fire:
            return ((c-c-rise)*100*CT, True, hold)   # pnl = -rise*100*CT
    return (hold,False,hold)


def run(byday,vix,prior,kind,param):
    tot=worst=0.0; n=w=0; fires=whip=save=0
    for d in prior:
        if d not in byday or byday[d]["entry"] is None or d not in vix: continue
        r=sim_day(d,byday,vix,prior,kind,param)
        if r is None: continue
        pnl,fired,hold=r; tot+=pnl; worst=min(worst,pnl); n+=1; w+=pnl>0
        if fired:
            fires+=1
            if pnl<hold: whip+=1
            elif pnl>hold: save+=1
    return dict(tot=tot,worst=worst,n=n,win=w,fires=fires,whip=whip,save=save)


def main():
    byday,vix,prior=load()
    variants=[("HOLD (no stop)","hold",None),
              ("FLAT 1.5x (current)","credit",1.5),
              ("FLAT 2.5x (wider)","credit",2.5),
              ("DIST-AWARE @ strike","dist",0.0),
              ("DIST-AWARE thru 0.05%","dist",-0.0005),
              ("DIST-AWARE thru 0.10%","dist",-0.0010)]
    print(f"{'variant':>24} | {'total$':>10} | {'worst$':>9} {'%25k':>5} | {'win%':>4} | {'fires':>5} {'whip':>4} {'save':>4}")
    print("-"*86)
    for lbl,kind,p in variants:
        r=run(byday,vix,prior,kind,p)
        print(f"{lbl:>24} | {r['tot']:+10,.0f} | {r['worst']:+9,.0f} {r['worst']/POOL*100:+4.0f}% | {100*r['win']/r['n']:4.0f} | {r['fires']:5} {r['whip']:4} {r['save']:4}")
    print(f"\n{run(byday,vix,prior,'hold',None)['n']} gate-passing days 2023->2026-06. thin credit=0.58*BS-fair.")
    print("worst$ = the tail (what the stop protects). whip=fires that closed better if held; save=fires that avoided worse.")
    print("READ: FLAT 2.5x = wider credit stop (fewer whipsaws, WORSE worst-day). DIST-AWARE = fire on breach (fewer")
    print("whipsaws WHILE keeping the worst-day cap). Absolute $ BS-inflated -> compare columns, not magnitudes.")


if __name__ == "__main__":
    main()
