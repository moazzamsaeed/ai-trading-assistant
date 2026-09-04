"""Is the 1.5x condor stop too tight? Sweep the stop multiplier and, for every
day the stop FIRES, classify it as SAVED (holding would've been worse -> stop
earned its keep) vs WHIPSAW (holding would've been better -> stop cost money).

Reuses the calibrated engine (strikes x1.45 EM = live 0.48% width, credit $0.35).
CLEAN mode (no assignment) isolates the stop-vs-hold question; worst-day still
reflects what the stop protects against (a gradual breach settling at the wing).

Usage: .venv/bin/python -m scripts.backtest_condor_stop_whipsaw
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
CREDIT = 0.35
POOL = 25000.0


def cm(s, Kp, Kpl, Kc, Kcl, T, sig):
    return bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig)


def load():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()] = float(r["OPEN"])/100.0
        except Exception: pass
    cl = ac._stock_client(); end = datetime(2026,6,18,tzinfo=timezone.utc)
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
    return byday, vix, prior


def sim_day(d, byday, vix, prior, stop):
    """Return dict with hold_pnl, stop_pnl (chosen outcome given `stop`), fired, saved."""
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)*EM_CAL
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    bscred=cm(spot,Kp,Kpl,Kc,Kcl,T0,sig)
    if bscred<=0.05: return None
    put_s=max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); call_s=max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    hold_sh = CREDIT - put_s - call_s          # clean settle, no assignment
    hold = hold_sh*100*CT
    # does the stop fire? mark >= (1+stop)*bscred somewhere on the path
    fired=False
    if stop is not None:
        sl=bscred*(1+stop)
        for sp,m2c in byday[d]["path"][1:]:
            if cm(sp,Kp,Kpl,Kc,Kcl,max(m2c,0)/YEAR_MIN,sig)>=sl:
                fired=True; break
    if fired:
        stop_pnl=(CREDIT-stop*CREDIT)*100*CT     # exit ~ -stop x credit
    else:
        stop_pnl=hold
    saved = fired and (stop_pnl > hold)          # holding would've been worse
    return {"hold":hold,"pnl":stop_pnl,"fired":fired,"saved":saved,
            "delta": stop_pnl-hold if fired else 0.0}


def main():
    byday,vix,prior = load()
    days=[d for d in prior if d in byday and byday[d]["entry"] and d in vix]
    print("stop | total$      worst$    win% | fires  saved  whip | stop_saved$  whip_cost$  net$")
    print("-"*92)
    for stop in [None,1.0,1.5,2.0,2.5,3.0]:
        tot=worst=0.0; n=wins=fires=saved=whip=0; save_amt=whip_amt=0.0
        for d in days:
            r=sim_day(d,byday,vix,prior,stop)
            if r is None: continue
            n+=1; tot+=r["pnl"]; worst=min(worst,r["pnl"]); wins+=r["pnl"]>0
            if r["fired"]:
                fires+=1
                if r["saved"]: saved+=1; save_amt+=r["delta"]
                else: whip+=1; whip_amt+=r["delta"]   # delta<0 = cost
        net=save_amt+whip_amt
        lbl="none" if stop is None else f"{stop:.1f}x"
        print(f"{lbl:>4} | {tot:+9,.0f} {worst:+9,.0f} {100*wins/n:4.0f}% | {fires:4}  {saved:4}  {whip:4} | "
              f"{save_amt:+10,.0f}  {whip_amt:+9,.0f}  {net:+8,.0f}")
    print(f"\n{n} gate-passing days, 2023-01 .. 2026-06.  CLEAN (no assignment).")
    print("stop_saved$ = money the stop rescued on breach days that kept going;")
    print("whip_cost$  = money the stop gave up on days that recovered (whipsaws);")
    print("net$        = stop_saved + whip_cost (positive => the stop is net-additive at that level).")


if __name__ == "__main__":
    main()
