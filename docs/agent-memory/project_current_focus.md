---
name: Current focus — paper week 1 results + condor v2 gate (as of 2026-06-25)
description: As of 2026-06-25. First $10k paper week of the condor+trend build. Condor v2 (VIX1D<35) shipped, goes live Fri 06-26. Trend leg confirmed −EV. Full trade-by-trade record in linked memories.
type: project
originSessionId: 788694ac-3dc6-4a75-8ce6-fb5dfedd7148
---
## ⭐⭐ Status as of 2026-06-25 (CURRENT — first paper week of the condor+trend build)

**THE WEEK (06-22→06-26): $10k paper, deterministic condor + S/R trend engine both live. Effective capital ≈ $10,192 (~+$192, ~flat — one big winner carrying the −EV leg).**

- **CONDOR = the proven edge, but shut out all week.** Built v1 (calm-day filter prior-day DAILY ADX<25 & VIX1D<40); the daily ADX climbed 26.8→33.2→42.4→45.8 every day → condor HELD all 4 days, ZERO trades. Live MLEG execution validated 06-22 (probe). **06-25: shipped CONDOR v2 (`6cc1d20`) — gate on forward VIX1D<35 ONLY, dropped the over-conservative lagging daily-ADX gate** (backtest `scripts/backtest_condor_vix_gate.py`: VIX1D<35 PASSES the gate DSR 99%/+2.54 OOS, ~2× coverage). prior_adx now telemetry-only. **GOES LIVE Fri 06-26 7:45 AM auto-restart**; if Fri VIX1D<35, FIRST real condor trade. See [[project_condor_vix_gate]], [[project_regime_dead_zone]].
- **TREND LEG = −EV, confirmed every way.** 4 trades (#79–82): −$380 call (thesis cut), +$140 put (trailing), −$252 put (FIRST live theta-backstop firing), +$684 put incl. +100% scale-out (trailing). Net +$192 but it's ONE lucky breakdown (#82) carrying 3 losers — variance, not edge. **Proved −EV: 1-min-with-costs backtest (31k trades, theta alone kills the tiny edge even at $0 spread; faster timeframe = WORSE) [[project_1min_backtest_verdict]]; ADX doesn't predict per-trade outcome (loser had stronger ADX than winner, live + backtest).** Exit machinery all validated live: S/R gate (vetoed + cleared puts), theta backstop, trailing stop. [[project_exit_theta_gap]].
- **Fixes shipped this week (all on main):** holiday-robust warmup (`8ca8034`, Juneteenth broke ema50), per-ticker decision logging (`de805e1`), time-scaled theta backstop for 0DTE longs (`da3de04`, −40/−30/−22% by time-of-day), pre-market briefing reliability (`64c4e97`, Gemini 503→Anthropic-primary + 180s timeout; FIXED, verified 06-25 briefing succeeded), condor v2 gate (`6cc1d20`).
- **LLM strategy verdict (deep-research, cited):** do NOT build a custom/fine-tuned LLM — it appears profitable only by memorizing history (decays 50-72% out-of-sample; fine-tuning makes generalization collapse). No specialized financial LLM shows real out-of-sample trading-decision edge. Only genuine niche = unstructured-text (news/filings) synthesis, modest after lookahead removed. If invested anywhere → lookahead-clean news/regime FILTER on the proven condor, not the −EV directional leg.
- **THE THESIS (unchanged, now heavily evidenced):** the edge is SELLING premium (condor, theta works FOR you), not BUYING it (directional 0DTE, −EV regardless of model/timeframe/data). The condor v2 gate is the week's one real forward step.
- All work committed+pushed to main (latest `6cc1d20`). Daemon paper Mon-Fri via systemd (7:45-16:15 ET).

---
## Status as of 2026-06-18 (major strategy rethink — full detail in [[project_strategy_review_0618]])

The project pivoted hard this week from "ship knobs" to "do we even have an edge." Summary:
- **Strategy verdict:** the LLM-directional and the new deterministic trend-follow engine have **NO durable edge** — formally rejected by a walk-forward + Deflated-Sharpe gate (`scripts/validate_strategy.py`): trend engine IS Sharpe +1.11 → OOS −0.25, DSR 1.3%. **GO-LIVE on real money is on HOLD.** The earlier "$5k test→live" plan (06-14 section below) is SUPERSEDED.
- **Architecture WON though:** demoted the LLM out of the decision hot-path → **deterministic rules engine** for entry (`agents/directional/signal_engine.py`) AND exit (`exit_monitor._rules_exit_confirm`), behind the `deterministic_engine` flag (`.env DETERMINISTIC_ENGINE=true`). LLM now = research/commentary only. Matches field-standard production (confirmed by deep-research). Live on paper from 06-18; both calls+puts, flat sizing, 15-min. **Paper = architecture validation ONLY (strategy failed the gate; don't read P&L as edge).**
- **The ONE real edge found:** **VRP harvesting** (sell premium, don't predict direction). Deterministic SPY 0DTE iron condor on **real VIX1D** (`scripts/backtest_vrp.py`, data/vix1d.csv) PASSES the hard gate (OOS Sharpe +3.18, DSR 99.8%, generalizes) — but is **cost-fragile** (degrades to marginal at realistic 4-leg fills). It's an EXECUTION-cost problem, not an absence-of-edge problem.
- **⏳ OPEN THREAD (resume here):** designing a **HIGH-RISK premium-selling strategy** (short strangle/straddle + stops instead of wings → fewer legs/lower cost, more credit; trend/ADX repurposed as a vol-regime filter). Full spec + the core "buy-vs-sell premium" insight + research in [[project_strategy_review_0618]] → "OPEN THREAD" section. **NEXT: write the strangle spec + backtest it through the gate.**
- Key memories: [[project_strategy_review_0618]] (the whole arc + go-live HOLD), [[project_entry_model_replay]] (Sonnet swap + replay harness). All work committed/pushed to main (latest `3fee6c2`); backtest/research scripts in `scripts/` (some untracked). Daemon runs paper Mon-Fri via systemd.

---
## Status as of 2026-06-14 (HISTORICAL — superseded by the 06-18 go-live HOLD above) — $5k test→live cycle + .env reconciliation

**Plan set by user:** scale capital DOWN to **$5,000** for a clean test cycle.
- **Next week (week of 06-15):** paper-test the full system at $5k.
- **Week after:** go **LIVE with real $5,000**.

**`.env` changes made 06-14 (committed to disk, daemon picks up on next restart):**
- `TRADING_CAPITAL_USD` 25000 → **5000**.
- `BASELINE_RESET_AT` 2026-05-22 → **2026-06-14T00:00:00Z** so dynamic effective capital starts **clean at exactly $5000** (verified via `get_effective_capital()` = 5000). Old +$123 realized-since-reset dropped; full history stays in DB.
- Removed **dead** `DAILY_LOSS_LIMIT_USD=500` var — code never read it; the real daily limit is `daily_loss_limit_pct=0.15` (15%) in config.py. Added a comment block explaining this + that `MAX_POSITION_SIZE_USD`/`MAX_CONCURRENT_POSITIONS` are legacy notional caps superseded by the % caps for directional.

**Live config is still `directional_mode=aggressive`** (NOT selective — .env override; aggressive = +100% PT / −50% hard floor). Memory previously assumed selective.

**Derived money limits at $5k:** max single-trade premium ≈ 10% = **$500**; max simultaneous exposure 30% = **$1,500**; daily-loss auto-halt 15% ≈ **$652 real** (shrinking-capital math) / $750 nominal; weekly 25% ≈ $1,250. No cap on # trades/day (`max_trades_per_day=0`).

**Before going live (week-after checklist, NOT yet done):** flip `TRADING_MODE=live`, swap `ALPACA_BASE_URL` to the live endpoint, install real live Alpaca keys.

**IMPORTANT — `/approve` does NOT apply to the directional engine.** `/approve` (D-014 pending-order flow) is OUR design choice, not an Alpaca requirement, and it's wired ONLY into the iron-condor strategist path (`agents/options/executor.py`), which is **disabled** (`enable_iron_condor=False`). The directional executor explicitly has **no approval gate — "Both paper and live modes execute immediately"** (`agents/directional/executor.py:11-12`). So going live = **fully autonomous real-money execution, no manual prompt.** If a human checkpoint is wanted for the first live week, it needs a small code change to add an approval gate to the directional path. (Corrects an earlier memory/RUNBOOK claim that "live requires /approve".)

**DECISION 2026-06-14 (do not re-litigate):** user chose **full-auto behind the guardrails for live** — no approval gate. Rationale: "that's what the testing is for." The $5k paper test next week validates the guardrails; real-money week after runs the same way, fully autonomous. Backstops relied on: $500/trade cap, $1,500 exposure, ~$652 daily-loss halt, ~$1,250 weekly halt, `/kill` + `/pause`, −50% hard floor.

## Status as of 2026-06-12

Spent the week (06-08 → 06-12) building a loss-prevention package on the directional engine after live losing days, then fixed a critical news bug. **Daemon on `5199e6a`.** Full detail in [[project_loss_prevention_package]]; the scheduled threshold calibration is in [[project_adx_calibration_pending]].

- **Loss-prevention fixes** (all live, all `settings.*` knobs, all provisional): continuous trailing stop (peak−10%), thesis-invalidation force-exit, re-entry **freshness gate** (motivated by the 3rd-trade-of-day being 0-for-4), conviction/RSI/**ADX** sizing, **0DTE early-cut**, **chop filter** (evidence + ADX), exit-job race idempotency, DTE-explicit exit prompt, #trades per-trade + daily/weekly reporting.
- **NEWS BUG (critical):** the bot was effectively **news-blind** — `get_recent_news` mis-parsed `NewsSet.data["news"]` and returned 1 empty article, so the LLM had no readable headlines (only the WebSocket stream worked, as a *trigger*). Fixed `5199e6a`. Now returns real headlines.
- **Results:** 06-11 was 0W/4L −$3,347 (choppy false-breakouts); 06-12 (news fix live) was 3W/1L, ADX gate blocked a chop entry. Small wins on 06-12 = small *moves* (+9–24% peaks), not capping.
- **A background "notify-only" watch** (journal tail for thesis_invalidated/zdte_early_cut/reentry_throttle/skipped_chop/skipped_low_adx) was running this session but is **session-only → dies on machine restart**; re-arm only if the user asks.
- **Next:** let 06-17 ADX calibration run; watch whether the news fix improves entry quality (entry-quality in chop is the remaining weak spot — the LLM still takes failed breakouts, just smaller/cut faster). Possible future lever: ADX-proactive is in; could refine with news-aware HOLDs on conflicting headlines.

## Status as of 2026-06-04 (validation week 1 complete)

The rebuilt architecture has been live on real (paper) data Mon 06-01 → Thu 06-04. Trades fire; protections, scale-outs, and field persistence all validated in production. Full week + fixes are logged in `data/strategy_kb.md` (incident **I8** + the 06-01→06-04 change-log entry). Headlines:

- **Mon 06-01:** 3 SPY CALL trades, net **−$235** (controlled losing day — every stop/scale-out/persist worked). First trades since rebuild.
- **Tue 06-02:** 1 trade (#43, **+$98**, first winner). Health check caught a **duplicate scale-out tier** race (30s tick vs 5min monitor) → fixed with per-trade lock (`1ab457e`).
- **Wed 06-03:** 0 trades. 7 BUY_PUT signals all blocked by a strike-range bug; 52 near-misses, all correctly-held puts in a choppy NEUTRAL range. **Daily-trade count cap (6) was NEVER the constraint** — don't reach for "raise the budget"; the bottleneck was execution.
- **Thu 06-04:** Fixed the put strike-range bug (`f2d2170`, I8) — **puts can finally fill** (every directional trade pre-fix was a CALL). Also: OPRA feed checked = no access (indicative is sufficient).

**Other upgrades shipped & live this week:** health-check daily cron (`77d4dc2`), LLM context enrichment — intraday path / positions / key levels (`e792807`), full trade-lifecycle #signals messaging (`fcd4898`), chain-retry + no-dangling-plan (`7ebc1b4`). Daemon on commit `f2d2170` (paper).

**Next:** watch whether puts now actually fill & whether the LLM's heavy put-bias on non-trending days is a strategy issue (06-03 was 100% puts in a market that didn't break down). Pending (not started): duplicate-tier fix only covered scale-out — the full-EXIT path is not yet under the same lock.

---

## (historical) Status as of 2026-05-29 EOD (Friday)

After 6 trading days (Tue 2026-05-26 through Fri 2026-05-29) with **0 trades**, shipped a coherent strategy-relaxation package on Friday EOD as commit `76bbc7d`. Daemon restarted on the new code. Monday 2026-06-01 open is the first real-data validation.

**Why:** Each fix earlier in the week (criteria_met counter, indicator bootstrap, RTH filter) was validated only by unit tests + offline data snapshots — never by an actual trade firing. Friday's session finally surfaced the binding constraints when the LLM produced 8 BUY_PUT execute attempts and ~30 near-misses but every single one hit a gate calibrated for the multi-stock era. The package re-tunes the 6 binding gates for SPY 0DTE while strengthening the catastrophic-loss defense.

**How to apply:** When picking this back up Monday morning, the priorities are (a) verify trades actually fire, (b) verify all the recent fixes populate their extra fields, (c) run the health check on the first close. Do NOT add more code changes until at least one trade has cleared the full lifecycle.

## Update 2026-05-31 (Sat) — signal-quality workstream started

Two commits shipped Saturday (direct to main):
- `77d4dc2` — `scripts/trade_health_check.py` wired into the scheduler as a daily 16:15 ET job (`_trade_health_check_job` in `trademaster/scheduler.py`), posts findings to #logs. Also fixed a percent/fraction unit-mismatch in its scale-out tier check.
- `e792807` — **context enrichment for the directional scan LLM.** Diagnosis: the scan reasoned off a single frozen snapshot with no memory of the day's price path, no awareness of its own open positions / today's outcomes, and only a scattered levels picture. Added three tight, fail-open prompt blocks in `agents/directional/intraday.py`: (1) intraday price-path narrative `_summarize_price_path`, (2) position + today's-outcome awareness via `get_directional_trade_context` in `db.py` + `_format_position_context`, (3) consolidated S/R map `_build_key_levels_block`. Prompt STEP 1 + STEP 4 updated to use them.

- `fcd4898` — **full trade-lifecycle #signals messaging** (the richer format). Per-event signal stream: PLAN (`format_directional_plan` — "enter as SPY trades $X" + per-indicator green-light checklist + next level watched) → ENTRY fill (`format_entry_combined`) → one SCALE-OUT signal per tier (`format_scale_out`) → final close. `TickerDecision` gained an optional `analysis` dict (indicator snapshot + entry trigger + nearest target) attached to actionable decisions in `run_directional_scan`. Decision resolved: **entry stays immediate** (no price-triggered pending-entry engine) — the plan posts moments before the fill. Exits stay premium-%-based, reported as they fire (NOT price-predicted). Respects [[feedback_signal_jargon]] — plain-English indicators only, no greeks/spread terms.

The signal-quality workstream the user asked for is now **complete** (context enrichment + lifecycle messaging both shipped). Next natural step if revisited: validate the new signals against a real Monday session, and consider whether a price-triggered pending-entry mechanism is worth adding (deferred — would change trade timing, not just messaging).

## What's live for Monday open (commit 76bbc7d)

| Gate | Before | After | Hypothesis |
|------|--------|-------|------------|
| `volume_ratio` threshold | 1.3 | **1.0** (permanent) | D10 dead |
| RSI Calls band | 45-72 | **40-80** | D11 dead |
| RSI Puts band | 28-55 | **20-60** | D11 dead |
| EMA cross | Hard gate | **Conviction modifier** (3/4 with EMA disagreement = MEDIUM fires) | D12 dead |
| ORB override vol | 2.0 | **1.5** | D13 dead |
| Strike floor MIN_ASK | $0.50 | **$0.30** | D14 dead (replaced by max-loss cap) |
| **NEW** MAX_LOSS_PER_TRADE_USD | — | **$500** | Direct defense vs trade #37 pattern |

Daily 15% / weekly 25% / per-trade 30% exposure cap / per-(ticker, action) cooldown / scale-out tiers all **unchanged**.

## What hasn't been tested in production yet

Everything we fixed in the last 2 weeks is still un-validated until an actual trade fires:
- `peak_pnl_pct: 0.0` initialization at entry (`agents/directional/executor.py::_persist_entry`) — Bug B from W21
- `original_qty`, `scale_out_tiers_fired`, `partial_realized_pnl_usd` persistence on tier crossings — Trade #38 discovery
- Per-(ticker, action) 30-min cooldown — Bug A from W21
- Indicator bootstrap with `warmup_days=1` + RTH filter at production-LLM level (verified at offline snapshot only)
- `criteria_met` counter against production threshold
- **NEW** MAX_LOSS_PER_TRADE_USD cap activation

The trade health check at `scripts/trade_health_check.py` is the automated probe for the first 4. Run it on the first close.

## What to watch first thing Monday

1. **9:35-10:30 ET**: ORB-path entries (vol≥1.5 + price broke ORH/ORL). With the lowered ORB vol threshold this should be reachable for the first time.
2. **First trade's `extra` JSON**: confirm `peak_pnl_pct=0.0`, `original_qty=N`, `conviction in {HIGH, MEDIUM, LOW}`. Also check `directional_execute_qty_capped_by_loss_cap` log line if budget would have exceeded $500.
3. **Watch for `directional_indicators_unbootstrapped`** — should NOT fire under normal conditions; if it does, the warmup fetch broke.
4. **`scripts/trade_health_check.py --since 2026-06-01`** after first close — automated validation of all the recently-fixed extra fields.
5. **Weekly review cron fires Friday 17:00 ET** — first review with actual tradable data.

## Reference: where everything is documented

- **`data/strategy_kb.md`** — full hypothesis state (H1-H5 active, D1-D14 dead, I1-I7 incidents, engineering evolution log, change log)
- **`data/reviews/`** — weekly review markdown (W21 is the only one so far, from the broken-pipeline era)
- **commit `76bbc7d`** — the strategy relaxation package, includes detailed rationale in commit message
- **commit `f2783af`** — criteria_met counter fix
- **commit `9c7d621`** — indicator bootstrap (warmup_days=1 + Fix B guard)
- **commit `29e45a7`** — RTH filter + Alpaca pagination anchor
- **commit `8ed3c17`** — per-(ticker, action) cooldown + peak_pnl_pct init

## What's been ruled out / don't re-propose

- ALL of D10-D14 (vol 1.3, narrow RSI, EMA hard gate, ORB vol 2.0, $0.50 floor) — empirically falsified by 6 zero-trade days
- Multi-ticker watchlist (the original loss pattern was clear)
- Hard dollar stops at the trade level (percentage + trailing stop already cover this; MAX_LOSS_PER_TRADE_USD is the explicit catastrophic-loss cap)

## Active config snapshot (updated 2026-06-05 — "go aggressive" changes)

> ⚠️ SUPERSEDED 06-14: base capital is now **$5,000** (see top section). The $25k/$2,450/$3.7k/$6.1k figures below are historical.

- Base capital: **$25,000** (TRADING_CAPITAL_USD in .env; was $10k). Effective ≈ base + cumulative realized P&L.
- Mode: **aggressive** (HIGH + MEDIUM execute)
- Watchlist: **SPY only**
- Exposure cap: 30% (≈ $7.4k per position at $25k)
- **Per-trade loss cap: 10% of effective capital** (`settings.max_loss_per_trade_pct`, was a flat $500) ≈ $2,450 — auto-scales with capital. This is the binding throttle on position size.
- Daily loss limit: 15% (≈ $3.7k) · Weekly: 25% (≈ $6.1k)
- **Event blackout (NFP/CPI/FOMC): DISABLED** (`enable_event_blackout=False`) — trades event days now (the LLM took a profitable NFP put #47 on 06-05). Re-enable to restore the skip.
- **Exit logic overhauled 2026-06-05** (all live, all configurable):
  - **Ride-then-scale-once ladder** (`ad6ab6c`): protected the whole way up (trailing-stop locks at +25→+8/+50→+20/+80→+45), but **sells only ONCE — 25% at +100%** — then rides the rest. (Evolved from v1 25/50 → this.)
  - **Perpetual trailing stop** (`040cd22`): above +100% trails continuously at peak−20% (`trailing_stop_trail_gap_pct`), no cap — +200%→lock+180%, +242%→+222%. Was capped at +75%.
  - **0DTE force-close 15:30 → 15:45 ET** (`040cd22`); 15:50 scheduler sweep is the backstop.
  - **LLM exit-check every 1 min** (`8d57577`, was 5); 30s mechanical tick unchanged. Option D (per-trade monitor coroutine) spec'd at `docs/specs/per-trade-monitor.md`, build-gated.
  - **Governor counts scale-out partials** (`7dcda61`) — daily/weekly/cumulative realized P&L now include locked scale-out gains.
  - All ladder/gap params via `settings.*`. Watch health-check peak-vs-realized capture over ~10-15 trades.
- Trading window: 9:35 AM – 3:15 PM ET

**Validation milestone (06-05):** first PUT ever filled (#47, ATM $745) — the put strike-range fix + blackout removal + scale-out race fix all validated live in one trade (clean scale-outs [15,30] under old ladder, +$72 partial). Commits: `aae584f` (blackout off), `180eb29` (ladder), `4ede09a` (% cap). Macro-feed cron disabled (never fetched news). API cost now ~$0 (was $83/mo Hermes macro feed); coding is on Max plan.

## Daemon state at memory-write time

- Active, PID 1907789, running commit `76bbc7d`
- systemd timer + cron both configured: start 7:45 AM ET, stop 4:15 PM ET Mon-Fri
- Restart=always + RestartPreventExitStatus=0 so crash recovery is automatic
