---
name: loss-prevention-package-news-fix-2026-06-08-06-12
description: "Week of fixes after live losses — trailing stop, thesis-invalidation, re-entry freshness gate, conviction/RSI/ADX sizing, 0DTE early-cut, chop filters, exit-race fix,"
metadata: 
  node_type: memory
  type: project
  originSessionId: a424fa8f-032b-48d2-860b-a2ba6e3b87cd
---

A week of loss-prevention work on the directional engine, driven by analyzing real losing trades. All shipped to main + daemon restarted each time. Every threshold is a `settings.*` knob and is **provisional / pending calibration** — do not treat the defaults as tuned.

## Fixes shipped (commit → what)
- **Continuous trailing stop** (`2494036`, 06-08): stop trails `peak − trailing_stop_trail_gap_pct` (default **0.10**) across the whole in-profit range, not just above +100%. Engages only above the +25% tier. Fixed #51 giving back +70%→+20%.
- **Per-trade #trades reporting** (`b1964fd`): full close report + daily (16:05 ET) & weekly (Fri 16:10) tabular summaries; P&L includes scale-out partials.
- **Loss-prevention A–D** (`5525d5a`, 06-09): (A) **thesis-invalidation force-exit** — a LOSING position with ≥2 reversal signals (incl. new `rsi_reversal_*` rules, RSI-9 crossing 55/45) is cut with NO LLM (reason `thesis_invalidated`); (B) re-entry throttle; (C) **conviction/RSI sizing** — MEDIUM ×0.5, weak-RSI (put RSI≥50 / call RSI≤50) ×0.5; (D) **exit-job race fix** — `_close_trade_row` is idempotent (first close wins) + both jobs re-read before submitting. D revealed #57 was a REAL hard-floor loss mislabeled phantom by the race (books were understating losses).
- **Re-entry freshness gate** (`9c0d733`, 06-10): replaced the count throttle. After **2** consecutive same-direction trades, a further same-direction entry is allowed only if a FRESH leg — pullback ≥30% of day range OR new-extreme break w/ volume ≥1.5 (`is_fresh_leg` in intraday.py). No conviction exemption. Motivated by: **the 3rd trade of the day was 0-for-4, −$5,198** (always a late chase of an exhausted move; peaks 0–10% vs 16–242% for trades 1–2). The 4th trade is the BEST bucket (catches a fresh leg) — so a blunt block would kill winners; freshness distinguishes them.
- **DTE-explicit exit prompt + 0DTE early-cut** (`bdbf2be`, 06-11): #64 (0DTE call) was held −37%→−49% because the LLM read the ISO expiry "2026-06-11" as "June 2026, ample time." Prompt now says "0DTE — EXPIRES TODAY, theta lethal." Plus: a **0DTE indicator-independent early-cut** — past −25% (`zdte_early_loss_cut_pct`) with price through VWAP against it → cut, no LLM (covers early session when RSI/EMA aren't warmed up so the ≥2-signal gate can't fire).
- **Evidence-based chop filter** (`728ced0`, 06-11) + **ADX gate & pause bump** (`9e6e6b3`): after **2** failed breakouts today (peaked <10% + loss), pause ALL new entries for **chop_pause_minutes=90** (was 45 — trades are ~70 min apart). **ADX gate**: skip entries when ADX<18 (`adx_block_below`), downsize ×0.5 when ADX<25 (`adx_full_above`). ADX added to indicators.py (Wilder; verified trend→100, chop→~0). Three layers now: ADX (proactive) + evidence filter (reactive) + exit fixes (damage control).
- **entry_adx / entry_rsi9 persisted** (`531a85f`): on every trade for threshold calibration. See [[project_adx_calibration_pending]].
- **NEWS BUG FIX** (`5199e6a`, 06-12) — **critical**: `get_recent_news` did `items = raw.data` but alpaca-py's `NewsSet.data` is `{"news":[...]}`; iterating a dict yields KEYS → **1 empty article every fetch**. The trade-decision LLM, premarket, and intraday scans had **NO readable headlines the whole time** (logged `news_count:1`). The real-time WebSocket stream (`alpaca_stream`) DID work as a *trigger* (19 triggered scans on 06-12) — so the bot reacted to news *timing* but was blind to news *content*. Fixed via `_unwrap_news()`; now returns 40 real headlines. Tests had mocked the wrong shape (`.news`) so never caught it. (Corrects [[project_hermes_learning_loop]]'s "bot's real news = Alpaca stream" — it was broken until 06-12.)

## Results
- 06-11: full red day, **0W/4L −$3,347** — all failed breakouts in choppy tape (alternating CALL/PUT, every peak <10%). But losses shrank intra-day as fixes kicked in (#64 −$1,490 full-size-rode-to-floor → #66 −$88 downsized+1-min-cut).
- 06-12 (news fix live): **3W/1L** (#69 +210, #70 +162, #71 +238, #68 −954). Winners had entry ADX 25–40; ADX gate correctly blocked a chop entry at ADX 16.3. Small wins because **moves were small** (peaks +9–24% vs +100–243% on big days) — NOT because fixes capped them (trailing stop never engaged <+25%; only MEDIUM half-sizing trimmed #70/#71, by design).

**Why this matters:** the fixes optimize loss-reduction; they trim upside on MARGINAL setups (MEDIUM/weak-RSI/low-ADX) by design but do NOT touch HIGH-conviction full-size trending runners (#50/#54/#56 would still run). [[project_lessons_learned]]
