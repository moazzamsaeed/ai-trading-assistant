# TradeMaster Self-Learning Loop — Design

**Status:** Phase 0–1 built (2026-06-28); Phase 2+ proposed.
**Owner:** human-gated throughout. Nothing in here auto-changes live trading without a merge.

## 1. Why this exists

Hermes today is a **retrospective analyst**: `weekly_review.py` and `hypothesis_review.py`
read closed trades, ask Sonnet 4.6 to find patterns / grade hypotheses, and post proposed
KB edits to Discord. The human hand-applies them.

The loop is **open**. Two breaks:

1. **Knowledge never reaches live decisions.** The deterministic engine (`agents/directional/signal_engine.decide`)
   reads `strategy.yaml` + `.env`. It does **not** read `data/strategy_kb.md`. Everything the
   review learns only changes behavior if a human writes code. The KB is write-only w.r.t. trading.
2. **Evaluation is too slow and too noisy.** Hypotheses H1–H5 accrue ~1 live trade/day. With
   n in the dozens and **negative expectancy at a decent win rate** (SPY: 44.7% win, −$42/trade,
   n=47), naive auto-tuning would fit noise and lose money faster.

So "self-learning" here is **not** "let the bot tune itself." Given there is *no demonstrated edge*
(`docs/STRATEGY_REVIEW_2026-06-18.md`, GO-LIVE HOLD), the loop's job is to **accelerate the human's
find-an-edge cycle and remove toil**, automating only what is provably safe.

## 2. Design principles (non-negotiable)

- **Win rate ≠ edge.** Every claim is judged on *expectancy*, not hit rate. (Wired into the
  hypothesis engine 2026-06-28: H3/H5 now disprove on negative/inferior expectancy.)
- **Enforce in code, not prompt.** The KB's own lesson: "Prompt instructions ≠ enforcement."
  Learned knowledge becomes **config/guardrails the deterministic engine reads**, never free-text
  rules injected into an LLM prompt and hoped-for.
- **Out-of-sample before live.** No parameter reaches live trading without passing a cost-robust,
  out-of-sample backtest gate (reuse the DSR / cost-sensitivity method from the condor/strangle work).
- **Small-n humility.** Keep the existing `n<5 ⇒ no pattern` rule; criteria carry explicit n-floors
  (H3 n≥40, H4 n≥30, H5 n≥10).
- **Human merge gate for anything touching live behavior.** Automate analysis and *proposals*;
  never auto-merge a strategy change.
- **Provenance + reversibility.** Every learned change is tagged with the evidence (backtest id, n,
  date) that justified it, so it is auditable in `git log` and revertible.

## 3. The closed loop (target architecture)

```
        ┌───────────────────────────────────────────────────────────────┐
        │ LIVE TRADING — signal_engine.decide (deterministic)            │
        │   reads: strategy.yaml + .env + (NEW) data/learned_params.yaml │
        └───────────────┬───────────────────────────────────────────────┘
                        │ emits RICH trade records (Phase 1 features)
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ OBSERVE — trades.db  (+ VIX1D, regime, hour, realized slippage)│
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ ANALYZE (Hermes / Sonnet 4.6)                                  │
        │   weekly_review · hypothesis_review · event post-mortems       │
        │   → hypotheses graded vs criteria + expectancy                 │
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ VALIDATE — auto-backtest the proposed change                   │
        │   out-of-sample + cost-robust (DSR gate). Fail ⇒ drop + log why│
        └───────────────┬───────────────────────────────────────────────┘
              pass gate  │
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ PROPOSE — auto-draft a diff (KB + learned_params.yaml)         │
        │   git branch / PR, with evidence provenance attached          │
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ HUMAN GATE — review & merge (champion vs challenger)           │
        └───────────────┬───────────────────────────────────────────────┘
                        ▼
        ┌───────────────────────────────────────────────────────────────┐
        │ SHADOW/PAPER the challenger → promote only if it beats the     │
        │   champion live; else revert (provenance makes this one diff)  │
        └───────────────┬───────────────────────────────────────────────┘
                        └────────► back to LIVE  (loop closed)

        DRIFT MONITOR (continuous): live expectancy vs backtested expectation.
          Divergence ⇒ re-open ANALYZE / flag the responsible learned_param.
```

The **feedback mechanism is `data/learned_params.yaml`** — a version-controlled file the engine reads
at decision time, populated *only* by human-merged, backtest-validated changes. This is the one safe
way to close the loop in their architecture: deterministic engine + config, not prompt text.

## 4. Tiers of autonomy (what is safe to automate, and when)

| Tier | What | Autonomy | Status |
|------|------|----------|--------|
| 0 Observe | weekly/hypothesis review, post-mortems | LLM analyses, human reads | **built** (reviews; hypothesis engine 2026-06-28) |
| 1 Capture | rich features per trade (regime, vol, hour, entry haircut) | automatic logging | **built 2026-06-28** |
| 2 Validate | auto-backtest any proposed change, out-of-sample + cost-robust | automatic gate, surfaces only passers | **proposed (Phase 2)** |
| 3 Propose | auto-draft KB + `learned_params.yaml` diff as a branch/PR | automatic draft, **human merges** | **proposed (Phase 3)** |
| 3.5 Feedback | engine reads `learned_params.yaml` | reads merged config only | **proposed (Phase 3)** |
| 4 Bookkeeping | auto-apply *pure-bookkeeping* KB edits (update evidence n, mark hypothesis dead once n+backtest both cross threshold) | auto-commit, never strategy params | **future, optional** |

