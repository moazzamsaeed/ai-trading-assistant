# Runbook

## Current Phase

**Phase 0 — Foundation and scaffolding.** Repo created, structure laid out. No agents wired yet.

## Build Phases

| Phase | Goal | Status |
|---|---|---|
| 0 | Repo + scaffold | In progress |
| 1 | TradeRouter skeleton + Alpaca MCP + Discord bot + pre-market research agent | Not started |
| 2 | SPY 0DTE iron condor (backtest → paper) | Not started |
| 3 | Crypto trend-follow + equity alerts | Not started |
| 4 | Dashboard + scheduler + 30-day paper run | Not started |
| 5 | Live deployment review | Not started |

## How to Start the System

(Stub — will be filled in once Phase 1 lands.)

```bash
cd ~/ai-trading-assistant
source .venv/bin/activate
python -m traderouter.orchestrator
```

## How to Stop the System

```bash
# Graceful: send SIGTERM, TradeRouter flushes positions log and exits
pkill -TERM -f "traderouter.orchestrator"
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

### TradeRouter crashes mid-trade
- Check `data/traderouter.log` for the last action
- Run `python -m traderouter.recover` (planned for Phase 4) — reconciles in-memory state with Alpaca
- If positions are open and you can't recover, use Alpaca dashboard to flatten manually

### Discord bot is offline
- Check `systemctl status traderouter-discord` (if systemd-managed)
- Bot disconnection does not stop trading — TradeRouter continues. Bot is for alerts only.

### Loss limit hit
- TradeRouter halts automatically and posts to `#alerts`
- All open positions are flattened
- Trading does not resume until next trading day OR manual `/resume`

### NUC reboots
- systemd restarts TradeRouter automatically (configured in Phase 4)
- On startup, TradeRouter reconciles position state with Alpaca before accepting new signals

## Monitoring

- **Dashboard:** http://nuc.local:3000 (or your NUC's IP)
- **Logs:** `data/traderouter.log`
- **DB inspection:** `sqlite3 data/traderouter.db`
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
