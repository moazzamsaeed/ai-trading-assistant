# Runbook

## Current Phase

**Phase 1.3 — Pre-market research vertical slice live.** Alpaca news client,
Gemini-driven briefing agent, APScheduler with 8am ET cron, and a minimal
Discord poster are wired end-to-end. Risk manager + slash commands land in
Phase 1.4.

## Build Phases

| Phase | Goal | Status |
|---|---|---|
| 0 | Repo + scaffold | Done (`344441e`) |
| 1.1 | Foundation: config, db, logging, models | Done (`540d197`) |
| 1.2 | Router + provider clients + budget enforcement | Done (`874f8e7`) |
| 1.3 | Pre-market research vertical slice (Alpaca news, Gemini, Discord, scheduler) | Done |
| 1.4 | Risk manager + Discord slash commands + intraday scan loop | Not started |
| 2 | SPY 0DTE iron condor (backtest → paper) | Not started |
| 3 | Crypto trend-follow + equity alerts | Not started |
| 4 | Dashboard + 30-day paper run + Nous Hermes Agent (D-010) | Not started |
| 5 | Live deployment review | Not started |

## How to Start the System

```bash
cd ~/ai-trading-assistant
source .venv/bin/activate

# Full daemon (Discord bot + scheduler firing at 8am ET Mon-Fri)
python -m trademaster.orchestrator

# Smoke test: fire one pre-market briefing now and exit
python -m trademaster.orchestrator --once
```

Requires `.env` populated with `ALPACA_*`, `GOOGLE_API_KEY`, `DISCORD_BOT_TOKEN`,
and `DISCORD_CHANNEL_RESEARCH` at minimum.

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
