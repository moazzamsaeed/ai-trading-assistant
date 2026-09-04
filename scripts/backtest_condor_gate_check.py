"""Would a VIX1D or intraday-ADX gate have blocked 9/01-9/03? These 3 passed the
live gate (VIX1D<35 & prior-day daily ADX<27) and all stopped out on the call side.

For each day report:
  - VIX1D backed out of the strikes the engine actually used (EM = 2*(short-spot),
    vix1d = EM/(spot*sqrt(T)); shows whether tightening the vol gate could catch it)
  - prior-day daily ADX-14 (current gate) and ADX-7 (a faster daily ADX)
  - rolling intraday 15-min ADX-14 as of the 10:00 ET entry bar (the trend-lag test)

Diagnostic for the 2026-09-01..03 losing streak (reads trades #163-165 from the DB).

Usage: .venv/bin/python -m scripts.backtest_condor_gate_check
"""
from __future__ import annotations
import math, sqlite3, json
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import is_rth

ET = ZoneInfo("America/New_York")
YEAR_MIN = 252 * 390
TARGETS = [date(2026,9,1), date(2026,9,2), date(2026,9,3)]


def wilder_adx_series(H, L, C, period=14):
    """Return ADX list aligned to input (None until warmed up)."""
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
    atr=rma(tr); pdi_s=rma(pdm); ndi_s=rma(ndm)
    dx=[None]*n
    for i in range(n):
        if atr[i] and atr[i]>0 and pdi_s[i] is not None:
            pdi=100*pdi_s[i]/atr[i]; ndi=100*ndi_s[i]/atr[i]
            dx[i]=100*abs(pdi-ndi)/(pdi+ndi) if (pdi+ndi)>0 else 0.0
    adx=[None]*n; start=2*period
    if n>start:
        first=[d for d in dx[period:start] if d is not None]
        if first:
            a=sum(first)/len(first); adx[start-1]=a
            for i in range(start,n):
                if dx[i] is not None: a=(a*(period-1)+dx[i])/period; adx[i]=a
    return adx


def main():
    cl=ac._stock_client(); end=datetime(2026,9,4,tzinfo=timezone.utc)
    # daily bars
    db=sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(1,TimeFrameUnit.Day),
        start=datetime(2026,6,1,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]),key=lambda b:b.timestamp)
    dd=[b.timestamp.astimezone(ET).date() for b in db]
    H=[float(b.high) for b in db]; L=[float(b.low) for b in db]; C=[float(b.close) for b in db]
    adx14=wilder_adx_series(H,L,C,14); adx7=wilder_adx_series(H,L,C,7)
    idx={d:i for i,d in enumerate(dd)}

    # 15-min bars for intraday ADX
    mb=[b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY",timeframe=TimeFrame(15,TimeFrameUnit.Minute),
        start=datetime(2026,8,15,tzinfo=timezone.utc),end=end,feed=DataFeed.IEX)).data.get("SPY",[]) if is_rth(b.timestamp)]
    mts=[b.timestamp.astimezone(ET) for b in mb]
    mH=[float(b.high) for b in mb]; mL=[float(b.low) for b in mb]; mC=[float(b.close) for b in mb]
    iadx=wilder_adx_series(mH,mL,mC,14)

    # trades -> backed-out VIX1D
    c=sqlite3.connect("data/trademaster.db")
    tr={}
    for tid in (163,164,165):
        e=json.loads(c.execute("SELECT extra FROM trades WHERE id=?",(tid,)).fetchone()[0])
        tr[tid]=e

    print(f"{'day':>11} | {'VIX1D*':>6} | {'pADX14':>6} {'gate<27':>7} | {'pADX7':>6} | {'intraday15mADX@10:00':>20} | outcome")
    print("-"*92)
    tid_by_day={date(2026,9,1):163,date(2026,9,2):164,date(2026,9,3):165}
    for d in TARGETS:
        i=idx.get(d)
        p14=adx14[i-1] if i and i>0 else None
        p7=adx7[i-1] if i and i>0 else None
        # backed-out vix1d from strikes
        tid=tid_by_day[d]; e=tr[tid]
        sp=float(e["short_put"][-8:])/1000; sc=float(e["short_call"][-8:])/1000
        # entry spot ~ midpoint of shorts (engine centers condor on spot)
        spot=(sp+sc)/2.0
        em=(sc-sp)   # short_call-short_put = EM (since each short = spot +/- 0.5*EM)
        T=360/YEAR_MIN  # 10:00 ET -> 360 min to close
        vix1d=em/(spot*math.sqrt(T))*100
        # intraday adx at last 15m bar with time <= 10:00 ET that day
        cand=[(k,mts[k]) for k in range(len(mts)) if mts[k].date()==d and (mts[k].hour<10 or (mts[k].hour==10 and mts[k].minute==0))]
        ival=iadx[cand[-1][0]] if cand and iadx[cand[-1][0]] is not None else None
        istr=f"{ival:.1f}" if ival is not None else "n/a"
        pnl=e.get("realized_pnl_usd","?")
        print(f"{str(d):>11} | {vix1d:6.1f} | {p14:6.1f} {'PASS' if (p14 and p14<27) else '?':>7} | {p7:6.1f} | {istr:>20} | stop -{'?'}")
    print("\n* VIX1D backed out of the strikes the engine used (EM = short_call - short_put).")
    print("pADX14 = prior-day daily ADX-14 (the LIVE gate, blocks if >=27). pADX7 = faster daily ADX-7.")
    print("intraday 15m ADX@10:00 = rolling Wilder ADX-14 on RTH 15-min bars as of the entry bar.")
    # context: show recent daily ADX trajectory
    print("\nrecent daily ADX-14 / ADX-7 trajectory (last 12 sessions):")
    for j in range(max(0,len(dd)-12),len(dd)):
        a14=f"{adx14[j]:.1f}" if adx14[j] is not None else "  - "
        a7=f"{adx7[j]:.1f}" if adx7[j] is not None else "  - "
        print(f"  {dd[j]}  close {C[j]:7.2f}  ADX14 {a14:>5}  ADX7 {a7:>5}")


if __name__ == "__main__":
    main()
