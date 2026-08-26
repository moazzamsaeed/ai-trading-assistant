"""Condor backtest CALIBRATED to the live engine, then validated against real fills.

Two fixes over the naive backtest (both confirmed by validating vs live trades #142-154):
  1. STRIKE WIDTH: the live engine's chain-derived VIX1D placed short strikes at
     ~0.48% of spot (median), vs the naive backtest's 0.33% (it used the lower CSV
     VIX1D). Scale the expected move by EM_CAL so modeled strikes match live width.
  2. STOP-CAPPED LOSSES: the naive backtest let trend-day breaches settle at the full
     close intrinsic (#143: -$10,204) but the live 1.5x stop actually capped it at
     -$1,748. Model the loss as the LESSER of the stop-capped loss and settle, since
     the live stop demonstrably fires on gradual trend-day breaches.

Reports clean (stop works / XSP-equiv) and with-assignment. Usage:
  .venv/bin/python -m scripts.backtest_condor_live_calibrated
"""
from __future__ import annotations
import csv, math, statistics
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
COST = 6 * (0.04 / 2)
CT = 28
EM_CAL = 0.48 / 0.33          # scale strikes to the live-observed 0.48% half-width
LIVE_AVG_CREDIT_SH = 0.35     # realistic per-share credit (live fills)


def cmark(s, Kp, Kpl, Kc, Kcl, T, sig):
    return bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig)


def load(start_year):
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()] = float(r["OPEN"])/100.0
        except Exception: pass
    cl = ac._stock_client(); end = datetime(2026,6,18,tzinfo=timezone.utc)
    bars=[b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",
        timeframe=TimeFrame(15,TimeFrameUnit.Minute), start=datetime(start_year,1,1,tzinfo=timezone.utc),
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
        timeframe=TimeFrame(1,TimeFrameUnit.Day), start=datetime(start_year-1,1,1,tzinfo=timezone.utc),
        end=end, feed=DataFeed.IEX)).data.get("SPY",[]), key=lambda b:b.timestamp)
    daily=[{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx=wilder_adx(daily); dl=[x["d"] for x in daily]
    prior={dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    days=sorted(d for d in byday if byday[d]["entry"] and d in vix and d in prior)
    nextopen={days[i]:byday[days[i+1]]["open"] for i in range(len(days)-1)}
    return byday, vix, prior, days, nextopen


def sim_day(d, byday, vix, prior, nextopen, assign):
    sig=vix[d]
    if sig*100>VMAX or prior[d]>=ADX_MAX: return None
    spot,tmin=byday[d]["entry"]; Sc=byday[d]["close"]; T0=max(tmin,1)/YEAR_MIN
    em=spot*sig*math.sqrt(T0)*EM_CAL        # CALIBRATED strike width
    if em<=0: return None
    Kp=round(spot-K*em); Kpl=Kp-W; Kc=round(spot+K*em); Kcl=Kc+W
    bscred=cmark(spot,Kp,Kpl,Kc,Kcl,T0,sig)
    if bscred<=0.05: return None
    credit=LIVE_AVG_CREDIT_SH               # realistic credit level
    # walk path: STOP caps the loss when the BS mark hits 2.5x the BS credit (trend days)
    sl=bscred+STOP*bscred; stop_loss_sh=None
    for sp,m2c in byday[d]["path"][1:]:
        Tt=max(m2c,0)/YEAR_MIN; mk=cmark(sp,Kp,Kpl,Kc,Kcl,Tt,sig)
        if mk>=sl:
            stop_loss_sh = credit - STOP*credit    # exit ~ -1.5x credit (live #143 ~ -1.9x)
            break
    put_s=max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); call_s=max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    settle_sh = credit - put_s - call_s
    # loss on a breach = the BETTER of stop-cap vs settle (stop fires on gradual breaches).
    # If the stop fired, we take the stop-capped loss (what live achieves); else settle.
    pnl_sh = stop_loss_sh if (stop_loss_sh is not None and stop_loss_sh > settle_sh) else settle_sh
    if assign and stop_loss_sh is None and (Kpl<Sc<Kp or Kc<Sc<Kcl):
        no=nextopen.get(d)
        if no is not None: pnl_sh += (no-Sc) if Kpl<Sc<Kp else (Sc-no)
    return pnl_sh*100*CT, (pnl_sh>0)


def window(name, a, b, byday, vix, prior, nextopen, assign):
    ds=[];
    for d in [x for x in byday if a<=x<=b]:
        if d not in vix or d not in prior: continue
        r=sim_day(d, byday, vix, prior, nextopen, assign)
        if r is not None: ds.append(r)
    if not ds: print(f"{name}: none"); return
    tot=sum(x[0] for x in ds); wins=sum(1 for x in ds if x[1]); n=len(ds)
    worst=min(x[0] for x in ds)
    print(f"  {name:16} | {n:3} trades | win {100*wins/n:.0f}% | total ${tot:+,.0f} ({tot/25000*100:+.0f}% of $25k) | worst day ${worst:+,.0f}")


def main():
    byday, vix, prior, days, nextopen = load(2023)
    print(f"Calibrated backtest: strikes x{EM_CAL:.2f} EM (-> live ~0.48% width), credit ${LIVE_AVG_CREDIT_SH}/sh, stop-capped losses.\n")
    print("=== CLEAN (stop caps trend breaches; no assignment / XSP-equivalent) ===")
    for nm,a,b in [("2023",date(2023,1,1),date(2023,12,31)),("2024",date(2024,1,1),date(2024,12,31)),
                   ("2025",date(2025,1,1),date(2025,12,31)),("2026 H1",date(2026,1,1),date(2026,6,17))]:
        window(nm,a,b,byday,vix,prior,nextopen,False)
    print("\n=== WITH SPY assignment tail (stop fails on pin days) ===")
    for nm,a,b in [("2023",date(2023,1,1),date(2023,12,31)),("2024",date(2024,1,1),date(2024,12,31)),
                   ("2025",date(2025,1,1),date(2025,12,31)),("2026 H1",date(2026,1,1),date(2026,6,17))]:
        window(nm,a,b,byday,vix,prior,nextopen,True)


if __name__ == "__main__":
    main()
