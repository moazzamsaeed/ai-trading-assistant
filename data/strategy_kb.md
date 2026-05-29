# TradeMaster Strategy Knowledge Base

> Hermes reads this on every weekly review. KB edits are proposed in Discord
> and applied manually after approval. Cite n (sample size) for every claim.
> Don't promote a pattern to "confirmed" with n<5.

**Last updated:** 2026-05-23 (seeded from lessons-learned doc + 90-commit history)
**Total trades observed:** ~46 (21% win rate, net −$2,600 paper)
**Sample-size warning:** Below n=100, treat all patterns as provisional.

---

## Current strategy snapshot
*(Auto-refreshed from .env + strategy.yaml on each review)*

- **Watchlist:** SPY only (since 2026-05-22)
- **Mode:** aggressive — MEDIUM + HIGH conviction execute
- **Base capital:** $10,000 (baseline reset 2026-05-22T18:18:12Z)
- **Per-trade exposure cap:** 30% = $3,000 (no per-trade fraction)
- **Trailing tiers:** (+15%, lock +3%, sell 25%) → (+30%, +10%, 25%) → (+50%, +25%, 25%) → (+75%, +40%, hold) → (+100%, +60%, hold)
- **Trailing tick:** 30s (RTH only)
- **Exit monitor:** 5-min indicator + LLM smart_exit (Claude Sonnet 4.6)
- **Daily loss limit:** 15% / Weekly: 25%
- **Entry window:** 9:35 – 15:15 ET
- **Max trades/day:** 4 (MEDIUM cap: 2)

---

## Confirmed patterns
*(Promote here once n≥5 AND result is consistent across regimes.)*

*(none yet — sample size insufficient)*

---

## Active hypotheses (being tested)

**H1. Scale-out tier 1 at +15% is the right first tier**
- Origin: trade #37 peaked +27% then went −32%. With +15% first tier, 25% would have been locked at +18% peak.
- Evidence: n=0 with new architecture (Monday 2026-05-26 is day 1).
- Disproves if: <40% of winning trades hit +15% in 60 days.

**H2. 30-second trailing tick catches peaks 5-min poll missed**
- Origin: trade #37 — 5-min check captured +5.66% but actual peak was +27%.
- Evidence: n=0 with new architecture.
- Disproves if: peak-to-realized gap on wins doesn't shrink vs pre-pivot.

**H3. SPY-only beats multi-ticker**
- Origin: failure mode #1 — individual stocks indistinguishable from coin flip directionally.
- Evidence: pre-pivot multi-ticker win rate ~21%. Need SPY-only baseline.
- Disproves if: SPY-only win rate stays ≤25% after n=40.

**H4. Standard intraday indicators (VWAP/RSI/EMA/MACD) lack edge**
- Origin: 21% win rate across 33+ trades with this indicator set.
- Evidence: **CONTAMINATED by I7 (indicator bootstrap silent failure).** The 33+ pre-fix trades were taken with `ema50` and `volume_ratio_20` returning None → 0 for the first 1.5–4 hours of every session. The "indicator stack" wasn't actually evaluable during that window — entries mostly fired late in the day when indicators had bootstrapped, which itself selects for trend-exhaustion setups. The 21% win rate reflects a broken pipeline, not the indicators themselves.
- **Reset the sample.** Treat H4 as un-tested post-2026-05-28. Need ≥30 trades under properly-bootstrapped indicators (including morning sessions) before any verdict.
- Disproves if: post-fix SPY-only win rate jumps to ≥45% on n≥30. If it stays ≤30% on n≥30 fresh trades, H4 confirmed for real.

**H5. MEDIUM conviction 0DTE on SPY is acceptable (vs blocked for stocks)**
- Origin: failure mode #4 — MEDIUM block was correct for illiquid stocks; assumed not needed for SPY.
- Evidence: n=0 with new config. **Risk:** if MEDIUM trades dominate losses, this assumption is wrong.
- Disproves if: MEDIUM trades have win rate <15% over n=10.

---

## Dead hypotheses (tested, rejected)

