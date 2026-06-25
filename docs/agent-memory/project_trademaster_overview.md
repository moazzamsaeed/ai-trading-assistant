---
name: TradeMaster system — what's built as of 2026-05-11
description: High-level snapshot of the trading agent system, including which agents/strategies are live, key decisions, and where the daemon stands
type: project
originSessionId: 957d0221-caf9-44af-930a-3e1e3bb9202b
---
Multi-agent SPY/options/equity trading system at `/home/moazzam/ai-trading-assistant`. Package: `trademaster/` (was `hermes/` then `traderouter/` — see D-011, D-012). Single Python process orchestrating Discord + Alpaca + 3 LLM providers.

**Why:** User wants a passive-secondary-income trading bot they can validate on paper, then go live with $5k → compound to $50k.

**How to apply:** When picking up work, this is the high-level map. Always read code (`git log -5`, recent commits) for current state — this memory captures stable structure, not commit-by-commit progress.

## Models (D-002, D-016)
| Task | Model |
|---|---|
| Orchestrate / execution decisions | claude-opus-4-7 |
| Pre-market research | gemini-2.5-pro (swapped from gemini-3.1-pro-preview which 503'd chronically — D-016) |
| Options strategy | deepseek-v4-pro |
| Intraday scans | deepseek-v4-flash |
| Budget cap | MONTHLY_LLM_BUDGET_USD=100 |

## Agents (each has its own scheduler job)
- **`agents/research/premarket.py`** — daily 8:00 ET briefing → `#research`. Working live (verified 2026-05-11).
- **`agents/intraday/scan.py`** — every 15 min RTH, news-only → `#signals` if actionable (mostly HOLDs in quiet markets).
- **`agents/options/strategist.py`** — SPY 0DTE iron condor at 9:45 ET → `#signals` (manual) + `#trades` (auto-execute in paper). **Paused as of 2026-05-11** — see `project_current_focus.md`.
- **`agents/options/executor.py`** — multi-leg order submission, paper auto + live `/approve` (D-014).
- **`agents/options/exit_monitor.py`** — every 5 min RTH, 50% PT / 2× stop / 15:50 force-close. Verified working with forced test trade today.
- **`agents/directional/intraday.py`** — directional options agent (BUY_CALL/BUY_PUT) every 10 min RTH. Built, alert-only, never run live yet. **This is the next focus.**

## Strategy state
| Strategy | Status | Notes |
|---|---|---|
| SPY 0DTE iron condor | **Paused** | Math doesn't work on $5k — see project_current_focus.md |
| Directional options (selective) | **Built, untested live** | DeepSeek V4-Pro decides BUY_CALL/BUY_PUT/HOLD per ticker |
| Directional options (aggressive) | **Not built yet** | Step 2 of next-up plan |
| Equity VWAP alerts | Not built | Phase 3 in original plan |
| Crypto trend-follow | Not built | Phase 3 in original plan |

## Infrastructure landmarks (don't re-derive)
- **Black-Scholes math**: `trademaster/options_math.py` (shared between live + backtest). IV solved per-strike via bisection because Alpaca's indicative feed gives no greeks (D-017).
- **Working-capital cap**: `TRADING_CAPITAL_USD=5000` enforced in risk_manager via `effective_cash = min(account.cash, cap)` + `deployed_capital_usd()`.
- **Channel split (D-013)**: `#signals` (manual broker-ready), `#trades` (bot execution), `#research` (briefing), `#logs` (errors), `#commands` (slash interactions), `#watchlist` (current ticker list).
- **Watchlist** (`trademaster/watchlist.py`): JSON file at `data/watchlist.json`, mutable via `/watchlist_add` `/watchlist_remove` slash commands.

## Test + lint discipline
- pytest + ruff run after every meaningful change. 248 tests passing as of last commit.
- Per-file ruff override allows quant-convention S/K/T/sigma in BS math files.

## Decisions log
`docs/DECISIONS.md` has D-001 through D-017. Read it before changing any architectural assumption.

## Phase 1+2 complete
- Phase 1: foundation, router, pre-market, intraday news scan, risk manager, Discord slash commands ✅
- Phase 2: iron condor strategy + execution + exit + approval flow + backtest harness (synthetic data) ✅
- Phase 3 (crypto + equity VWAP), Phase 4 (dashboard + Hermes Agent install), Phase 5 (live deployment) — not started.
