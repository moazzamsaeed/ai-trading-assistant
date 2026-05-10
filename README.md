# AI Trading Assistant

Hermes-orchestrated multi-agent trading and alert system. Runs on a local NUC, executes paper-then-live trades on Alpaca, and pushes alerts to Discord.

## Critical Constraints

> **Cash account only. No margin. No leverage. Ever.**
>
> Hermes enforces this in code — every order is rejected unless backed by available cash. Defined-risk options structures only (iron condors, spreads). No naked options. No borrowed capital.

## Hardware

- ASUS NUC Pro — 16GB RAM, 1TB SSD, Intel Core i5
- Ubuntu 26.04 LTS
- Python 3.14, Node.js 20, SQLite, tmux

## Architecture (one-line summary)

Hermes (Claude Opus 4.7) orchestrates specialized sub-agents (Gemini 3.1 Pro for pre-market research, DeepSeek V4-Pro for strategy decisions, DeepSeek V4-Flash for high-frequency scans), each calling the Alpaca MCP for data and execution, with results routed to a Discord bot for alerts and a Next.js dashboard for performance tracking.

See `docs/ARCHITECTURE.md` for the full diagram and data flow.

## Trading Scope

| Asset | Strategy | Execution |
|---|---|---|
| Options (SPY 0DTE) | Iron condors when IV rank > 50 | Auto on paper, approval-gated on live |
| Crypto (BTC/ETH/SOL) | Trend-follow 4H/1D + grid in low ATR | Auto on paper, approval-gated on live |
| Equities | VWAP reclaim + pre-market gap analysis | Alerts only — user trades manually |

## Status

Phase 0 — Foundation and scaffolding. See `docs/RUNBOOK.md` for current build phase and how to start the system.

## Setup

```bash
git clone https://github.com/moazzamsaeed/ai-trading-assistant.git
cd ai-trading-assistant
./scripts/setup.sh
cp .env.example .env  # fill in your keys
```

See `docs/RUNBOOK.md` for full operational instructions.

## Repo Layout

```
hermes/          # Orchestrator + risk manager + router + scheduler
agents/          # Sub-agents (research, options, crypto, equity_alerts)
strategies/      # Pure strategy logic (independently testable)
integrations/    # Alpaca client, Discord bot, LLM clients
dashboard/       # Next.js dashboard
backtests/       # Backtest scripts and stored results
docs/            # ARCHITECTURE, STRATEGIES, RUNBOOK, DECISIONS
data/            # SQLite DB, logs (gitignored)
scripts/         # setup.sh, deploy_nuc.sh
tests/           # pytest suite
```
