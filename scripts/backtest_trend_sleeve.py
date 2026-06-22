"""TREND-DAY SLEEVE (experimental, high-risk, gate-IGNORED) — put the condor's idle
~43% trending days to work, with weekly park/halt discipline.

Honest constraint: buying premium on trend days is -EV (backtest_trend_leg.py). So
the sleeve is a DEFENSIVE PREMIUM SELLER — a WIDER condor (k=1.0 shorts vs the
calm-day 0.5), tighter stop, SMALLER size — only on ADX>=25 days (the days the
calm-day condor sits out). It was +EV but failed the strict gate, so this is an
experimental sleeve, not a certified strategy.

User's risk rules (weekly state machine, ISO week):
  • BIG LOSS on a trend trade  -> PARK the sleeve for the rest of the week
    (fall back to calm-day condor only).
  • BIG WIN  on a trend trade  -> HALT the sleeve for the rest of the week
    (lock the gain, don't round-trip it on a volatile day).
  • The calm-day condor keeps running regardless.

We IGNORE the DSR gate (per request) but still MEASURE: $ contribution, win rate,
drawdown, downtime reduced. A halt rule can't make -EV +EV, so the number is the
number.

Usage: uv run python -m scripts.backtest_trend_sleeve
"""
from __future__ import annotations
import csv, math
from datetime import datetime, timezone
from collections import defaultdict
from zoneinfo import ZoneInfo
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
import integrations.alpaca_client as ac
from scripts.backtest_strangle import bs, wilder_adx, is_rth, YEAR_MIN

ET = ZoneInfo("America/New_York")
W, STOP, VMAX = 5.0, 1.5, 40.0
ADX_MAX = 25.0           # calm/trend split
K_CALM, K_TREND = 0.5, 1.0
COST = 6 * (0.04 / 2)
BIG_LOSS = -0.70         # return-on-risk <= this  -> big loss -> park week
BIG_WIN = 0.20           # return-on-risk >= this  -> big win  -> halt week
CAP = 60000.0
RISK_CALM, RISK_TREND = 0.05, 0.025   # trend sleeve at HALF size (high-risk)


def cm(s, Kp, Kpl, Kc, Kcl, T, sig):
    return (bs(s,Kp,T,False,sig)-bs(s,Kpl,T,False,sig)+bs(s,Kc,T,True,sig)-bs(s,Kcl,T,True,sig))


def condor_ror(rec, sig, k):
    spot, tmin = rec["entry"]; Sc = rec["close"]; T0 = max(tmin,1)/YEAR_MIN
    em = spot*sig*math.sqrt(T0); Kp = round(spot-k*em); Kpl = Kp-W; Kc = round(spot+k*em); Kcl = Kc+W
    credit = cm(spot,Kp,Kpl,Kc,Kcl,T0,sig); risk = W-credit
    if credit <= 0.05 or risk <= 0.05: return None
    sl = credit+STOP*credit
    for sp, m2c in rec["path"][1:]:
        Tt = max(m2c,0)/YEAR_MIN; mk = cm(sp,Kp,Kpl,Kc,Kcl,Tt,sig)
        if mk >= sl: return (credit-mk-COST)/risk, risk
    ps = max(0.0,Kp-Sc)-max(0.0,Kpl-Sc); cs = max(0.0,Sc-Kc)-max(0.0,Sc-Kcl)
    return (credit-ps-cs-COST)/risk, risk


