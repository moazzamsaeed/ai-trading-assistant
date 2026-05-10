# Decisions Log

This file captures the *why* behind architectural and tooling choices, so a V2 build does not relitigate settled questions. Append new decisions; do not edit historical ones.

---

## D-001 — Cash account only, no margin, ever

**Decision:** Trade exclusively on a cash account. No margin, no leverage, no naked options, no perps.

**Why:** User-imposed hard constraint for survivability. Cash-only caps downside to deployed capital and removes margin-call risk entirely. Aligns with "robust and successful over many years" rather than maximum-return-per-trade.

**Implication:** Risk manager refuses to start if `ACCOUNT_TYPE != cash`. Every order verified against available cash before submission. Options structures must be defined-risk (iron condors, vertical spreads). Crypto is spot only.

---

## D-002 — Hermes (Opus 4.7) orchestrates, sub-agents run cheaper models

**Decision:** Claude Opus 4.7 only for the Hermes orchestrator role. All sub-agent work (research, scans, strategy, execution) runs on cheaper models.

**Why:** Opus is the strongest at multi-step orchestration and tool routing. But running it for every intraday scan would burn budget. DeepSeek V4 is "near state-of-the-art" at 1/6 the cost — sufficient for sub-agent work where Hermes has the final say anyway. Pre-market research is the one task where reasoning matters more than orchestration, so it gets Gemini 3.1 Pro (highest GPQA score).

**Alternatives considered:**
- LiteLLM proxy for routing — rejected. Hermes calls each provider's SDK directly via a thin `router.route_to_model()`. One less moving part.
- All-Claude stack — rejected. Cost would be 3-4× higher with no measurable quality gain on routine scans.
- All-DeepSeek stack — rejected. Orchestration quality matters most for risk decisions, and Opus has a real edge there.

---

## D-003 — Alpaca only for data and execution

**Decision:** Use Alpaca's official MCP server for all data, news, and execution. No Polygon.io, no Bloomberg, no premium news feeds.

**Why:** Alpaca's Algo Trader Plus tier covers real-time stock + options + news + crypto on one bill. Their official MCP server eliminates the need to build custom data fetchers. Fewer providers = fewer failure modes.

**Alternatives considered:**
- Polygon.io for equity data — rejected. Alpaca covers it.
- Benzinga news — rejected. Alpaca's news is sufficient until proven otherwise.
- TradingView MCP — rejected. Community tool, browser-automation based, fragile. User can view charts manually on TradingView while agents send analysis to Discord.

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

**Decision:** SQLite at `data/hermes.db` for all persistence.

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
