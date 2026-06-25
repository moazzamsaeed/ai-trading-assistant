---
name: entry-model-replay-study-deepseek-vs-sonnet-4-6-vs-opus-4-8
description: "2026-06-15 — replay harness comparing 3 LLMs on past entry decisions, to decide whether to upgrade the directional ENTRY model off DeepSeek before going live. Cost discussion pending."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9687e976-81cd-4827-b620-0a56231a0fd5
---

User asked (2026-06-15): why does cheap **DeepSeek v4-flash** pick directional ENTRIES while exits use Claude Sonnet 4.6? (routing in `trademaster/router.py` MODEL_MAP: INTRADAY_SCAN→deepseek-v4-flash, EXIT_DECISION→sonnet-4-6; rationale is decision **D-002** in `docs/DECISIONS.md` — cheap proposer + deterministic gates have final say). User wanted a **replay comparison BEFORE changing anything**, including **Opus 4.8**, then "we'll discuss cost".

**Harness:** `scripts/replay_model_comparison.py` (run with `.venv/bin/python -m ...`, NOT `uv run` — uv re-resolves and fails on the truthbrush/py3.14 extra). Feeds the SAME reconstructed entry prompt at each historical scan timestamp to all 3 models; model is the only variable. Lookahead-safe: historical bar/news fetchers set `end=T`; position context reconstructed as-of-T (open + earlier-today closes only). Writes/budget isolated to a throwaway temp DB; as-of-T position context reads the real DB. Output JSONL: `data/replays/full.jsonl` (full run 06-11+06-12, 10-min grid + real-entry points).

**Per-call cost (measured in pilot):** DeepSeek **$0.0008**, Sonnet **$0.019**, Opus **$0.032**. Pricing $/1M in/out: deepseek-flash 0.14/0.28, sonnet-4-6 3/15, opus-4-8 5/25. Added `claude-opus-4-8` ($5/$25) to `trademaster/llm/pricing.py`.

**PRELIMINARY signal (pilot, before position-ctx fix):** at the real losing-entry timestamps on 06-11, **DeepSeek HELD while Sonnet+Opus took BUY_CALL** — i.e. the "smarter" models looked MORE aggressive, not more selective. Opposite of the upgrade hypothesis. Full run (with as-of-T loss-streak context now working) pending analysis.

**Context for the decision:** entry quality in chop is the documented weak spot (the whole 06-08→06-12 loss-prevention package compensates for bad entries). 06-11 was 0W/4L −$3,347 whipsaw (CALL/PUT/CALL/PUT). Question: would a smarter entry model avoid those? See [[project_current_focus]], [[project_loss_prevention_package]]. NOT yet changed in production — this is evidence-gathering before the $5k live week.

**FULL-RUN RESULT (06-11+06-12, 78 pts, 234 calls, $3.91):** With as-of-T loss-streak context working, the smarter models are MORE selective + crucially DON'T whipsaw direction. BUY rate: DeepSeek 18%, Sonnet 8%, Opus 9%. **06-11 direction sequence: DeepSeek `CCPCPP` (3 flips), Sonnet `C` (0), Opus `CCC` (0)** — DeepSeek alternates call/put in chop, the exact −$3.3k whipsaw pattern; the Claudes hold one direction. At the 4 real losing entries: all 3 took the 1st (−$1490 CALL); on the −$1449 reversal PUT, **Sonnet HELD (avoided), Opus took it smaller, DeepSeek took full** → Sonnet would've saved ~$1,449 of the $3,347. Sonnet vs Opus 94% identical; Opus 2× cost and took the loss Sonnet avoided → Sonnet is the pick. Caveats: small N, 1st big loss unavoidable, hypothetical entry P&L not scored, live chop/ADX scheduler gates already block some whipsaw (replay is at the raw LLM-proposal layer).

**DECISION 2026-06-15 (user): switch directional ENTRY to Sonnet 4.6 "until I ask to flip back to DeepSeek."** IMPLEMENTED:
- New task type `DIRECTIONAL_ENTRY` in `trademaster/router.py` → `("anthropic","claude-sonnet-4-6")`, fallback → `deepseek-v4-flash`. INTRADAY_SCAN (news scan + market_analysis) UNCHANGED on DeepSeek.
- `agents/directional/intraday.py` route_to_model + SignalRow label → DIRECTIONAL_ENTRY.
- **TO FLIP BACK:** change the `DIRECTIONAL_ENTRY` line in MODEL_MAP back to `("deepseek","deepseek-v4-flash")` (one line) and restart daemon. (Reverting the whole change is also fine but unnecessary.)
- Exits unchanged (already Sonnet). Cost impact ≈ +$16/mo (directional-only) vs current ~$1/mo, well under $100 budget.
- Daemon: change is on disk; goes live at the normal **Tue 06-16 06:45 CDT** auto-start (I restarted briefly to verify then stopped it — market was closed, returned to normal overnight-off). 73 router/pricing/intraday tests pass.

**06-15 SESSION POST-MORTEM + 2nd fix (committed `11b0ab5`):** 06-15 SPY ground +$5 to the late-AM high; bot took **1 trade, +$6**, missed the whole grind. Root cause: **ADX was never shown to the entry LLM** (only used post-hoc for sizing in executor.py). With ADX=59 the model blindly applied prompt rules `volume_ratio<1.0→HOLD` + `RSI>75→overbought/put-bias` → held 55 BUY_CALL setups on a clean low-volume uptrend; the 1 trade fired late at the top and was (correctly) scratched by the volume_fading exit. NOT a model problem — DeepSeek WAS saying BUY; the deterministic prompt gates blocked it. **Fix (`agents/directional/intraday.py`):** (1) add `adx` to the ticker block; (2) STRONG-TREND OVERRIDE at **ADX≥25** (= existing `adx_full_above`) waives the volume>1.0 requirement + extends RSI band (calls→88); (3) RSI-overbought + STEP-4 volume-fade vetoes now fire only when ADX<25. Validated w/ live Sonnet: 06-15 (ADX 36-51) now BUY_CALL at 10:00/10:30/11:00 (were HOLD); **06-11 (ADX 17-22) carve-out does NOT fire → still HOLDs the −1449 reversal + 2 small losers, no regression.** ADX≥25 cleanly separates trend from chop. 180 tests pass.

**LIVE CONFIG for paper week (both load at Tue 06-16 06:45 CDT auto-start, daemon currently stopped — market closed):** (1) directional ENTRY model = **Sonnet 4.6** (`cd61f5b`), (2) **ADX-aware entry gates** (`11b0ab5`). Exits unchanged (Sonnet). Both committed+pushed. `scripts/replay_model_comparison.py` + `data/replays/*` are the evidence (replay script committed in `cd61f5b`; data/ gitignored). Working tree clean except pre-existing `M .gitignore`.