def main():
    vix = {}
    for r in csv.DictReader(open("data/vix1d.csv")):
        try: vix[datetime.strptime(r["DATE"],"%m/%d/%Y").date()] = float(r["OPEN"])/100.0
        except Exception: pass
    cl = ac._stock_client()
    bars = [b for b in cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(15,TimeFrameUnit.Minute), start=datetime(2023,1,1,tzinfo=timezone.utc), end=datetime(2026,6,18,tzinfo=timezone.utc), feed=DataFeed.IEX)).data.get("SPY",[]) if is_rth(b.timestamp)]
    byday = {}
    for b in bars:
        et = b.timestamp.astimezone(ET); d = et.date()
        rec = byday.setdefault(d, {"entry":None,"close":None,"path":[]})
        rec["close"]=float(b.close); m2c=(16-et.hour)*60-et.minute
        if rec["entry"] is None and et.hour==10 and et.minute==0: rec["entry"]=(float(b.close),m2c)
        if rec["entry"] is not None: rec["path"].append((float(b.close),max(m2c,0)))
    dbars = sorted(cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(1,TimeFrameUnit.Day), start=datetime(2022,1,1,tzinfo=timezone.utc), end=datetime(2026,6,18,tzinfo=timezone.utc), feed=DataFeed.IEX)).data.get("SPY",[]), key=lambda b: b.timestamp)
    daily = [{"d":b.timestamp.astimezone(ET).date(),"h":float(b.high),"l":float(b.low),"c":float(b.close)} for b in dbars]
    adx = wilder_adx(daily); dl=[x["d"] for x in daily]
    padx = {dl[i]:adx[dl[i-1]] for i in range(1,len(dl)) if dl[i-1] in adx}
    days = sorted(d for d in byday if byday[d]["entry"] and d in vix and d in padx)
    yrs = (days[-1]-days[0]).days/365.25

    calm_pnl = trend_pnl = 0.0
    calm_n = trend_n = trend_w = trend_skipped = 0
    week_state = {}   # iso (yr,wk) -> 'parked'|'halted'
    big_wins = big_losses = 0
    for d in days:
        sig = vix[d]
        if sig*100 > VMAX:    # crisis: both sit out
            continue
        rec = byday[d]
        if padx[d] < ADX_MAX:          # CALM -> base condor
            r = condor_ror(rec, sig, K_CALM)
            if r: calm_pnl += RISK_CALM*CAP*r[0]; calm_n += 1
        else:                           # TREND -> sleeve (with weekly state)
            wk = d.isocalendar()[:2]
            if week_state.get(wk) in ("parked", "halted"):
                trend_skipped += 1; continue
            r = condor_ror(rec, sig, K_TREND)
            if not r: continue
            ror = r[0]; trend_pnl += RISK_TREND*CAP*ror; trend_n += 1
            if ror > 0: trend_w += 1
            if ror <= BIG_LOSS: week_state[wk] = "parked"; big_losses += 1
            elif ror >= BIG_WIN: week_state[wk] = "halted"; big_wins += 1

    total_days = len(days)
    print(f"COMBINED SYSTEM on ${CAP:,.0f}  (calm condor {RISK_CALM*100:.0f}% + trend sleeve {RISK_TREND*100:.0f}% size)")
    print(f"sample {days[0]} → {days[-1]}  ({total_days} days, {yrs:.1f}yr)\n")
    print(f"CALM-DAY CONDOR (base):  {calm_n} trades  +${calm_pnl:,.0f}  = +${calm_pnl/yrs:,.0f}/yr  (+${calm_pnl/yrs/12:,.0f}/mo)")
    print(f"TREND SLEEVE (experimental):")
    print(f"  trades taken: {trend_n}   win {trend_w}/{trend_n} ({trend_w/max(trend_n,1)*100:.0f}%)   skipped(parked/halted): {trend_skipped}")
    print(f"  big losses (→park wk): {big_losses}   big wins (→halt wk): {big_wins}")
    print(f"  contribution: +${trend_pnl:,.0f}  = +${trend_pnl/yrs:,.0f}/yr  (+${trend_pnl/yrs/12:,.0f}/mo)")
    tot = calm_pnl + trend_pnl
    print(f"\nCOMBINED: +${tot:,.0f}  = +${tot/yrs:,.0f}/yr  (+${tot/yrs/12:,.0f}/mo)  vs condor-only +${calm_pnl/yrs/12:,.0f}/mo")
    traded = calm_n + trend_n
    print(f"DOWNTIME: condor-only traded {calm_n}/{total_days} ({calm_n/total_days*100:.0f}%); +sleeve {traded}/{total_days} ({traded/total_days*100:.0f}%)")
    # sleeve EV sanity (no halt rules) — does the underlying trend seller even make money?
    raw = [condor_ror(byday[d], vix[d], K_TREND) for d in days if padx[d]>=ADX_MAX and vix[d]*100<=VMAX and byday[d]["entry"]]
    raw = [x[0] for x in raw if x]
    print(f"\nSLEEVE EV CHECK (every trend day, no halt rules): {len(raw)} trades, "
          f"avg {sum(raw)/len(raw)*100:+.1f}% of risk/trade, win {sum(1 for r in raw if r>0)/len(raw)*100:.0f}%  "
          f"→ {'+EV' if sum(raw)>0 else '-EV'}")


if __name__ == "__main__":
    main()
