---
name: project_regime_dead_zone
description: "Condor+trend have a structural \"dead zone\" — both stand aside in some regimes. Confirmed live 2026-06-22. Mitigation = VIX1D-gated condor re-test."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**STRUCTURAL DEAD ZONE between the condor and trend engine (confirmed live 2026-06-22, first paper day).** The two strategies do NOT tile the regime space — there are regimes where BOTH correctly stand aside, so the system trades nothing. Root cause: they gate on ADX measured on **different timeframes**.

- **Condor** (`condor_engine.py`, `ADX_MAX=25`): stands aside when **prior-day DAILY ADX ≥ 25**. Won't sell premium into a (daily-)trending regime.
- **Trend engine** (`signal_engine.py`): needs **INTRADAY ADX in [25, 50)** + VWAP separation + EMA align. HOLDs if intraday ADX < 25 (`ADX_MIN`, "weak trend") OR ≥ 50 (`ADX_OVEREXT`, "overextended — reverts").

**Dead zone = prior-day daily ADX ≥ 25 (condor out) AND intraday either limp (ADX<25) or overextended (ADX≥50) (trend out).** Two sub-cases. Observed live 06-22 noon via the OVEREXTENDED branch: daily ADX 26.8 (condor out) + **intraday ADX 56.4** (trend out, "overextended, reverts"). So it wasn't a quiet day — SPY trended HARD intraday; too hot for the condor to sell premium, too late/exhausted for the trend engine to chase. Both holding = CORRECT edge-preserving behavior (chasing ADX 56 buys the top). Cost is downtime/opportunity, NOT loss.

**S/R note:** the v2 S/R gate sits AFTER the ADX_MIN/ADX_OVEREXT checks in `decide()`, so on weak-trend or overextended HOLDs the S/R headroom logic is **never reached** — S/R only engages once intraday ADX is in the tradeable [25,50) band.

**✅ FIRST LIVE S/R VETO — VALIDATED (06-23 11:26 ET).** Trend engine produced its first full BUY_PUT of the paper week (SPY broke below VWAP: alignment✓ ADX 30.9 sweet✓ DIST_MIN✓), but the v2 S/R gate BLOCKED it: "support $734.22 only 0.04% ahead (< 0.15% HEADROOM_MIN), no room to run." Outcome: support HELD — SPY bounced ~$1.9 off 734.22 back to ~736 (VWAP) within ~30 min. A put bought there would be underwater. So the gate's "don't buy a put pinned at support" thesis was vindicated (n=1; doesn't prove 0.15% is optimal, but first real evidence the feature avoids a losing entry). Note the gate's smart behavior: if support had BROKEN, the next-lower level becomes the reference and the put can re-fire with room — i.e. it converts "buy AT support" into "buy on confirmed breakdown." Still 0 actual trades through 06-23 midday (condor also HOLD: prior-day daily ADX 33.2).

**⚠️ DEBUG CORRECTION (06-22): a daemon restart does NOT wipe indicator state.** Directional indicators are recomputed FRESH each scan from `alpaca_client.get_recent_bars(..., warmup_days=1)` — there is NO in-memory bar buffer. So a mid-session restart does not cause a warmup blind spot (I initially misattributed it to that). The real cause of the 06-22 midday blind spot: a **holiday-warmup bug** — `get_recent_bars`'s session walk-back padded only for weekends, not holidays, so on Mon 06-22 the "1 session back" anchor landed on Fri 06-19 (Juneteenth, no data) → ZERO warmup bars → ema50/volume_ratio_20 None until today alone hit 50 bars (~13:40 ET). That suppressed 8 BUY_PUT signals (overridden to HOLD), likely the first real trade. FIXED `8ca8034` (`_warmup_window` + 5-day holiday pad + req_limit spanning extended-hours bars; 576 tests green, pushed to main). So on 06-22 BOTH the structural dead zone AND this (now-fixed) bug kept the engine flat. (Per-ticker `directional_decision` reasoning is now logged to journald — commit `de805e1` — so these holds are observable outside Discord.)

**MITIGATION (hypothesis, NOT yet validated — keep parked till after paper week):** the condor's prior-day DAILY ADX gate is a lagging, crude proxy for "will today be calm intraday." A day can print daily ADX 26.8 yet be dead-calm intraday = a GOOD premium day declined for the wrong reason. **Re-test the condor gated on a forward/same-day vol measure (intraday realized vol or the live VIX1D it already derives, 22.0 on 06-22) instead of prior-day daily ADX** → could reclaim "elevated daily ADX but calm intraday" days and shrink the dead zone with a MEASURED edge. MUST pass the embargoed walk-forward + DSR gate before going live. Do NOT bolt on a 3rd "middle-regime" strategy just to stay busy (manufacturing trades w/o edge = the trap the strategy review exists to avoid). See [[project_condor_build_0619]], [[project_strategy_review_0618]].