- **D1. Tue/Thu HIGH-only conviction filter** — based on multi-stock backtest, no edge on SPY. Removed 2026-05-21 (commit 518889c).
- **D2. All-or-nothing trailing stops for 0DTE** — trade #37 falsified this. Replaced with scale-out tiers.
- **D3. 5-min polling sufficient for 0DTE exit** — bid moves 30% in 2 min on 0DTE. Replaced with 30s tick + 5-min indicator.
- **D4. Global 3:30 PM force-close** — was killing weekly positions with days remaining. Cost $110+. Replaced with per-trade expiry check.
- **D5. rel_vs_spy gate while trading SPY** — irrelevant. Removed.
- **D6. Hard profit target** — Replaced 2026-05-15 (24807c3) with indicator-driven smart_exit. Fixed PT was leaving money on the table on trends and exiting late on reversals.
- **D7. DeepSeek Flash for exit decisions** — Replaced 2026-05-15 (3d92ec2) with Claude Sonnet 4.6. Sonnet provides better thesis-reversal reasoning at acceptable cost.
- **D8. 10-min news polling** — Replaced 2026-05-12 (d4ae218) with WebSocket stream triggers. Polling missed event-driven moves.
- **D9. Iron condor on $5k account** — Math doesn't work at this capital size. Paused 2026-05-11; revisit if account grows past ~$25k.
- **D10. `volume_ratio > 1.3` for SPY 0DTE** — Inherited from the multi-stock era where catalysts pushed volume 2-3x. Falsified on 6 trading days of 2026-05-26 through 2026-05-29: SPY 5-min `volume_ratio_20` rarely sustains ≥1.3 (today's reading hovered 0.3–1.7, mostly 0.5–1.2). With 1.3 the criteria gate produced 0 trades in 6 days. Lowered to **1.0** on 2026-05-30 (commit pending) as the permanent threshold for SPY.
- **D11. RSI9 band 45-72 (calls) / 28-55 (puts)** — Too narrow for SPY 0DTE momentum. On fast moves RSI overshoots the band before all other criteria align (today's morning breakout RSI = 89, afternoon selloff RSI = 18). Widened to **40-80 (calls) / 20-60 (puts)** on 2026-05-30 (commit pending).
- **D12. EMA cross as a HARD entry gate** — EMAs lag actual price action by their period. Requiring EMA cross alignment for entry means waiting for confirmation after the move is well underway. Demoted to a **conviction modifier** on 2026-05-30: 3/4 criteria with EMA disagreement still fires as MEDIUM. The 4/4 case (EMA aligned) becomes HIGH conviction.
- **D13. ORB override volume threshold 2.0** — Sustained 2.0× volume on SPY 5-min bars is rare even on confirmed breakouts. Lowered to **1.5** on 2026-05-30 to make the ORB path realistically reachable.
- **D14. `$0.50/share strike floor` as the catastrophic-loss defense** — Conflated two concerns: (a) Alpaca paper account position tracking (the I3 ghost-position pattern), and (b) the trade #37 high-contract-count loss pattern. The premium floor bounded loss by proxy and blocked too many valid 0DTE OTM setups. Replaced on 2026-05-30 by an **explicit `MAX_LOSS_PER_TRADE_USD = $500` cap** in `agents/directional/executor.py` (bounds qty × premium × 100 ≤ $500 = bounded realized loss when the option goes to zero). The MIN_ASK was lowered to $0.30 (still above suspected paper-tracking threshold).

---

## Incidents (n=1 events worth remembering)

**I1. Trade #37 (2026-05-22) — $952 single-trade loss**
- 56 contracts × $0.53 OTM 0DTE
- Peak +27% → −32% in 40 min
- All-or-nothing trailing missed the peak
- Drove the entire trailing-stop rewrite + scale-out architecture
- **Watch: if contract×notional ratio ever again allows >$500 single-trade loss, alert.**

**I2. Premarket briefing cron silently not firing (2026-05-19, 05-20 mornings)**
- Linger enabled, cron running, jobs not executing
- Switched to systemd user timers with Persistent=true
- **Watch: if briefing missing from #research by 8:05 ET, alert.**

**I3. Ghost-position BUY/SELL swap (caught in audit, never executed)**
- Executor was calling `submitter()` instead of `seller()` for ghost recovery
- Would have doubled positions instead of closing
- **Pattern: any code touching positions must distinguish BUY from SELL explicitly in tests.**

**I4. SIP bars silent failure (multiple days pre-2026-05-14)**
- `StockBarsRequest` defaulted to SIP feed (paid). No subscription → empty bars returned with no error.
- LLM received empty indicators, hallucinated bullish/bearish setups, fired bad trades.
- **Pattern: this is the canonical "silent failure" — every new data fetch must be audited for this class of bug.**

**I5. Trade #39 (2026-05-22) — $1,215 single-trade loss via hard_floor_stop**
- 27 contracts × $0.92 SPY call. Loss = ~49% of position value.
- Exceeded the $500 single-trade loss alert threshold established after I1.
- Same strike/expiry as trade #38 (`SPY260522C00748000`), opened 16 min after #38 opened, 10 min after #38 closed (+$232).
- Investigation: not a concurrent-cap-bypass (#38 had closed before #39 opened). Actual failure mode = **same-side re-entry on identical OCC contract just past the 15-min per-ticker cooldown**. Per-(ticker, action) cooldown of 30 min added 2026-05-23 to prevent.
- **Watch: any back-to-back BUY_CALL or back-to-back BUY_PUT on the same ticker within 30 min should now be blocked at the scheduler.**

**I7. Missed SPY breakout — indicators unbootstrapped (2026-05-28)**
- SPY 750.23 → 754.84 (+0.61% intraday). Clean breakout bar at 10:10 ET (low 750.13 → high 753.99 in single 5-min bar, 38k vol = 1.7× average).
- System DID see it: `volume_surge_2.7x` stream trigger fired 10:13 ET, LLM scanned 4 times in 2 min (10:13, 10:14, 10:15, 10:15). All returned HOLD. Zero near-misses logged.
- Root cause: `ema(bars, 50)` returns None until 50 bars accumulate (≥ 13:35 ET from a 09:30 open at 5-min). `volume_ratio(bars, 20)` returns None until 20 bars (≥ 11:10 ET). In `_log_near_misses`, `float(snap.get("ema50") or 0)` converted None → 0, then `ema_bull = ema20 > ema50 > 0` evaluated False permanently. Max possible criteria_met = 2/4 during the breakout window → below the ≥3 near-miss logging threshold → zero audit trail of what the LLM saw.
- **This is a STRUCTURAL flaw: every trading day's first 1.5–4 hours were effectively un-tradable.** Explains why every W21 winning trade opened after 13:00 ET — only window where indicators had bootstrapped.
- **Fix shipped 2026-05-28** (commit `9c7d621`):
  - `get_recent_bars(warmup_days=1)` spans back 1+2 calendar days; IEX feed returns only RTH bars → cross-day fetches don't mix extended hours.
  - `indicators.snapshot(session_start_et=...)` keeps VWAP session-anchored; EMA/RSI/MACD/vol_ratio use the full bar history.
  - ORB and today's open continue to use a today-only slice (`[b for b in bars if to_et(b.timestamp) >= session_open_et]`).
  - **Fix B guard**: if `snap["ema50"] is None or snap["volume_ratio_20"] is None`, ticker is added to `no_indicators_tickers`, prompt warns "INDICATORS NOT BOOTSTRAPPED — MUST HOLD", any non-HOLD decision hard-overridden to HOLD with `reason=indicators_unbootstrapped`. None can no longer silently become 0 in the criteria gate.
- **Watch:** if `directional_indicators_unbootstrapped` log line appears after market open Mon-Fri, prior-day bar fetch failed — escalate.

**I6. First weekly review validated the loop (2026-05-23)**
- Week 1 of the strategy KB + weekly review skill (review at `data/reviews/2026-W21.md`).
- Surfaced 2 real code bugs that were invisible without the structured review:
  - **Bug A — per-(ticker, action) cooldown missing.** Same-side same-OCC re-entry was allowed after only 15 min. Fixed in `trademaster/scheduler.py` by adding `_last_trade_open_by_action` dict with 30-min cooldown. Drove I5.
  - **Bug B — `peak_pnl_pct` silent failure.** Losing trades never wrote peak (default 0 ≠ stored 0 was indistinguishable from "tick never ran"). Fixed in `agents/directional/executor.py::_persist_entry` by initializing `peak_pnl_pct: 0.0` at entry. Affected 6/10 trades in W21 review — invalidated H1/H2 evaluation that week.
  - Third investigation (`UNKNOWN` conviction on trades #1–#35) turned out to be a historical artifact, not a current bug — trades predated commit `3821a76` which added the field. No fix needed.
- **Pattern: the weekly review loop is paying for itself.** $0.07 LLM cost surfaced two bugs that had been silently degrading risk management and observability. Trust the n-cited findings; investigate them.

---

## Operational lessons (non-strategy, but affect outcomes)

- Multiple daemon processes = catastrophic (websocket limits, race conditions). Always `pkill -f trademaster.orchestrator` between restarts.
- Alpaca error `42210000` is overloaded — never whitelist on code alone, check message text.
- Silent failures: any function that falls back to zero/empty default needs explicit log warning AND execution block.
- LLM cannot self-police missing data — enforce in code, not just prompt.

---

## Engineering evolution log

> History of issues found and how they were resolved. Hermes reads this to
> understand which classes of problem are already solved (don't re-propose),
> which design choices are intentional vs accidental, and what patterns to
> apply when proposing new changes.

### Data integrity — the "silent failure" class
*Pattern: empty/zero defaults that don't crash but produce wrong behavior.*
- **SIP feed → IEX feed** (2026-05-14, e50d171) — `StockBarsRequest` defaulted to SIP (paid). Without subscription it returned `bars=[]` with no error → LLM hallucinated indicators. Always pass `feed=DataFeed.IEX`.
- **No-bar-data hard HOLD** (2026-05-14, 33ec52a) — Prompt-based instructions to "ignore missing data" weren't enforced. Now blocked in code before LLM sees the ticker.
- **`get_recent_bars` anchored to RTH open** (2026-05-13, 5856363) — Without `start` param, Alpaca returned pre-market bars → directional agent blind to intraday action.
- **BARS_LIMIT 30 → 60** (2026-05-12, dc62f5b) — EMA50 needed more history than was being fetched.
- **RSI key mismatch** (2026-05-14, 416a916) — Indicator field renamed but exit monitor still read old key, thresholds applied to wrong value.
- **Indicators unbootstrapped — first 1.5–4 hours of every session** (2026-05-28, 9c7d621) — `ema50` returned None until 50 5-min bars accumulated (~13:35 ET); `volume_ratio_20` returned None until 20 bars (~11:10 ET). `_log_near_misses` converted None→0 via `float(snap.get("ema50") or 0)`, capping max criteria_met at 2/4 during morning sessions. Fixed by adding `warmup_days=1` to `get_recent_bars` + session-anchored VWAP in `indicators.snapshot` + hard-block guard `no_indicators_tickers` when either field is None at scan time. See incident I7.
- **criteria_met counter against relaxed vs production threshold** (2026-05-27, f2783af) — `_log_near_misses` computed criteria_met using `vol_relaxed = vr >= 1.0`, overstating by 1 for HOLDs with vol in [1.0, 1.3). Now uses production `vol_meets_threshold = vr >= 1.3` so criteria_met=4 genuinely means "all 4 met". Historical records pre-fix overstate by 1 in the vol-only-miss case.

### Order execution — Alpaca SDK quirks
*Audit any new alpaca-py call against these.*
- **Options use `TimeInForce.DAY`, not IOC** (2026-05-14, c571f39 + 7eeb393) — IOC silently rejected with error `42210000` ("not supported for options"). DAY fills immediately at best bid during RTH, auto-cancels at 4 PM.
- **Error `42210000` is overloaded** — Same code for "position not in book" AND "IOC not supported". Whitelist must check message text, not code alone.
- **Verify position exists before DB auto-close** (2026-05-14, 4e20803) — Sell rejection used to auto-close the DB row, leaving position live in Alpaca.
- **Snap LLM strikes to chain** (2026-05-13, b6f092e) — LLM occasionally picks non-existent strike; snap to nearest valid contract.
- **$0.50/share premium floor** (2026-05-13, 0a8500a) — Strike selection rejected cheaper-than-$0.50 contracts after early losses on cheap deep-OTM.
- **In-process Black-Scholes greeks** (2026-05-11, 4b83935, D-017) — Alpaca indicative feed returns `greeks=None`. We bisect IV from mid price → derive delta.
- **`abs(filled_avg_price * 100)` on credit fills** — Credit spreads return negative price. Naive multiplication records negative entry.
- **`_enum_str(v)` helper for alpaca-py enums** — `str(AccountStatus.ACTIVE)` returns `"AccountStatus.ACTIVE"`, not `"ACTIVE"`.
- **discord.py `_app_ready`, never `_ready`** — `commands.Bot` has its own `_ready`; shadowing it deadlocks startup.

### Risk management evolution (chronological)
- **2026-05-11** — Working-capital cap (`TRADING_CAPITAL_USD`) for paper/live parity.
- **2026-05-12** — Weekly options force-close only on expiry day (was killing them at 3:30 PM).
- **2026-05-15** — Removed global 3:30 force-close entirely; per-trade expiry check handles 0DTE.
- **2026-05-15** — 10 audit bugs fixed + regression tests (cde0a4a / e8fa8f6).
- **2026-05-17** — 7 missing risk controls from GPT audit (2c28cf5).
- **2026-05-18** — First trailing stop (ratchet up as position appreciates).
- **2026-05-19** — Added +20% trailing tier with +5% lock.
- **2026-05-19** — Removed per-trade size fraction (full exposure cap = position size).
- **2026-05-22** — Full trailing stop overhaul: 3-tuple scale-out tiers + 30s tick. *(Current architecture.)*

### Strategy evolution (chronological)
- **2026-05-11** — Paused IC scheduler (math doesn't work on $5k), added aggressive directional mode.
- **2026-05-13** — Signal intelligence overhaul (professional trader framework).
- **2026-05-13** — WebSocket stream triggers replaced 10-min news poll.
- **2026-05-13** — 3-tier news triggers (ticker / macro / general financial); Tier 3 later dropped as noise.
- **2026-05-14** — Indicator stack: RSI-9, MACD, ORB, 15-min, ATR, day-filter.
- **2026-05-18** — Asymmetric burden of proof in regime rules.
- **2026-05-18** — 5 strategy improvements (rel_vs_spy gate, tiered cap, time filter, premarket context, VIX filter).
- **2026-05-19** — Pivot to SPY-only (rel_vs_spy gate then removed as consequence).
- **2026-05-20** — Multi-day SPY context (prev close, MA5/MA10, week trend, gap).
- **2026-05-21** — Removed Tue/Thu HIGH-only filter (didn't apply to SPY).
- **2026-05-21** — Removed MEDIUM 0DTE block; cooldown 60min → 15min.
- **2026-05-21** — Entry window 14:30 → 15:15 ET.

### LLM resilience
- **Premarket research** — Gemini 3.1 Preview chronically 503'd → swapped to `gemini-2.5-pro` (2026-05-11, 73c3a03, D-016). Sonnet fallback added 2026-05-15 (e385163).
- **Directional/intraday** — Claude Haiku fallback when DeepSeek times out (2026-05-14, c5cbbfa).
- **Exit decisions** — Routed to Claude Sonnet 4.6 (was DeepSeek Flash) — Sonnet better at thesis-reversal reasoning (2026-05-15, 3d92ec2).
- **Budget governor** — `MONTHLY_LLM_BUDGET_USD=100`; router refuses non-essential calls past cap.

### Infrastructure
- **Package rename** — `hermes/` → `traderouter/` → `trademaster/` (2026-05-10, D-010/D-011/D-012) to avoid name collision with Hermes Agent ecosystem.
- **ET timezone standardization** (2026-05-13, 8630f3d) throughout the codebase.
- **Dynamic capital accounting** (2026-05-13, 4dc5b7c) — baseline reset for clean slate; per-trade sizing scales with effective capital.
- **Tz-aware SQLite reads** — SQLAlchemy + SQLite drops `tzinfo` on read even with `DateTime(timezone=True)`; `_as_aware_utc()` helper re-adds it.
- **systemd user timers** replaced cron for daemon start/stop (cron was silently not firing some mornings — see I2).

### Architectural patterns Hermes should apply when proposing changes
- **Silent failure → explicit failure** — Any function that falls back to zero/empty default must log a warning AND block execution. Don't return the default and pray.
- **Prompt instructions ≠ enforcement** — Anything critical must be enforced in code, not just told to the LLM in a system prompt.
- **Separate concerns by cadence** — Indicators on 5-min bars, bid-math on 30s tick, LLM analysis on 5-min (cost control). Don't collapse these.
- **Audit message text, not just error codes** — Same code can mean different things (Alpaca 42210000 example).
- **Verify position before DB write** — Never auto-close a DB row based on order rejection alone.
- **Test BUY vs SELL paths separately** — Ghost-position audit caught a BUY-instead-of-SELL bug that would have doubled positions.

---

## Open questions (no data yet, watch for them in reviews)

- Does win rate change by hour-of-day? (entries 9:35–10:00 vs 14:00–15:15)
- Does VIX regime affect scale-out tier hit rates?
- Are losses concentrated in any specific market regime (trending vs chop)?
- LLM smart_exit thesis-reversal calls — what's their hit rate when they trigger?
- **Split-entry cap bypass:** Are trades #38 and #39 (same symbol `SPY260522C00748000`, opened same day) a split of one intended position? If so, combined notional was 56 contracts (~$2,968) — at the exposure cap. Does the system aggregate same-symbol same-expiry open positions against the per-trade cap, or only per-order? Worth verifying in code before it produces a combined loss exceeding I1.
- **Peak data completeness:** 6 of 10 trades in week 2026-W21 (#30–#34, #39) have no `peak_pnl_pct` recorded. Is peak-tracking only activating under the new trailing-stop architecture, or is it failing silently for older/pre-pivot trades? If the latter, this is an I4-pattern silent failure that needs a code audit. Without peak data on losses, H1 and H2 cannot be evaluated.

---

## Change log

- **2026-05-23** — Seeded from `project_lessons_learned.md` + `project_current_focus.md` + 90-commit history. Includes engineering evolution log so Hermes has full institutional context, not just last 2 weeks of failures.
- **2026-05-23** — Applied 3 KB edits proposed by weekly review `data/reviews/2026-W21.md`: added incident I5 (trade #39, $1,215 loss, split-entry watch); added two open questions (split-entry cap bypass; peak data completeness).
- **2026-05-23** — Investigated all 3 code-side findings from the W21 review. Fixed Bug A (per-(ticker, action) 30-min cooldown in `trademaster/scheduler.py`) and Bug B (`peak_pnl_pct: 0.0` initialized at entry in `agents/directional/executor.py::_persist_entry`). Third finding (`UNKNOWN` conviction) was historical artifact, no fix needed. Added incident I6 documenting the loop validation. Updated I5 with revised root cause. 450 tests pass.
- **2026-05-27** — Fixed `near_misses.criteria_met` counter bug (commit `f2783af`): was computing against relaxed `vol >= 1.0`, now uses production `vol >= 1.3`. Surfaced by the daily analysis when two 4/4 records had LLM reasoning that said 3/4 or 2/4. Daemon restarted 14:54 CDT same day.
- **2026-05-28** — Fixed indicator bootstrap silent failure (commit `9c7d621`, incident I7): added `warmup_days=1` to `get_recent_bars`, session-anchored VWAP via `session_start_et` in `indicators.snapshot`, hard-block guard for null `ema50`/`volume_ratio_20`. Daemon restarted 17:24 CDT. Root cause discovered investigating "why we missed the SPY up-run today": system saw the breakout in real time but the criteria gate couldn't evaluate it because indicators were structurally unavailable in the first 1.5–4 hours of every session.
- **2026-05-29** — Fixed two followups to the bootstrap fix (commit `29e45a7`): (a) IEX returns extended-hours bars on multi-day spans — added `_is_rth_et` filter; (b) Alpaca returns oldest-first up to `limit`, so today's bars got dropped — anchored start to `warmup_days` trading sessions back walking weekends. Daemon restarted 09:43 CDT.
- **2026-05-29** — **TEMPORARY**: lowered `volume_ratio` threshold 1.3 → 1.0 in LLM prompt (`agents/directional/intraday.py` lines 120/123/131/132) and `_MIN_VOLUME_RATIO` constant (commit `a595dba`). Goal: validate end-to-end pipeline with at least one real trade after 5 zero-trade days. **REVERT BEFORE MONDAY 2026-06-01 open** if no trades fire today, or hold at empirically-validated level if one fires cleanly. Daemon restarted ~10:00 CDT.
- **2026-05-30** — Friday's session produced 8 BUY_PUT execute attempts (LLM willing to fire), all blocked by the $0.50 strike floor, plus ~30 near-misses showing pattern of RSI-overshoot blocking morning calls and EMA-disagreement blocking afternoon puts. Shipped a coherent strategy-relaxation package to enable trades Monday:
  - Vol 1.0 promoted to permanent (D10)
  - RSI bands widened to 40-80 / 20-60 (D11)
  - EMA demoted to conviction modifier (D12)
  - ORB vol threshold lowered to 1.5 (D13)
  - Strike floor $0.50 → $0.30, paired with new $500 per-trade max-loss cap (D14)
  - All test cases updated; 450 tests pass.
  - Expected effect: 2-4 trades/day under normal SPY conditions, bounded $500 max single-trade loss, daily 15% governor still catches catastrophic days. **First real-data validation of the rebuilt architecture starts Monday 2026-06-01.**
