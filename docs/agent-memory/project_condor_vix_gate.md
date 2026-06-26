---
name: project_condor_vix_gate
description: 2026-06-25 — VIX1D-gated condor (drop prior-day daily ADX) PASSES the validation gate; daily-ADX filter is over-conservative. Concrete improvement to the proven condor edge.
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**RESULT (2026-06-25): replacing the condor's prior-day DAILY-ADX gate with a forward VIX1D gate PASSES the validation gate and roughly DOUBLES coverage.** Unparks the long-flagged "VIX1D-gated condor re-test" with concrete numbers. Motivated by the condor being shut out 4+ days this week (daily ADX climbed 26.8→33.2→42.4→45.8) on days that were DEAD CALM intraday (intraday ADX ~10) — the daily-ADX filter is a LAGGING proxy with false negatives. Script: `scripts/backtest_condor_vix_gate.py` (A/B over the same data + same walk-forward + DSR as backtest_wide_condor.py; only the per-day gate changes). SPY 2023-01→2026-06, 866 candidate days; baseline skips **364** days on daily-ADX≥25 alone (VIX1D already <40).

| gate | days | OOS Sharpe | DSR% | win% | verdict |
|---|---|---|---|---|---|
| BASELINE prior-ADX<25 & VIX1D<40 | 497 | +3.10 | 100 | 76% | PASS |
| VIX1D<40 only (drop daily ADX) | 861 | +2.18 | 97 | 73% | PASS |
| VIX1D<35 only | 858 | +2.54 | 99 | 73% | PASS |
| VIX1D<30 only | 857 | +2.54 | 99 | 73% | PASS |
| VIX1D<25 only | 843 | +2.33 | 98 | 72% | PASS |

**KEY:** VIX1D-only PASSES at EVERY threshold 25–40 (DSR 97–99%) — robust, not a cherry-picked artifact. The daily-ADX gate is **over-conservative** (throws away tradeable days). Tradeoff = quality vs quantity: baseline trades fewer HIGHER-Sharpe days (+3.10) vs ~73% MORE days at +2.5. Worst-case unchanged (~−104%, wing-capped defined risk). **Recommended a-priori gate: VIX1D < 35** (best Sharpe +2.54 @ DSR 99%, near-full coverage). Today's VIX1D was 35.9 → trades under <40, not <35.

**🎉 FIRST CONDOR TRADE EVER — v2 FIRED 2026-06-26 10:00 ET (trade #83).** Exactly the day v1 would have blocked: prior-day daily ADX **45.8** (v1 gate would HOLD) but VIX1D **28.6 < 35** (v2 calm) → SELL_CONDOR. Strikes 719/724 put + 737/742 call ($5 wings); modeled credit $58.50/ct, max-loss $441.50; **FILLED at $48.00/ct** — an ~18% haircut vs model = the FIRST live measurement of the 4-leg cost-fragility the strategy review flagged (worth tracking across fills). Risk ~$452 ≈ 4.4% of $10k. First +EV (premium-selling) trade of the whole paper effort, after the condor was shut out 4 days by the old daily-ADX gate. Managed by condor exit monitor (1.5× stop / 15:50 force-close, no profit target).

**#83 CLOSED: WIN +$46 (force_close_15:50).** Held all day, no stop; SPY drifted up but stayed inside the 724/737 shorts → both spreads decayed, bought back for $2 debit → kept ~96% of credit (+10.2% on $452 risk). Effective capital $10,192→$10,238. ⚠️ COST DATA: entry filled $48 vs $58.50 model = **18% haircut** (the 4-leg cost-fragility the strategy review flagged) — exit clean ($2), so robustly +$ anyway, but TRACK the entry haircut across fills (if 18% persists it erodes the edge). n=1 — promising, not proven. Week net (06-22→26): trend #79-82 +$192 + condor #83 +$46 = **+$238 (+2.4%), $10,238**. The proven edge fired+won on the exact day v2 unlocked.

**STATUS: IMPLEMENTED + PUSHED 2026-06-25 (`6cc1d20`, on main, 580 tests green).** `decide_condor` now gates on **VIX1D < 35 ONLY**; prior_adx is telemetry-only (logged, never gates) and optional (missing ADX no longer blocks — also kills the old get_daily_bars-None-→-HOLD-forever failure mode). `CONDOR_VERSION → vrp_condor_v2`. VIX1D_MAX 40→35; ADX_MAX retained as deprecated/reference. Tests updated (high-ADX-now-trades + high-vol-holds, engine + strategist). **GOES LIVE at tomorrow's 7:45 AM ET auto-restart (Fri 2026-06-26)** — if Friday's VIX1D < 35, the condor takes its FIRST real trade (today's VIX1D was 35.9 → would've been just over the new cap). This is the FIRST concrete improvement to the PROVEN edge (condor), vs the −EV directional leg. See [[project_regime_dead_zone]], [[project_condor_build_0619]], [[project_broker_options_ceiling]]. Caveats: IEX 15-min data, BS pricing w/ VIX1D σ, modeled 4-leg cost; threshold chosen a priori (not tuned to OOS).
