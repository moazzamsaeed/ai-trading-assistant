---
name: Repo + daemon quick reference
description: Paths, commands, channel IDs, watchlist tickers for the TradeMaster project — quick lookup so future-me doesn't re-explore
type: reference
originSessionId: 957d0221-caf9-44af-930a-3e1e3bb9202b
---
## Paths
- Repo: `/home/moazzam/ai-trading-assistant`
- GitHub: `moazzamsaeed/ai-trading-assistant` (HTTPS via gh CLI; `git push origin main` works without prompt)
- Memory (this dir): `/home/moazzam/.claude/projects/-home-moazzam-ai-trading-assistant/memory/`
- SQLite DB: `data/trademaster.db` (auto-created)
- Watchlist file: `data/watchlist.json`
- Daemon stdout log: `/tmp/trademaster-live.log` (when started via nohup)

## Run commands
```bash
cd ~/ai-trading-assistant
uv run pytest -q           # all tests
uv run ruff check .         # lint
uv run python -m trademaster.orchestrator               # full daemon (Discord bot + scheduler)
uv run python -m trademaster.orchestrator --once        # one pre-market briefing then exit
uv run python -m trademaster.orchestrator --scan-once   # one intraday news scan
uv run python -m trademaster.orchestrator --ic-once     # one iron-condor strategist run
uv run python -m trademaster.orchestrator --dir-once    # one directional intraday scan

# Backtest (iron condor — synthetic data)
uv run python -m backtests.cli --start 2025-01-01 --end 2025-12-31 \
    --spy 500 --vol 0.15 --iv 0.18 --csv out.csv

# Background daemon (for live testing while keeping terminal usable)
nohup uv run python -m trademaster.orchestrator > /tmp/trademaster-live.log 2>&1 &
disown
pkill -TERM -f "trademaster.orchestrator"   # stop
```

## .env state (don't commit, gitignored)
Has real keys for: Anthropic, Google AI Studio, DeepSeek, Alpaca paper. Discord bot token + 6 channel IDs filled. `TRADING_CAPITAL_USD=5000`. `MONTHLY_LLM_BUDGET_USD=100`.

## Discord channel mapping
- `#research` — daily pre-market briefing (Gemini 2.5 Pro)
- `#signals` — broker-ready manual buy/sell instructions
- `#trades` — automated bot execution telemetry
- `#logs` — scheduler/agent errors
- `#commands` — slash command interactions (auto)
- `#watchlist` — current ticker list (auto-posted on add/remove)

## Slash commands (12 registered, owner-only)
- `/status` `/positions` `/cash` — read state
- `/kill` `/pause <min>` `/resume` — emergency control
- `/pending` `/approve <id>` `/reject <id>` — live-mode trade approval (D-014, paper auto-executes)
- `/watchlist` `/watchlist_add <ticker>` `/watchlist_remove <ticker>` — manage the watchlist

## Watchlist tickers (as of 2026-05-11)
`SPY, QQQ, QQQM, VOO, VTI, META, MSFT, GOOG, AMD, TSLA, NVDA` (11 tickers, in `data/watchlist.json`).

## Daemon scheduled jobs (when running)
- 8:00 ET Mon-Fri — pre-market briefing
- 9:00, 9:15, 9:30, 9:45 ET (etc., every 15 min) — intraday news scan
- 9:00, 9:10, 9:20… every 10 min RTH — directional intraday scan
- 9:45 ET — iron-condor strategist (paused per current focus — will be removed)
- 10:00, 10:05, … every 5 min RTH — exit monitor
- 15:50 ET — force-close any open ICs

## Important env knobs
- `TRADING_MODE=paper|live` — paper auto-executes, live needs `/approve`
- `ACCOUNT_TYPE=cash` — locked, D-001
- `TRADING_CAPITAL_USD=5000` — working-capital cap
- `MONTHLY_LLM_BUDGET_USD=100` — router refuses non-essential calls past cap
- `DAILY_LOSS_LIMIT_USD=500` — risk-manager halt trigger
- `MAX_POSITION_SIZE_USD=2000` — per-trade notional cap
- `MAX_CONCURRENT_POSITIONS=5`
