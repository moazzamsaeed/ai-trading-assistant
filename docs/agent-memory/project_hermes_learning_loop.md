---
name: Hermes learning loop — strategy KB + weekly review + autocommit hook
description: State of the self-improving review loop built 2026-05-23/24. KB + script + Hermes cron + auto-commit hook. One open verification item.
type: project
originSessionId: 788694ac-3dc6-4a75-8ce6-fb5dfedd7148
---
The "Hermes learns the strategy" loop, built over 2026-05-23/24. Resume here when picking this thread back up.

**Why:** User wanted Hermes to accumulate institutional knowledge week over week and propose KB edits from real trade data, while keeping the human as the final gate on KB changes and code changes. Inspired by a YouTube video but executed conservatively (no auto-apply, n-cited claims only, no patterns at n<5).

**How to apply:** When user mentions "weekly review", "strategy KB", "Hermes self-learning", or "the autocommit hook", this is the loop they mean. Don't re-architect — extend.

## What's built and live

1. **Strategy KB** at `~/ai-trading-assistant/data/strategy_kb.md` — version-controlled (gitignore was changed from `data/` → `data/*` with `!data/strategy_kb.md` exception). Sections: snapshot, confirmed patterns, active hypotheses (H1-H5), dead hypotheses (D1-D9), incidents (I1-I6), operational lessons, engineering evolution log, open questions, change log. ~250 lines.

2. **Weekly review script** at `~/ai-trading-assistant/scripts/weekly_review.py` — pulls last 7 days of trades + KB + last 4 prior reviews, calls Claude Sonnet 4.6 via streaming (not the shared `trademaster.llm.anthropic_client` which has a 30s timeout), saves to `data/reviews/YYYY-Www.md` (gitignored). Cost ~$0.07/run. Args: `--week-offset N`, `--dry-run`.

3. **Hermes cron job** `d56f500aa927` — Friday 16:00 CDT (= 5 PM ET) — workdir `~/ai-trading-assistant`, delivers TL;DR to Discord `#research` (channel `1502926024210649098`). Verified working: ran 2026-05-24 with status ok, produced real TL;DR. Cron output saved at `~/.hermes/cron/output/d56f500aa927/*.md`.

