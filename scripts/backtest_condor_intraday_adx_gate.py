"""Full-sample backtest: add an INTRADAY 15-min ADX entry gate on top of the live
gates (VIX1D<35 & prior-day daily ADX<27), sweep the threshold, and measure the
net trade-off. The 9/01-9/03 losses showed intraday 15m ADX@10:00 = 35.6/31.9/25.0
while daily ADX/VIX1D stayed calm — so does gating on it help ACROSS the sample,
or just create a dead-zone (veto more winners than losers)?

Calibrated engine (strikes x1.45 EM = live 0.48% width, credit $0.35, 1.5x stop,
CLEAN no-assignment so the stop caps trend breaches). Absolute $ inflated -> trust
shape/ratios, not dollar totals.

For each threshold: total P&L, worst day, win%, #traded, #blocked, and of the blocked
days how many were losers (rightly avoided) vs winners (premium forfeited) + net.

Usage: .venv/bin/python -m scripts.backtest_condor_intraday_adx_gate
"""
from __future__ import annotations
import csv, math
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
K, W = 0.5, 5.0
ADX_MAX, VMAX = 27.0, 35.0
STOP = 1.5
CT = 28
EM_CAL = 1.45
CREDIT = 0.35
POOL = 25000.0


def cm(s,Kp,Kpl,Kc,Kcl,T,sig):
    return bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig)


def wilder_adx_series(H,L,C,period=14):
    n=len(C); tr=[0.0]*n; pdm=[0.0]*n; ndm=[0.0]*n
    for i in range(1,n):
        up=H[i]-H[i-1]; dn=L[i-1]-L[i]
        pdm[i]=up if (up>dn and up>0) else 0.0
        ndm[i]=dn if (dn>up and dn>0) else 0.0
        tr[i]=max(H[i]-L[i], abs(H[i]-C[i-1]), abs(L[i]-C[i-1]))
    def rma(x):
        out=[None]*n
        if n<=period: return out
        s=sum(x[1:period+1]); out[period]=s
        for i in range(period+1,n): s=s-(s/period)+x[i]; out[i]=s
        return out
    atr=rma(tr); ps=rma(pdm); ns=rma(ndm); dx=[None]*n
    for i in range(n):
        if atr[i] and atr[i]>0 and ps[i] is not None:
            pdi=100*ps[i]/atr[i]; ndi=100*ns[i]/atr[i]
            dx[i]=100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0.0
    adx=[None]*n; start=2*period
    if n>start:
        first=[d for d in dx[period:start] if d is not None]
        if first:
            a=sum(first)/len(first); adx[start-1]=a
            for i in range(start,n):
                if dx[i] is not None: a=(a*(period-1)+dx[i])/period; adx[i]=a
    return adx


def load():
    vix={}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()]=float(r["OPEN"])/100.0
        except Exception: pass
    cl=ac._stock_client(); end=datetime(2026,6,18,tzinfo=timezone.utc)
    mb=[b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(15,TimeFrameUnit.Minute),
        start=datetime(2023,1,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]) if is_rth(b.timestamp)]
    mts=[b.timestamp.astimezone(ET) for b in mb]
    mH=[float(b.high) for b in mb]; mL=[float(b.low) for b in mb]; mC=[float(b.close) for b in mb]
    iadx=wilder_adx_series(mH,mL,mC,14)   # continuous cross-session intraday ADX
    byday={}
    for j,et in enumerate(mts):
        d=et.date(); rec=byday.setdefault(d,{"entry":None,"close":None,"path":[],"iadx":None})
        c=mC[j]; m2c=(16-et.hour)*60-et.minute
        rec["close"]=c
        if et.hour<10 or (et.hour==10 and et.minute==0):
            if iadx[j] is not None: rec["iadx"]=iadx[j]   # last value at/<=10:00 entry
        if rec["entry"] is None and et.hour==10 and et.minute==0: rec["entry"]=(c,m2c)
        if rec["entry"] is not None: rec["path"].append((c,max(m2c,0)))
    dbars=sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(1,TimeFrameUnit.Day),
        start=datetime(2022,1,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]),key=lambda b:b.timestamp)
    daily=[{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx=wilder_adx(daily); dl=[x["d"] for x in daily]
    prior={dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    return byday,vix,prior


def sim_day(d,byday,vix,prior):
    """Calibrated clean P&L (stop-capped), no assignment. Returns pnl$ or None."""
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)*EM_CAL
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    bscred=cm(spot,Kp,Kpl,Kc,Kcl,T0,sig)
    if bscred<=0.05: return None
    sl=bscred*(1+STOP)
    for sp,m2c in byday[d]["path"][1:]:
        if cm(sp,Kp,Kpl,Kc,Kcl,max(m2c,0)/YEAR_MIN,sig)>=sl:
            return (CREDIT-STOP*CREDIT)*100*CT
    put_s=max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); call_s=max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    return (CREDIT-put_s-call_s)*100*CT


def main():
    byday,vix,prior=load()
    # baseline set: all gate-passing days with a pnl and an intraday-ADX reading
    base=[]
    for d in prior:
        if d not in byday or byday[d]["entry"] is None or d not in vix: continue
        p=sim_day(d,byday,vix,prior)
        if p is None: continue
        ia=byday[d].get("iadx")
        base.append((d,p,ia))
    have=[x for x in base if x[2] is not None]
    print(f"baseline gate-passing days: {len(base)}  (with intraday-ADX reading: {len(have)})")
    b_tot=sum(x[1] for x in base); b_worst=min(x[1] for x in base); b_win=sum(1 for x in base if x[1]>0)
    print(f"BASELINE (no intraday gate): total ${b_tot:+,.0f} | worst ${b_worst:+,.0f} ({b_worst/POOL*100:.0f}%) | win {100*b_win/len(base):.0f}% | {len(base)} trades\n")
    hdr=f"{'intraday ADX gate':>18} | {'total$':>10} {'vsBase':>8} | {'worst$':>9} | {'win%':>4} {'trades':>6} | {'blocked':>7} {'blkLoss':>7} {'blkWin':>6} | {'net$ from gate':>13}"
    print(hdr); print("-"*len(hdr))
    for thr in [25,28,30,32]:
        kept=[x for x in base if not (x[2] is not None and x[2]>=thr)]
        blocked=[x for x in base if (x[2] is not None and x[2]>=thr)]
        tot=sum(x[1] for x in kept); worst=min(x[1] for x in kept) if kept else 0
        win=sum(1 for x in kept if x[1]>0)
        blk_losers=sum(1 for x in blocked if x[1]<=0); blk_winners=sum(1 for x in blocked if x[1]>0)
        net=tot-b_tot     # positive => gate ADDED money (blocked net-losing days)
        print(f"{'ADX>='+str(thr)+' -> skip':>18} | {tot:+10,.0f} {net:+8,.0f} | {worst:+9,.0f} | {100*win/len(kept):4.0f} {len(kept):6} | {len(blocked):7} {blk_losers:7} {blk_winners:6} | {net:+13,.0f}")
    print("\nblkLoss/blkWin = of the days the gate blocked, how many were losers (rightly avoided) vs winners (premium forfeited).")
    print("net$ from gate = gated_total - baseline_total. POSITIVE => the intraday gate is net-additive; NEGATIVE => dead-zone (kills more winners than losers).")
    print("Absolute $ inflated (BS marks) -> read the SIGN and RATIO, not the dollar magnitude.")


if __name__ == "__main__":
    main()
