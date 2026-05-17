# Decisions Log

This file captures the *why* behind architectural and tooling choices, so a V2 build does not relitigate settled questions. Append new decisions; do not edit historical ones.

---

## D-001 — Cash account only, no margin, ever

**Decision:** Trade exclusively on a cash account. No margin, no leverage, no naked options, no perps.

**Why:** User-imposed hard constraint for survivability. Cash-only caps downside to deployed capital and removes margin-call risk entirely. Aligns with "robust and successful over many years" rather than maximum-return-per-trade.

**Implication:** Risk manager refuses to start if `ACCOUNT_TYPE != cash`. Every order verified against available cash before submission. Options structures must be defined-risk (iron condors, vertical spreads). Crypto is spot only.

---

## D-002 — TradeMaster (Opus 4.7) orchestrates, sub-agents run cheaper models

**Decision:** Claude Opus 4.7 only for the TradeMaster orchestrator role. All sub-agent work (research, scans, strategy, execution) runs on cheaper models.

**Why:** Opus is the strongest at multi-step orchestration and tool routing. But running it for every intraday scan would burn budget. DeepSeek V4 is "near state-of-the-art" at 1/6 the cost — sufficient for sub-agent work where TradeMaster has the final say anyway. Pre-market research is the one task where reasoning matters more than orchestration, so it gets Gemini 3.1 Pro (highest GPQA score).

**Alternatives considered:**
- LiteLLM proxy for routing — rejected. TradeMaster calls each provider's SDK directly via a thin `router.route_to_model()`. One less moving part.
- All-Claude stack — rejected. Cost would be 3-4× higher with no measurable quality gain on routine scans.
- All-DeepSeek stack — rejected. Orchestration quality matters most for risk decisions, and Opus has a real edge there.

---

## D-003 — Alpaca only for data and execution ⚠️ SUPERSEDED by D-009

**Original decision:** Use Alpaca's official MCP server for all data, news, and execution.

**Superseded by D-009 (2025-12):** TradeMaster uses the `alpaca-py` SDK directly, not the MCP server. See D-009 for the full rationale. The MCP server is not part of the trading daemon — it can be used for ad-hoc Claude Desktop conversations with the account but is independent of TradeMaster.

**Still valid:** Alpaca-only for data. No Polygon.io, no Bloomberg, no premium news feeds.

---

## D-004 — Discord, not Telegram or WhatsApp

**Decision:** Discord for all alerts and command interface.

**Why:** Discord has clean bot API, great message formatting (code blocks, embeds, buttons for approve/reject), and supports private servers with channels. Telegram is a fine alternative but Discord's formatting suits trading alerts better. WhatsApp Business API is painful and adds compliance overhead.

---

## D-005 — Paper trading ≥30 days before live capital

**Decision:** No live deployment until 30+ days of paper trading on each strategy with documented edge.

**Why:** LLM-driven trading systems are new territory. Paper trading exposes operational bugs (race conditions, MCP failures, agent logic gaps) without financial loss. 30 days covers a reasonable mix of market regimes.

**Alternatives considered:**
- Skip paper, go live with tiny size — rejected. Operational bugs would still cost real money even with small size, and erode confidence in the system.

---

## D-006 — SQLite, not Postgres or cloud DB

**Decision:** SQLite at `data/trademaster.db` for all persistence.