4. **First W21 review surfaced 2 real bugs, both fixed and shipped** (commit `8ed3c17` in trademaster):
   - **Bug A:** Per-(ticker, action) cooldown was missing. Trade #38 (+$232 BUY_CALL SPY) and #39 (−$1,215 BUY_CALL same OCC) fired 16 min apart, past the per-ticker 15-min cooldown. Fixed by adding `_last_trade_open_by_action` dict + 30-min cooldown in `trademaster/scheduler.py`.
   - **Bug B:** `peak_pnl_pct` silent failure — losing trades never wrote peak (default 0 indistinguishable from "tick never ran"). Fixed by initializing `peak_pnl_pct: 0.0` at entry in `agents/directional/executor.py::_persist_entry`.
   - Third "bug" (UNKNOWN conviction on trades #1–#35) was historical artifact (predated commit `3821a76`), not a bug. No fix needed.

5. **Autocommit hook** at `~/.hermes/hooks/git-autocommit/{HOOK.yaml, handler.py}` — fires on `agent:end`, runs `git add -u` (tracked-file mods/deletions only — never untracked) + commit with `chore(via-hermes): <platform> session <id> — <files>` attribution. Repos handled: `~/ai-trading-assistant`, `~/projects/hermes-config`. Audit log at `~/.hermes/hooks/git-autocommit/log.jsonl`.

## Autocommit hook — verified scope (2026-05-25)

**Confirmed: Gateway hooks fire on messaging-platform sessions (Discord/Telegram/Slack) but NOT on cron-triggered agent runs.**

Evidence: the macro_context cron (`91c7b17a37f7`) fired ~100 times between 2026-05-24 13:04 and 2026-05-25 19:27, with the updated handler (logs `{"event":"clean", ...}` even when nothing to commit) loaded the whole time. `~/.hermes/hooks/git-autocommit/log.jsonl` never appeared. If hooks fired on cron, it would be full.

Discord-session firing is **still unverified** — depends on user having a Discord chat with Hermes that triggers `agent:end`. Failure mode is independent from cron behavior.

**Doesn't matter for current setup** because all current cron jobs write to gitignored paths (macro_context.json, data/reviews/) — nothing committable from cron anyway. Weekly review proposes KB edits as text in TL;DR; the human applies them manually.

**If ever needed for cron-driven commits:**
- Cleanest: append `git add -u && git commit -m 'chore(cron): ...'` step inside the cron prompt itself. Targeted, no extra infra.
- Or: separate Hermes cron at e.g. 8 AM ET that nags Discord if either repo is dirty.

## Update 2026-06-04 — macro-feed cron was the surprise cost driver; retuned

Investigating an $83.59/month Anthropic bill (Sonnet 4.6, the single `trading-api` key): traced it to the **`91c7b17a37f7` Macro Context Feed cron**, which was running **every 15 min, 24/7** on the default model (Sonnet 4.6) via the direct Anthropic API — 1,966 runs since 05-14, ~93/day, ~$3.8/day. NOT the trading daemon (logs ~$0.30) and NOT Claude Code (that uses the **Max subscription** — OAuth, never hits the API key). Note the chart's "API + Console" = Anthropic Workbench, not Claude Code.

**DISABLED 2026-06-05** (`hermes cron pause 91c7b17a37f7`, enabled=False): the macro feed **never actually worked** — Hermes' `web` toolset is fetch-a-URL, not real-time search, so the agent could never find current news and just wrote an **empty `headlines` array** every run (confirmed: empty responses on both Sonnet and Haiku; the Anthropic bill showed "web search cost $0.00" = zero searches ever performed). So the ~$83/mo bought nothing. The bot's real news comes from **Alpaca's news stream** (free, working) + per-ticker Alpaca news in the scan — the Hermes feed was redundant. The scan reads `macro_context.json` fail-open, so disabling is safe (empty macro block, which it already was). To revive it properly later, give the cron a real news source (NewsAPI/Alpha Vantage key, or point the fetch tool at specific RSS URLs) — NOT just a model/schedule change. The Weekly Review cron (`d56f500aa927`, Fri 16:00 CDT) is untouched and still useful.

**Pre-disable retune (2026-06-04, now moot since paused):** had retuned job `91c7b17a37f7` via `hermes cron edit --schedule` + a direct `jobs.json` model edit, then restarted `hermes-gateway`:
- schedule `every 15m` (interval) → **cron `*/30 6-15 * * 1-5`** (every 30 min, 6:00–15:30 CDT = ~7:00–16:30 ET, weekdays only — kills overnight/weekend runs)
- model default-Sonnet → **`claude-haiku-4-5-20251001`** (3× cheaper/token; fine for headline summarization)
- Net: ~93 runs/day → ~20 weekday runs, on a 3×-cheaper model → expected **~$83/mo → ~$4–5/mo**.
- **Hermes cron times are LOCAL (America/Chicago, CDT), not UTC** — confirmed via the weekly review `0 16 * * 5` → next run `16:00-05:00` = 17:00 ET. (The system *user crontab*, by contrast, runs in UTC.)
- `hermes cron edit` has `--schedule` but **no `--model` flag** — model must be set directly in `~/.hermes/cron/jobs.json`.

## Other followups from the W21 review

- **Scale-out tier-hit logging discrepancy** — cron's W21 TL;DR surfaced: "Trade #38 peaked +67.86% yet shows 0 tier hits at all levels". **ADDRESSED 2026-06-02/04:** the trade health-check now audits this on every close, and a duplicate-tier scale-out race (commit `1ab457e`) + the put strike-range bug (`f2d2170`, KB I8) were found and fixed during validation week. See `data/strategy_kb.md`.
- **Split-entry cap aggregation (latent)** — the actual incident was solved by the per-(ticker, action) cooldown, but the underlying latent bug remains: `risk_manager.validate_signal` doesn't aggregate against open positions on the same OCC. Not triggered in W21 (trade #38 had closed before #39 opened), but worth fixing for held positions.

## Commits this work landed in

**ai-trading-assistant** (`main`, pushed to origin):
- `8ed3c17` — KB + weekly_review.py + Bug A/B fixes + gitignore
- `d702d5a` — scripts/fetch_macro_context.py (long-untracked, finally tracked)

**hermes-config** (`main`, pushed to origin):
- `e3d52ef` — `docs(via-hermes): ...` — Hermes-via-Discord additions (Why-Didn't-We-Trade, Macro Context Feed)
- `d79fbf6` — weekly review fold-in to umbrella SKILL.md
- `3f5a9ea` — chore: removed dead yaml
