# Architecture

## High-Level Diagram

```
                          ┌───────────────────────────────┐
                          │   TRADEROUTER (Opus 4.7)      │
                          │   Orchestrator                │
                          │   Risk manager (cash-only)    │
                          │   Router → sub-agents         │
                          │   Scheduler                   │
                          └──┬──────────┬──────────┬─────┬┘
                            │        │        │     │
            ┌───────────────┘        │        │     └─────────────┐
            │                        │        │                   │
   ┌────────▼────────┐  ┌────────────▼─┐  ┌──▼──────────┐  ┌────▼────────┐
   │  Pre-market     │  │ Quant scans  │  │ Options     │  │  Crypto     │
   │  Research       │  │ (intraday)   │  │ strategist  │  │  regime     │
   │  Gemini 3.1 Pro │  │ DSV4-Flash   │  │ DSV4-Pro    │  │  DSV4-Pro   │
   └────────┬────────┘  └─────┬────────┘  └─────┬───────┘  └─────┬───────┘
            │                 │                 │                 │
            └────────┬────────┴────────┬────────┴────────┬────────┘
                     │                 │                 │
              ┌──────▼──────────────────▼─────────────────▼──────┐
              │     Alpaca (via `alpaca-py` SDK — see D-009)     │
              │   data + news + execution + portfolio + crypto   │
              └──────────────────────────────────────────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
              ┌──────▼───────┐             ┌───────▼──────┐
              │ Discord Bot  │             │  Dashboard   │
              │ alerts/cmds  │             │  (Next.js)   │
              └──────────────┘             └──────────────┘
```

## Data Flow

1. **Scheduler** (in TradeRouter) fires events: `pre_market_briefing` (8am ET), `intraday_scan` (every 10-15 min during RTH), `eod_summary` (4:15pm ET), and crypto-only ticks 24/7.
2. TradeRouter receives the event and dispatches the appropriate sub-agent via `router.route_to_model(task_type)`.
3. Sub-agent calls Alpaca (via `alpaca-py`) for data → reasons about it → returns a structured signal (Pydantic model).
4. TradeRouter runs the signal through `risk_manager.validate(signal)`. Rejects on:
   - Margin/leverage detected
   - Daily loss limit hit
   - Position size > MAX_POSITION_SIZE_USD
   - Concurrent positions > MAX_CONCURRENT_POSITIONS
   - Cash insufficient
5. If approved, TradeRouter either:
   - Executes via `alpaca-py` (auto-mode)
   - Posts to Discord `#alerts` and waits for `/approve` (approval-mode)
   - Posts as alert only (alert-only mode for equities)
6. Trade outcome logged to SQLite. Dashboard reads from SQLite.

## Model Routing Table

| Task type | Model | Provider |
|---|---|---|
| `orchestrate` | Claude Opus 4.7 | Anthropic |
| `pre_market_research` | Gemini 3.1 Pro | Google AI Studio |
| `intraday_scan` | DeepSeek V4-Flash | DeepSeek |
| `format_alert` | DeepSeek V4-Flash | DeepSeek |
| `options_strategy` | DeepSeek V4-Pro | DeepSeek |
| `crypto_regime` | DeepSeek V4-Pro | DeepSeek |
| `execution_decision` | Claude Opus 4.7 | Anthropic |

## Risk Manager (Hard-Coded, Non-LLM)

The risk manager is pure Python — no LLM in the loop. It runs after every agent signal and before every order placement.

Responsibilities:
- Verify `ACCOUNT_TYPE=cash` (refuses to start otherwise)
- Verify cash availability ≥ order notional
- Reject naked options (must be defined-risk structure)
- Track daily P&L; halt trading if `DAILY_LOSS_LIMIT_USD` breached
- Track open positions count
- Provide `/kill` command handler that flattens all positions immediately

## Persistence

- **SQLite** at `data/traderouter.db`
  - `trades` — executed trades with entry/exit/P&L
  - `signals` — every agent signal for audit and retro-analysis
  - `agent_runs` — every LLM call: model, tokens, cost, duration
  - `risk_events` — every rejection/halt with reason

## External Dependencies

- **Alpaca** — official `alpaca-py` SDK (see D-009 for why we picked SDK over MCP)
- **Anthropic API** — TradeRouter orchestration
- **DeepSeek API** — sub-agents
- **Google AI Studio API** — pre-market research
- **Discord Developer API** — bot