**Why:** Single user, single machine, single process. Postgres adds operational overhead (backups, server management) for zero benefit at this scale. SQLite is fast, transactional, and trivially backed up (it's a file).

**When to revisit:** If we add multi-instance deployments or need concurrent writers across machines. Not before.

---

## D-007 — Risk manager is pure Python, not LLM

**Decision:** All hard limits (cash check, daily loss, position size, concurrent positions, naked-options rejection) are enforced by deterministic Python code, not by an LLM.

**Why:** LLMs make mistakes. Risk limits cannot. A hard-coded check is auditable, testable, and impossible to "talk out of." The LLM proposes; the risk manager disposes.

---

## D-008 — Python 3.14 (Ubuntu 26.04 default), but project pins ≥3.11

**Decision:** Project requires Python ≥3.11. NUC runs 3.14 (Ubuntu default). `uv` manages the interpreter.

**Why:** 3.11 is widely supported and includes faster startup. Pinning ≥3.11 (not exactly 3.14) keeps V2 portability if future hardware ships with a slightly older Python.

---

## D-009 — `alpaca-py` SDK for TradeMaster, not the Alpaca MCP server

**Decision:** TradeMaster calls Alpaca directly via the official `alpaca-py` Python SDK. The Alpaca MCP server is not used inside the TradeMaster process.

**Why:** MCP servers are designed for MCP *clients* (Claude Desktop and similar) and run as separate subprocesses, talking over stdio. Inside a long-running Python orchestrator, importing `alpaca-py` directly is simpler, faster, and removes a process boundary plus a serialization layer. The MCP server remains a useful tool for ad-hoc Claude-Desktop conversations with the account, but that workflow is independent of TradeMaster and does not need to live in the same codebase.

**Implication:** `integrations/alpaca_client.py` wraps `alpaca-py` for all data and execution. `ARCHITECTURE.md` reflects this — the diagram shows Alpaca via `alpaca-py` SDK, not MCP.

**When to revisit:** If TradeMaster itself needs to be exposed as a tool host to MCP clients, or if Alpaca ships a substantially richer surface in the MCP server than in the SDK.

---

## D-010 — Nous Hermes Agent as ecosystem-level "mission control" (deferred)

**Decision:** This trading project (`trademaster`) is one component of a larger personal-projects ecosystem. The ecosystem-level orchestrator — the always-on daemon that lets the user interact remotely via Discord, modify code while away from the NUC, and coordinate across multiple unrelated projects — will be [Nous Research's Hermes Agent](https://hermes-agent.nousresearch.com/). It will be installed as a separate process on the NUC during Phase 4 (deployment).

**Why:** Hermes Agent is purpose-built for what the user wants at the ecosystem level: a self-hosted, always-on daemon with cross-session memory, multi-channel messaging (Discord native), and skill-learning. Building this layer ourselves on top of the trading project would tightly couple two different concerns. Keeping them separate lets `trademaster` stay narrowly focused on trading, and lets new personal projects slot into the same Hermes Agent control plane without modification.

**Alternatives considered:**
- **Build remote-control into TradeMaster** — rejected. Couples dev/admin concerns to the trading core, expanding the safety-critical surface unnecessarily.
- **Claude Code remote control (claude.ai/code)** — rejected as the primary surface. Strong UX for code edits, but the channel is the web app, not Discord. Stays available as a backup admin path.
- **OpenClaw** — rejected. Multi-channel routing is valuable but Discord-only is fine; OpenClaw's strength (many messaging channels) is not load-bearing here.

**Implication:** TradeMaster's Discord bot handles trading-specific commands only (`/approve`, `/kill`, `/status`, `/positions`, `/cash`). Anything outside that scope — code edits, log reads, agent-prompt tweaks, cross-project queries — is the responsibility of the Hermes Agent layer, which we will add when the trading project is operationally stable.

**When to revisit / implement:** After Phase 4 (dashboard + 30-day paper run) is stable, OR when a second personal project needs the same kind of remote-control surface. Should not introduce changes that would force a rewrite of `trademaster/`.

---

## D-011 — Internal package renamed `hermes/` → `trademaster/`

**Decision:** The orchestrator package is named `trademaster/` (Python module: `trademaster`), not `hermes/`. The component is referred to as "TradeMaster" in prose.

**Why:** The original name `hermes/` collided with [Nous Research's Hermes Agent](https://hermes-agent.nousresearch.com/) — which D-010 commits to using at the ecosystem layer. Keeping both names in the same conversation would create durable confusion. "TradeMaster" makes the component's role explicit: it routes trades (and the agent calls that produce them).

**Implication:** All imports use `trademaster.*`. The SQLite DB is at `data/trademaster.db`. ASCII diagrams and prose refer to "TradeMaster" or "TRADEMASTER" where the orchestrator role is meant.

**When to revisit:** Not expected. The rename is mechanical; it does not bind any future architectural choice.

---

## D-012 — Renamed `traderouter/` → `trademaster/`

**Decision:** The orchestrator package is renamed once more, to `trademaster/`. Module name `trademaster`; prose name "TradeMaster".

**Why:** User preference. "TradeRouter" emphasized routing (one slice of what the component does); "TradeMaster" better captures the orchestrator role across research, strategy, execution, and risk management. Same character count as TRADEROUTER (11) so ASCII diagrams remain aligned.

**Implication:** Supersedes D-011's chosen name. All `traderouter.*` imports become `trademaster.*`. SQLite DB now at `data/trademaster.db`. D-002 / D-009 / D-010 / D-011 rewritten in-line to use the new name (mechanical sed) — historical record of the prior names lives in commit `e18de0d` (hermes → traderouter) and this commit (traderouter → trademaster).

**When to revisit:** Not expected.

---

## D-013 — Dual-channel output: `#signals` for manual, `#trades` for automated

**Decision:** Every strategy emits two parallel outputs:
- `#signals` — broker-ready manual-trading instructions for the user
  (specific strikes, expiry date, call/put, side, target prices, exit
  rules in $/contract). The user trades these themselves through their
  own broker if they choose to.
- `#trades` — automated bot execution telemetry against the Alpaca
  paper (or live) account. What the bot did, what it filled at, P&L.

Same strategy logic produces both. Errors route to a separate `#logs`
channel — never to `#signals` or `#trades` where they would create
alert fatigue.

**Why:** The user trades manually as well as letting the bot trade.
Both following the same strategy lets us compare manual-vs-automated
P&L at the end of the paper-trade run. If they diverge, we know whether
it's a strategy issue (both lose) or an execution issue (only one loses).

**Implication:** The renamed channel set in `.env.example` is:
`DISCORD_CHANNEL_SIGNALS`, `DISCORD_CHANNEL_TRADES`,
`DISCORD_CHANNEL_RESEARCH`, `DISCORD_CHANNEL_LOGS`, `DISCORD_CHANNEL_COMMANDS`.
The legacy `DISCORD_CHANNEL_ALERTS` is removed. Strategist and exit-monitor
return shapes changed to `(signal, signals_text, trade_text)` so the
scheduler can route each to the right channel.

**When to revisit:** If we add a second messaging surface (Telegram,
email) later, the named-poster pattern in scheduler should accept any
implementation — no architectural change needed.

---

## D-014 — Live-mode trades require Discord `/approve`; paper auto-executes

**Decision:** When `TRADING_MODE=paper`, the executor submits to Alpaca
automatically after risk-manager approval. When `TRADING_MODE=live`, the
executor instead persists a `pending_orders` row and posts an "awaiting
approval" message to `#trades`. The user runs `/approve <id>` in Discord
to submit, `/reject <id>` to discard, or `/pending` to list waiting orders.

**Why:** Live trading needs a human gate. The strategist + risk manager
are good enough for paper validation; for real capital, requiring an
explicit decision adds a layer that catches edge cases the rule-based
checks miss (e.g., FOMC day, a news event the agent didn't see). The
approval window is short — pending orders expire automatically after
15 minutes so a stale market state can't be approved hours later.

**Implication:**
- New `pending_orders` table with status `pending|approved|rejected|expired`,
  storing the plan as JSON so `/approve` can reconstruct the submission.
- Executor has two entry points: `execute_iron_condor` (paper auto-submit
  OR live pending-create) and `execute_approved_pending(id)` (post-approval
  submit). Both share `_submit_and_persist` so paper-fill and live-fill
  produce identical `trades` rows.
- New owner-only slash commands: `/approve N`, `/reject N`, `/pending`.

**When to revisit:** If the 30-day paper run gives us strong confidence
in the strategist, we might revisit the auto-execute gate for live mode —
but always keep `/kill` and `/pause`. The expiry window (15 min) may
also need tuning based on real-world response latency.

---

## D-015 — Backtest harness uses synthetic data (BS + GBM), not historical Alpaca options

**Decision:** Phase 2.4's backtest harness generates intraday SPY price
paths via Geometric Brownian Motion and synthesizes option chains
from Black-Scholes (using `math.erf`, no scipy dependency). Real
historical 0DTE chains from Alpaca will be added in a later phase as
a drop-in `price_paths` source — the rest of the simulator code
(`build_iron_condor`, exit policy, P&L math) stays identical.

**Why:** Three reasons:
1. **Speed of iteration.** Synthetic data runs locally with no rate
   limits — a full year sims in seconds. We can sweep parameters
   (target delta, wing width, IV regime) without API budget concerns.
2. **Strategy-code coverage.** The backtest reuses `build_iron_condor`
   from `strategies/`, so refactors to leg selection or credit math
   are caught immediately. The backtest IS a regression suite.
3. **Realistic-enough first cut.** GBM + BS doesn't model pin risk,
   vol skew, or intraday IV shocks, but it's adequate for stress-
   testing the exit logic (50% PT / 2× stop / 15:50 force-close)
   and getting an order-of-magnitude win-rate estimate.

**Implication:**
- `backtests/` package is self-contained: synthetic_options.py
  (BS math + chain), price_paths.py (GBM), simulator.py (one-day),
  runner.py (multi-day + stats), cli.py (entry point).
- Per-file ruff override allows quant-convention single-letter
  variable names (S, K, T, sigma) in BS math.
- The backtest's win-rate / expectancy numbers should NOT be used
  for live position sizing — only the 30-day paper run informs that
  (D-005).

**When to revisit:** Once Alpaca historical-options access is
available, add a `from_alpaca_history(date_range)` price-path source
that pulls real chain snapshots. Compare backtest results between
synthetic and real to calibrate the synthetic IV assumption.

---

## D-016 — Pre-market research model: gemini-2.5-pro (stable), not 3.1-pro-preview

**Decision:** The router's `PRE_MARKET_RESEARCH` task type uses
`gemini-2.5-pro` (stable) instead of `gemini-3.1-pro-preview`.

**Why:** First integration test of the pre-market briefing failed
three times on Gemini 3.1 Pro Preview with consistent 503
"experiencing high demand" responses — even on simple 10-token
prompts. The model is in preview status with severe load-balancing
constraints. Gemini 2.5 Pro (stable) responds reliably to the same
queries.

For our pre-market news synthesis task, the benchmark difference
between 2.5 Pro and 3.1 Pro Preview is irrelevant — the briefing is
~500 words from ~300 tokens of news, well within 2.5 Pro's
capabilities. Reliability > marginal GPQA score.

**Alternatives considered:**
- Stay on 3.1 Pro Preview and live with intermittent failures —
  rejected. Briefings would silently skip on busy days; risk-event
  log fills up with 503s.
- Fallback chain (try 3.1 → fall back to 2.5) — rejected as
  unnecessary complexity. The "preview model is unreliable" failure
  mode is structural, not transient; we'd hit the fallback nearly
  every call.

**Implication:** Updates D-002's "highest GPQA score" reasoning for
this specific task type. Other agents (Anthropic Opus 4.7, DeepSeek
V4-Pro/Flash) remain unchanged — they're all on stable models.
Cost-per-call drops from ~$2/$12 to $1.25/$10 per M tokens (lower
budget impact too).

**When to revisit:** Once Gemini 3.x emerges from preview status
with stable production tier, swap back if benchmarks justify the
cost delta for this task.

---

## D-017 — Compute option greeks in-process via Black-Scholes inversion

**Decision:** The live iron-condor strategist fills in missing greeks
(delta, IV) by solving Black-Scholes inverse on each option's market
mid, using a shared `trademaster/options_math.py` module. We do NOT
require the paid OPRA data feed for greeks.

**Why:** First integration test against the real Alpaca options chain
showed that the default (and indicative) data feed returns prices but
`greeks=None` and `implied_volatility=None` for every contract. OPRA
(real-time exchange feed) requires the signed OPRA Subscriber
Agreement, which costs an additional monthly fee. We don't need that.

Black-Scholes IV inversion is well-conditioned for liquid SPY 0DTE
strikes (mid prices > $0.01). The strategist gets per-strike skew
naturally because each leg's IV is solved from its own market price,
not assumed flat. Strikes whose mid pins at the $0.01 minimum tick
(no real market) stay with `delta=None` and are excluded from leg
selection — the right behavior for untradeable strikes.

**Implication:**
- `trademaster/options_math.py` is the canonical home for BS math.
  Backtest (`backtests/synthetic_options.py`) imports from there.
- `_enrich_chain_with_bs_greeks` runs inside the strategist after
  the chain fetch. Per-leg IV is computed via 60-iteration bisection
  (no scipy dep).
- Signal `extra` records the BS-derived IV so the agent_runs audit
  shows the IV regime the strategist saw.

**When to revisit:** If we upgrade to an Alpaca tier that provides
greeks server-side, the enricher becomes a no-op (already handled —
quotes with non-None delta pass through unchanged). Until then this
is the path.
