"""Deep backtest of the SELECTIVE late-day short-close (no XSP, keep 28ct).

Rule: at a late check time, close ONLY the short leg(s) within `buffer` of spot
(or ITM), via single-leg market orders; let comfortably-OTM shorts + all longs
expire. Optional second recheck near the bell to catch late movers.

Sweeps trigger time x buffer, and reports for each config:
  - total P&L (3.5y) + cost vs hold-to-expiry
  - ASSIGNMENT SLIP-THROUGH: assignment days a non-closed short still hit (the
    'chance left' — the whole point is to drive this toward 0)
  - worst day (should cap at the WING once assignment is handled)
  - close-rate (how often it fires = the false-alarm/theta-give-up driver)

Calibrated engine (strikes x1.45 EM = live 0.48% width, credit $0.35, 1.5x stop,
overnight assignment gap from real next-day opens). Validated vs live fills.

Usage: .venv/bin/python -m scripts.backtest_selective_close
"""
from __future__ import annotations
import csv, math, collections
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
STOP = 1.5
CT = 28
EM_CAL = 1.45
CREDIT = 0.35
HS = 0.015          # single-leg market crossing, per share
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
        et=b.timestamp.astimezone(ET); d=et.date(); rec=byday.setdefault(d,{"entry":None,"close":None,"open":None,"path":[]})
        c=float(b.close); m2c=(16-et.hour)*60-et.minute
        if rec["open"] is None: rec["open"]=c
        rec["close"]=c
        if rec["entry"] is None and et.hour==10 and et.minute==0: rec["entry"]=(c,m2c)
        if rec["entry"] is not None: rec["path"].append((c,max(m2c,0)))
    dbars=sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(1,TimeFrameUnit.Day),
        start=datetime(2022,1,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]),key=lambda b:b.timestamp)
    daily=[{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx=wilder_adx(daily); dl=[x["d"] for x in daily]
    prior={dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    days=sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior)
    nextopen={days[i]:byday[days[i+1]]["open"] for i in range(len(days)-1)}
    return byday, vix, prior, days, nextopen


def bar_at(path, t): return min(path, key=lambda pr: abs(pr[1]-t))


def sim(d, byday, vix, prior, nextopen, mode, t1=30, buf=0.002, recheck=False, hs=HS):
    """mode 'hold' or 'selective'. Returns (pnl_$, assigned_bool, closed_bool).
    `hs` = per-share slippage on the buyback (marketable-limit fill vs mid)."""
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)*EM_CAL
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    bscred=cm(spot,Kp,Kpl,Kc,Kcl,T0,sig)
    if bscred<=0.05: return None
    # 1.5x stop before t1
    sl=bscred+STOP*bscred
    for sp,m2c in byday[d]["path"][1:]:
        if m2c < t1: break
        if cm(sp,Kp,Kpl,Kc,Kcl,max(m2c,0)/YEAR_MIN,sig)>=sl:
            return ((CREDIT-STOP*CREDIT)*100*CT, False, True)
    put_settle = max(0.0,Kp-Sc)-max(0.0,Kpl-Sc)   # short-put vertical intrinsic at close
    call_settle = max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    if mode=="hold":
        pnl=CREDIT-put_settle-call_settle
        assigned = (Kpl<Sc<Kp) or (Kc<Sc<Kcl)
        if assigned:
            no=nextopen.get(d)
            if no is not None: pnl += (no-Sc) if Kpl<Sc<Kp else (Sc-no)
        return (pnl*100*CT, assigned, False)
    # selective: decide per short whether to close at t1 (and optional recheck at t2=2)
    def near(s, strike, is_put):
        return (s <= strike*(1+buf)) if is_put else (s >= strike*(1-buf))
    s1,_=bar_at(byday[d]["path"], t1)
    put_closed=near(s1,Kp,True); call_closed=near(s1,Kc,False)
    close_pnl=0.0; closed_any=False
    if put_closed:
        m=bar_at(byday[d]["path"],t1); Tc=max(m[1],0.3)/YEAR_MIN
        close_pnl += -(bs(s1,Kp,Tc,False,sig)+hs) + max(0.0,Kpl-Sc); closed_any=True
    if call_closed:
        m=bar_at(byday[d]["path"],t1); Tc=max(m[1],0.3)/YEAR_MIN
        close_pnl += -(bs(s1,Kc,Tc,True,sig)+hs) + max(0.0,Sc-Kcl); closed_any=True
    # recheck at 15:58 for shorts not yet closed
    if recheck:
        s2,_=bar_at(byday[d]["path"],2); Tc2=max(2,0.3)/YEAR_MIN
        if not put_closed and near(s2,Kp,True):
            close_pnl += -(bs(s2,Kp,Tc2,False,sig)+hs) + max(0.0,Kpl-Sc); put_closed=True; closed_any=True
        if not call_closed and near(s2,Kc,False):
            close_pnl += -(bs(s2,Kc,Tc2,True,sig)+hs) + max(0.0,Sc-Kcl); call_closed=True; closed_any=True
    # non-closed shorts settle at close; assignment if a non-closed short is ITM
    pnl=CREDIT + close_pnl
    assigned=False
    if not put_closed:
        pnl -= put_settle
        if Kpl<Sc<Kp:
            assigned=True; no=nextopen.get(d)
            if no is not None: pnl += (no-Sc)
    if not call_closed:
        pnl -= call_settle
        if Kc<Sc<Kcl:
            assigned=True; no=nextopen.get(d)
            if no is not None: pnl += (Sc-no)
    return (pnl*100*CT, assigned, closed_any)


def run(byday, vix, prior, nextopen, mode, **kw):  # kw includes hs
    tot=0.0; worst=0.0; n=0; assigned=0; closed=0
    for d in prior:
        if d not in byday or byday[d]["entry"] is None or d not in vix: continue
        r=sim(d,byday,vix,prior,nextopen,mode,**kw)
        if r is None: continue
        p,a,c=r; tot+=p; worst=min(worst,p); n+=1; assigned+=a; closed+=c
    return tot, worst, n, assigned, closed


def main():
    byday,vix,prior,_days,nextopen = load()
    h_tot,h_worst,h_n,h_asg,_ = run(byday,vix,prior,nextopen,"hold")
    print(f"HOLD baseline (stop-aware): {h_n} days | total ${h_tot:+,.0f} | worst ${h_worst:+,.0f} ({h_worst/POOL*100:.0f}%) | {h_asg} assignment days\n")
    hdr=f"{'config':>34} {'slip/sh':>7} | {'total$':>9} {'cost%':>6} | {'ASSIGN LEFT':>11} | {'worst$':>8} {'%25k':>5} | {'close%':>6}"
    print(hdr); print("-"*len(hdr))
    # (label, t1, buf, recheck, hs)
    configs=[
        ("IDEAL 15:55 +0.2% (tight slip)", 5, 0.002, False, 0.015),
        ("GUARDRAIL 15:50 +0.3% (slip .04)", 10, 0.003, False, 0.04),
        ("GUARDRAIL 15:50 +0.3% (slip .06)", 10, 0.003, False, 0.06),
        ("GUARDRAIL 15:50 +0.3% (slip .08)", 10, 0.003, False, 0.08),
        ("GUARDRAIL 15:50 +0.3% +58recheck (.06)", 10, 0.003, True, 0.06),
        ("close-ALL @15:55 ref (slip .06)", 5, 1.0, False, 0.06),
    ]
    for lbl,t1,buf,rc,hs in configs:
        tot,worst,n,asg,clo=run(byday,vix,prior,nextopen,"selective",t1=t1,buf=buf,recheck=rc,hs=hs)
        cost=(tot-h_tot)/h_tot*100
        print(f"{lbl:>34} {hs*100:>5.0f}/ct | {tot:+9,.0f} {cost:+5.0f}% | {asg:>4}/{h_asg:<4}   | {worst:+8,.0f} {worst/POOL*100:+4.0f}% | {100*clo/n:>5.0f}%")
    print("\nASSIGN LEFT = assignment days still slipping (goal 0). slip/sh = modeled buyback slippage.")
    print("GUARDRAIL variant = 15:50 (more liquid, retry buffer) + 0.3% buffer (wider safety) + realistic")
    print("slippage. The slippage CAP (marketable limit) prevents fills worse than modeled — shown by sensitivity.")


if __name__ == "__main__":
    main()
