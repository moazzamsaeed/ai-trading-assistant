# Runbook

## Current Phase

**Phase 2.3 (a + b) complete — dual-channel output.** Iron-condor strategist
runs at 9:45 ET, emits a broker-ready **manual signal** to `#signals` AND
auto-executes in paper mode with telemetry posted to `#trades`. Exit monitor
sweeps every 5 min with the same dual output (manual exit instructions +
automated close telemetry), and force-closes at 15:50 ET. Scheduler errors
route to `#logs`.

## Build Phases

| Phase | Goal | Status |
|---|---|---|
| 0 | Repo + scaffold | Done |
| 1.1 | Foundation: config, db, logging, models | Done |
| 1.2 | Router + provider clients + budget enforcement | Done |
| 1.3 | Pre-market research vertical slice (Alpaca news, Gemini, Discord, scheduler) | Done |
| 1.4a | Risk manager (cash-only, defined-risk, daily loss, max position, kill switch) | Done |
| 1.4b | Discord slash commands (/status /positions /cash /kill /pause /resume) | Done |
| 1.4c | Intraday scan loop (DeepSeek V4-Flash, every 15 min RTH, alert-only) | Done |
| 2.1 | Options chain wrapper + iron-condor leg construction | Done |
| 2.2 | Options strategist agent + entry-window scheduler (alert-only) | Done |
| 2.3a | Multi-leg order submission + paper-mode auto-execute | Done |
| 2.3b | Exit monitor (50% PT / 2x stop / 15:50 force-close) | Done |
| 2.3c | Live-mode approval flow (Discord /approve, /reject, /pending) | Done |
| 2.4 | Backtest harness (BS pricing + GBM paths) | Done |
| 3 | Crypto trend-follow + equity alerts | Not started |
| 4 | Dashboard + 30-day paper run + Nous Hermes Agent (D-010) | Not started |
| 5 | Live deployment review | Not started |

## How to Start the System

```bash
cd ~/ai-trading-assistant
source .venv/bin/activate

# Full daemon — Discord bot + scheduler (premarket 08:00 ET, intraday every 15 min RTH)
python -m trademaster.orchestrator

# Smoke tests
python -m trademaster.orchestrator --once        # one premarket briefing
python -m trademaster.orchestrator --scan-once   # one intraday scan
python -m trademaster.orchestrator --ic-once     # one iron-condor strategist run
```

Requires `.env` populated with `ALPACA_*`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
`DEEPSEEK_API_KEY`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, and channel IDs:
`DISCORD_CHANNEL_RESEARCH`, `DISCORD_CHANNEL_SIGNALS`, `DISCORD_CHANNEL_TRADES`,
`DISCORD_CHANNEL_LOGS`. The daemon refuses to start if the Alpaca account isn't
a cash account (D-001).

## Channel Routing

| Channel | Content |
|---|---|
| `#research` | Daily pre-market briefing (8 AM ET) |
| `#signals` | Broker-ready manual-trading alerts — specific strikes, expiry, calls/puts, side, entry price, exit thresholds. Acts as the comparison baseline against the bot's paper performance. |
| `#trades` | Automated bot trading activity — orders submitted, fills, exits, P&L per trade |
| `#logs` | Scheduler errors / diagnostics |
| `#commands` | Slash command responses (auto) |

## Discord Commands (owner-only)

All slash commands require you to be the bot owner. Synced per-guild.

| Command | What it does |
|---|---|
| `/status` | Trading mode, paused state, account snapshot, today's signals + P&L |
| `/positions` | Open Alpaca positions |
| `/cash` | Cash, buying power, equity, portfolio value |
| `/kill` | Emergency flatten — cancel orders + close positions, auto-pause 24h |
| `/pause <minutes>` | Pause new trades for N minutes |
| `/resume` | Clear pause |
| `/pending` | List live-mode trades awaiting approval |
| `/approve <id>` | Submit a pending live-mode trade to Alpaca |
| `/reject <id>` | Discard a pending live-mode trade |

In paper mode, the strategist auto-executes after risk-manager approval —
no `/approve` needed. In live mode (`TRADING_MODE=live`), the strategist
posts an "AWAITING APPROVAL" message to `#trades` and you confirm via
`/approve N`. Pending orders auto-expire after 15 minutes (D-014).

## Backtesting

The iron-condor strategy can be backtested against synthetic data
(GBM price paths + Black-Scholes option chains) using `python -m backtests.cli`:

```bash
python -m backtests.cli --start 2025-01-01 --end 2025-12-31 \
    --spy 500 --vol 0.15 --iv 0.18 --seed 42 \
    --csv backtests/results/2025_annual.csv
```

Outputs summary stats (win rate, expectancy, max drawdown, exit-reason
breakdown) to stdout and per-day P&L to the CSV. The same
`build_iron_condor` strategy code that runs in live mode is exercised,
so backtest results reflect actual production logic.

**Caveat (D-015):** the BS + GBM model is a first cut. Real SPY 0DTE
chains exhibit pin risk, volatility skew, and intraday IV shifts that
this synthetic generator doesn't model. Use the backtest to validate
the exit logic and parameter sensitivity, not to set live capital
sizing — that comes from the 30-day paper-trade.

## How to Stop the System

```bash
# Graceful: send SIGTERM, TradeMaster flushes positions log and exits
pkill -TERM -f "trademaster.orchestrator"
```

## Kill Switch (Emergency)

In Discord `#commands` channel:

```
/kill
```

This:
1. Cancels all open orders
2. Closes all open positions at market
3. Disables all trading until manual `/resume`
4. Posts confirmation to `#trades`

## What to Do If…

### TradeMaster crashes mid-trade
- Check `data/trademaster.log` for the last action
- Run `python -m trademaster.recover` (planned for Phase 4) — reconciles in-memory state with Alpaca
- If positions are open and you can't recover, use Alpaca dashboard to flatten manually

### Discord bot is offline
- Check `systemctl status trademaster-discord` (if systemd-managed)
- Bot disconnection does not stop trading — TradeMaster continues. Bot is for alerts only.

### Loss limit hit
- TradeMaster halts automatically and posts to `#alerts`
- All open positions are flattened
- Trading does not resume until next trading day OR manual `/resume`

### NUC reboots
- systemd restarts TradeMaster automatically (configured in Phase 4)
- On startup, TradeMaster reconciles position state with Alpaca before accepting new signals

## Monitoring

- **Dashboard:** http://nuc.local:3000 (or your NUC's IP)
- **Logs:** `data/trademaster.log`
- **DB inspection:** `sqlite3 data/trademaster.db`
- **Discord:** all human-relevant events posted to channels

## Manual Overrides (Discord commands)

| Command | Effect |
|---|---|
| `/kill` | Emergency flatten + halt |
| `/resume` | Resume trading after halt |
| `/pause <minutes>` | Pause new trades for N minutes |
| `/status` | Current positions, daily P&L, agent activity |
| `/approve <trade_id>` | Approve a pending trade in approval-mode |
| `/reject <trade_id>` | Reject a pending trade |
| `/positions` | List open positions |
| `/cash` | Show available buying power |
