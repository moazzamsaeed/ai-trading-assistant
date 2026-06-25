---
name: adx-threshold-calibration-scheduled-for-2026-06-17
description: A durable systemd timer auto-runs scripts/calibrate_adx.py on Wed 2026-06-17 ~8:03 AM ET and posts the report to
metadata: 
  node_type: memory
  type: project
  originSessionId: a424fa8f-032b-48d2-860b-a2ba6e3b87cd
---

The loss-prevention package ([[project_loss_prevention_package]]) shipped many `settings.*` thresholds as **first-pass guesses**, especially the ADX gate. They need calibration from real entry-ADX-vs-outcome data.

## The scheduled calibration (durable — survives machine restart)
- **systemd user timer `calibrate-adx.timer`** (in `~/.config/systemd/user/`, NOT the repo): `OnCalendar=2026-06-17 12:03:00 UTC` (= 8:03 AM ET), `Persistent=true`, and user `Linger=yes` → it runs even across reboots / while logged out. One-shot (specific date).
- Runs **`scripts/calibrate_adx.py --since 2026-06-12 --post`** (committed `3fb601a`): pairs each directional trade's `entry_adx` with its outcome, buckets by ADX, reports win-rate + avg P&L + failed-breakout% per band, recommends `adx_block_below`/`adx_full_above`. Needs ≥8 samples or it says "too small, recalibrate later." `--post` sends to #logs.
- **Applying the tuned values is left to a human/Claude** (script only recommends) — config edit + commit + restart. So on 06-17 the recommendation lands in #logs; user can say "apply the ADX calibration."

## Current provisional thresholds (do not treat as tuned)
- `adx_block_below=18`, `adx_full_above=25`, `adx_weak_size_mult=0.5`
- `chop_failed_breakout_limit=2`, `chop_failed_peak_pct=10`, `chop_pause_minutes=90`
- `reentry_same_direction_limit=2`, `reentry_pullback_range_frac=0.30`, `reentry_fresh_volume_min=1.5`
- `trailing_stop_trail_gap_pct=0.10`, `zdte_early_loss_cut_pct=0.25`, `medium_conviction_size_mult=0.5`, `weak_rsi_size_mult=0.5`

## Early calibration signal (06-12, n small)
Winners #69/#70/#71 had entry ADX **30.5 / 38.6 / 25.3** (all ≥25, won); the **blocked** entry was ADX **16.3** (<18, chop); #68 had ADX 40 but was a trend trade that REVERSED (a loss, but not a chop entry — gate rightly let it through). So early evidence: high-ADX entries win, low-ADX correctly blocked → 18/25 look roughly right but need more data. `intraday 5-min ADX runs lower than the daily-chart "25" convention`; SPY read 30.8 late-day on a chop day, so ADX is time-varying within a day.

## How to run manually anytime
`.venv/bin/python scripts/calibrate_adx.py --since 2026-06-12`