**Strategy-parameter changes never go above Tier 3** (human merge) until there is a demonstrated edge
*and* large n. Only bookkeeping is a candidate for full autonomy.

## 5. Phased roadmap

### Phase 0 — Observe *(built)*
- `scripts/weekly_review.py`, `scripts/hypothesis_review.py` (H1–H5, expectancy-aware).
- Friday Hermes cron posts both TL;DRs to #research.

### Phase 1 — Capture the features the loop needs *(built 2026-06-28)*
The open questions ("does win rate vary by hour / vol regime?") were *unanswerable* because the
features weren't logged. **Pure logging — zero trade-behavior change; all 591 tests pass.** Added to
`agents/directional/executor.py::_persist_entry` (`extra` JSON), with `spy_regime`/`vol_regime`/`vix`
surfaced into `decision.analysis` via `intraday.py::_build_analysis`:
- `entry_hour_et`, `entry_et` — time-of-day.
- `spy_regime`, `vol_regime`, `entry_vix` — regime/vol context (values already computed; no new I/O).
- `entry_quote_mid`, `entry_quote_ask`, `entry_fill_haircut_per_share` — entry fill-cost drag
  (filled − mid), computed from the quote already in hand at fill time.

The hypothesis engine's DB probe now reports `by_hour_et` (derived from `opened_at` → works on all
history) and `by_vol_regime` (fills in as new trades log it). First retroactive read already shows an
hour effect (10:00 ET expectancy +$214/trade n=11 vs negative in the 9/12/13 ET buckets).

**Two deliberate scope limits (zero-risk discipline):**
- **VIX1D vs VIX:** the directional path has the standard `vix` already; deriving true VIX1D needs a
  0DTE-chain fetch (`condor_engine.vix1d_from_chain`) = new I/O in the trade path, so it was *not*
  added. Wire it in later if the vol signal proves useful.
- **Exit fill-haircut deferred:** entry-side haircut is captured (clean reference at fill). Exit-side
  needs a reference price plumbed through three exit paths (30s tick / 5-min monitor / force-close) —
  more surface area, so it's a follow-up, not in this pass.

### Phase 2 — Validate automatically
A `scripts/challenger_backtest.py` that takes a proposed parameter change (e.g. "ADX floor 25→30",
"drop MEDIUM conviction", "tighten tier-1 to +12%") and runs the existing backtests **out-of-sample
and across a cost grid**, returning a pass/fail against a DSR-style gate. The hypothesis engine and
post-mortems call this before proposing — only changes that survive the gate get surfaced.

### Phase 3 — Propose as diffs + close the loop
- `data/learned_params.yaml` (new): engine-read overrides with provenance headers (`# from <backtest>,
  n=…, merged <date>`). `signal_engine.decide` reads it (fail-open to `strategy.yaml` defaults).
- The reviews/engine draft an actual git branch with the KB edit **and** the `learned_params.yaml`
  change, body = the validating evidence. Human reviews & merges. (Extends the existing autocommit-hook
  infra; reuses the conservative "human applies" gate, just removes manual transcription.)

### Phase 4 — Shadow + drift monitor
- Champion/challenger: a merged change runs in **paper/shadow** first; promote to live only if it beats
  the champion's live expectancy over a pre-set window; else auto-revert (one diff, thanks to provenance).
- Drift monitor (daily cron): compares rolling live expectancy to the backtested expectation that
  justified each live param; flags divergence to #research.

## 6. Cadence / triggers

- **Per-trade:** log rich features (Phase 1).
- **Daily EOD:** cheap digest + **event-triggered post-mortem** on any trade with |PnL| over a
  threshold or matching an incident class (same-OCC re-entry, theta bleed like #79). Catches code bugs
  in hours, not Friday-to-Friday (the W21 review found 2 real bugs — faster cadence finds them sooner).
- **Weekly (Fri cron):** weekly_review + hypothesis_review (DB-only). *(built)*
- **On-demand / monthly:** deep `hypothesis_review --backtests` + challenger backtests.
- **Continuous:** drift monitor (Phase 4).

## 7. Explicit non-goals (do NOT build these)

- ❌ Auto-tuning live parameters directly from live results (overfits noise at this n).
- ❌ Injecting KB free-text "rules" into the entry LLM prompt as the feedback path (LLMs follow textual
   rules poorly; their own KB says enforce in code). Soft *context* (e.g. a regime note) is fine where
   it cannot, by construction, bypass a code guardrail.
- ❌ An ML model that predicts trades. No edge + tiny labeled data ⇒ it would learn noise. Earn the
   right to this later with features (Phase 1) and a demonstrated baseline edge first.
- ❌ Removing the human merge gate on strategy changes before there is a demonstrated edge.

## 8. First concrete step

Phase 1 (feature capture) is the unlock for everything after it and carries zero live risk. The
expectancy criterion (Phase 0 polish) is already merged into the hypothesis engine and KB as of
2026-06-28.
