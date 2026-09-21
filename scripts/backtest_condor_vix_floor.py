"""VIX1D FLOOR sweep — does skipping low-vol days help the condor?

Motivation (2026-09-21, #176): on ultra-low-VIX days the engine sizes tiny strikes
(5pt band) and collects thin credit ($27), AND the tight band is more breach-prone —
the worst risk/reward. This tests adding a LOWER VIX1D bound to the live gate (which
already caps VIX1D<35 & prior-ADX<27): skip entry when VIX1D < floor.

Calibrated engine (strikes x1.45 EM = live ~0.48% width, credit $0.35, DISTANCE-AWARE
1.5x stop = the deployed config). For each floor reports total P&L, worst day, win%,
#traded, #skipped, and — the deciding number — the net P&L of the SKIPPED days (if
they were collectively net-negative, skipping them is pure gain).

Absolute $ BS-inflated -> read columns relative. Usage:
  .venv/bin/python -m scripts.backtest_condor_vix_floor
"""
from __future__ import annotations
import math
from scripts.backtest_condor_stop_compare import (
    load, cm, SHRINK, EM_CAL, K, W, VMAX, ADX_MAX, CT, POOL, YEAR_MIN,
)


def sim_day(d, byday, vix, prior):
    """Distance-aware 1.5x stop, thin credit, clean settle. Returns (pnl$, vix1d_pts)."""
    sig = vix[d]
    if sig * 100 > VMAX or prior[d] >= ADX_MAX:
        return None
    spot, tmin = byday[d]["entry"]; Sc = byday[d]["close"]; T0 = max(tmin, 1) / YEAR_MIN
    em = spot * sig * math.sqrt(T0) * EM_CAL
    if em <= 0:
        return None
    Kp = round(spot - K * em); Kpl = Kp - W; Kc = round(spot + K * em); Kcl = Kc + W
    bscred = cm(spot, Kp, Kpl, Kc, Kcl, T0, sig)
    if bscred <= 0.05:
        return None
    c = SHRINK * bscred; m0 = bscred
    for sp, m2c in byday[d]["path"][1:]:                       # distance-aware stop
        rise = cm(sp, Kp, Kpl, Kc, Kcl, max(m2c, 0) / YEAR_MIN, sig) - m0
        near = (sp >= Kc) or (sp <= Kp)
        if rise >= 1.5 * c and near:
            return (-rise * 100 * CT, sig * 100)
    put_s = max(0.0, Kp - Sc) - max(0.0, Kpl - Sc); call_s = max(0.0, Sc - Kc) - max(0.0, Sc - Kcl)
    return ((c - put_s - call_s) * 100 * CT, sig * 100)


def main():
    byday, vix, prior = load()
    # all gate-passing days with their vix1d
    days = []
    for d in prior:
        if d not in byday or byday[d]["entry"] is None or d not in vix:
            continue
        r = sim_day(d, byday, vix, prior)
        if r is not None:
            days.append(r)  # (pnl, vix1d_pts)
    base_tot = sum(p for p, _ in days); base_worst = min(p for p, _ in days)
    base_wins = sum(1 for p, _ in days if p > 0); n = len(days)
    print(f"BASELINE (no floor): {n} days | total ${base_tot:+,.0f} | worst ${base_worst:+,.0f} "
          f"({base_worst/POOL*100:.0f}%) | win {100*base_wins/n:.0f}%\n")
    print(f"{'VIX1D floor':>11} | {'total$':>10} {'vsBase':>8} | {'worst$':>9} | {'win%':>4} | "
          f"{'traded':>6} {'skipped':>7} | {'skipped-days net$':>16}")
    print("-" * 92)
    for floor in [0, 9, 10, 11, 12, 13, 14]:
        kept = [(p, v) for p, v in days if v >= floor]
        skip = [(p, v) for p, v in days if v < floor]
        tot = sum(p for p, _ in kept); worst = min((p for p, _ in kept), default=0)
        wins = sum(1 for p, _ in kept if p > 0)
        skip_net = sum(p for p, _ in skip)
        vs = tot - base_tot
        wr = 100 * wins / len(kept) if kept else 0
        print(f"{('>='+str(floor) if floor else 'none'):>11} | {tot:+10,.0f} {vs:+8,.0f} | "
              f"{worst:+9,.0f} | {wr:>4.0f} | {len(kept):>6} {len(skip):>7} | {skip_net:>+16,.0f}")
    print("\nskipped-days net$ = total P&L of the days each floor removes. If NEGATIVE, those low-vol")
    print("days were collectively money-LOSERS -> skipping them is pure gain (total rises by |that|).")
    print("If POSITIVE, the floor throws away net-winning days. Absolute $ BS-inflated -> read signs/ratios.")


if __name__ == "__main__":
    main()
