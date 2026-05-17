# Architecture

## High-Level Diagram

```
                          ┌───────────────────────────────┐
                          │   TRADEMASTER (Opus 4.7)      │
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
   │  Gemini 2.5 Pro │  │ DSV4-Flash   │  │ DSV4-Pro    │  │  DSV4-Pro   │
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

1. **Scheduler** (in TradeMaster) fires events: `pre_market_briefing` (8am ET), `intraday_scan` (every 10-15 min during RTH), `eod_summary` (4:15pm ET), and crypto-only ticks 24/7.
2. TradeMaster receives the event and dispatches the appropriate sub-agent via `router.route_to_model(task_type)`.
3. Sub-agent calls Alpaca (via `alpaca-py`) for data → reasons about it → returns a structured signal (Pydantic model).
4. TradeMaster runs the signal through `risk_manager.validate(signal)`. Rejects on:
   - Margin/leverage detected
   - Daily loss limit hit
   - Position size > MAX_POSITION_SIZE_USD
   - Concurrent positions > MAX_CONCURRENT_POSITIONS
   - Cash insufficient
5. If approved, TradeMaster either:
   - Executes via `alpaca-py` (auto-mode)
   - Posts to Discord `#alerts` and waits for `/approve` (approval-mode)
   - Posts as alert only (alert-only mode for equities)
6. Trade outcome logged to SQLite. Dashboard reads from SQLite.

## Model Routing Table

| Task type | API model ID | Provider |
|---|---|---|
| `orchestrate` | `claude-opus-4-7` | Anthropic |
| `pre_market_research` | `gemini-2.5-pro` | Google AI Studio |
| `intraday_scan` | `deepseek-v4-flash` | DeepSeek |
| `format_alert` | `deepseek-v4-flash` | DeepSeek |
| `options_strategy` | `deepseek-v4-pro` | DeepSeek |
| `crypto_regime` | `deepseek-v4-pro` | DeepSeek |
| `execution_decision` | `claude-opus-4-7` | Anthropic |
| `exit_decision` | `claude-sonnet-4-6` | Anthropic |

Fallbacks (automatic on provider error):
- `intraday_scan` → `claude-haiku-4-5-20251001`
- `options_strategy` → `claude-haiku-4-5-20251001`
- `pre_market_research` → `claude-sonnet-4-6`

Source of truth: `trademaster/router.py` `MODEL_MAP`.

## Risk Manager (Hard-Coded, Non-LLM)

The risk manager is pure Python — no LLM in the loop. It runs after every agent signal and before every order placement.

Responsibilities:
- Verify `ACCOUNT_TYPE=cash` (refuses to start otherwise)
- Verify cash availability ≥ order notional
- Reject naked options (must be defined-risk structure)
- Track daily P&L; halt trading if daily loss limit (`DAILY_LOSS_LIMIT_PCT`) breached
- Track weekly P&L; halt until Monday if weekly loss limit (`WEEKLY_LOSS_LIMIT_PCT`) breached
- Max trades per day gate (`MAX_TRADES_PER_DAY`)
- Event blackout calendar (FOMC/CPI/NFP — blocks new entries on high-impact macro days)
- Bid/ask spread filter (rejects illiquid options where spread > `MAX_BID_ASK_SPREAD_PCT` of mid)
- Startup reconciliation: compares DB open trades vs Alpaca live positions, repairs mismatches
- Track open positions count
- Provide `/kill` command handler that flattens all positions immediately

## Persistence

- **SQLite** at `data/trademaster.db`
  - `trades` — executed trades with entry/exit/P&L
  - `signals` — every agent signal for audit and retro-analysis
  - `agent_runs` — every LLM call: model, tokens, cost, duration
  - `risk_events` — every rejection/halt with reason

## External Dependencies

- **Alpaca** — official `alpaca-py` SDK (see D-009 for why we picked SDK over MCP)
- **Anthropic API** — TradeMaster orchestration
- **DeepSeek API** — sub-agents
- **Google AI Studio API** — pre-market research
- **Discord Developer API** — bot
