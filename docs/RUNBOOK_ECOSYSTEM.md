# TradeMaster — Ecosystem Runbook (Phase 4)

Operational reference covering TradeMaster in the context of the full Phase 4 ecosystem. For TradeMaster internals (agents, strategies, slash commands), see `RUNBOOK.md`.

---

## Full System Overview

| Service | Managed by | Auto-start | Port |
|---|---|---|---|
| `trademaster.service` | systemd user | Cron 7:55 AM ET weekdays | — |
| `hermes-gateway.service` | systemd user | Always (linger enabled) | — |
| `mission-control` (pm2) | pm2 | Always (pm2 startup) | 3000 |

---

## Daily Schedule

| Time (ET) | Event |
|---|---|
| 7:55 AM | TradeMaster daemon starts (cron) |
| 8:00 AM | Pre-market research agent runs → posts to #research |
| 9:35 AM | Directional scanner begins (every 10 min) |
| 4:15 PM | TradeMaster daemon stops (cron) |
| All day | Hermes gateway running, responds to #hermes |
| All day | Mission Control dashboard live at port 3000 |

---

## Check Full Ecosystem Status

```bash
# TradeMaster daemon
systemctl --user is-active trademaster

# Hermes gateway
hermes gateway status

# Mission Control
~/.local/lib/node_modules/pm2/bin/pm2 status

# Quick DB snapshot
sqlite3 ~/ai-trading-assistant/data/trademaster.db "
  SELECT 'open_trades', COUNT(*) FROM trades WHERE closed_at IS NULL
  UNION SELECT 'total_signals', COUNT(*) FROM signals
  UNION SELECT 'agent_runs', COUNT(*) FROM agent_runs;"
```

---

## Starting / Stopping

**Start everything (e.g. after NUC reboot):**
```bash
# Hermes and Mission Control start automatically via systemd/pm2.
# TradeMaster starts via cron — or manually:
systemctl --user start trademaster
```

**Stop TradeMaster without affecting ecosystem:**
```bash
systemctl --user stop trademaster
# Hermes and Mission Control keep running
```

**Restart Mission Control after dashboard code change:**
```bash
cd ~/projects/mission-control && npm run build
~/.local/lib/node_modules/pm2/bin/pm2 restart mission-control
```

**Restart Hermes after skill or config change:**
```bash
hermes gateway restart
```

---

## Monitoring TradeMaster via Hermes (Discord)

From **#hermes** on Discord:

| What you want | What to say |
|---|---|
| Current P&L and positions | `trademaster status` |
| Recent logs | `show me the last 50 trademaster logs` |
| Restart daemon | `restart the trading daemon` |
| Run a backtest | `run a backtest from 2026-05-01 to 2026-05-12` |
| Add a ticker | `add AMZN to the watchlist` |
| Remove a ticker | `remove AMD from the watchlist` |
| Weekly summary | `show me this week's trading report` |
| Delegate a code change | `@Hermes refactor the directional scanner to add MACD` |

---

## Monitoring TradeMaster via Mission Control

Open **http://192.168.1.96:3000/trademaster**:

| Tab | What you see |
|---|---|
| Overview | Daemon status badge, P&L today, open positions, LLM spend MTD |
| Trades | Full sortable/filterable trade table + equity curve chart |
| Signals | 172+ signals with acceptance rate donut + conviction distribution |
| Agent Performance | LLM cost breakdown by provider, daily spend vs $100 budget line |
| Reports | Weekly P&L breakdown, win rate, top ticker |

Data refreshes every 30 seconds automatically.

---

## Adding a New Project to the Ecosystem

One command scaffolds everything:

```bash
bash ~/projects/hermes-config/scripts/add-project.sh <id> "<Name>" <repo-path>
```

Then:
1. Fill in `~/projects/hermes-config/skills/hermes/<id>/SKILL.md`
2. Fill in `~/projects/mission-control/src/components/projects/<id>/`
3. `hermes gateway restart`
4. `cd ~/projects/mission-control && npm run build && pm2 restart mission-control`

TradeMaster code: untouched. Hermes core: untouched. Mission Control core: untouched.

---

## Key Paths — Full Ecosystem

| Path | Purpose |
|---|---|
| `~/ai-trading-assistant/` | TradeMaster source |
| `~/ai-trading-assistant/data/trademaster.db` | Live SQLite DB |
| `~/.config/systemd/user/trademaster.service` | TradeMaster systemd unit |
| `~/projects/hermes-config/` | Hermes skills, registry, scaffold scripts |
| `~/projects/hermes-config/registry.yaml` | Ecosystem registry (drives sidebar + Hermes cross-project queries) |
| `~/.hermes/config.yaml` | Hermes model and config |
| `~/.hermes/.env` | Hermes API keys and Discord tokens |
| `~/projects/mission-control/` | Dashboard source |
| `~/projects/mission-control/.next/` | Built dashboard (served by pm2) |

---

## GitHub Repos

| Repo | URL |
|---|---|
| TradeMaster | https://github.com/moazzamsaeed/ai-trading-assistant |
| Hermes Config | https://github.com/moazzamsaeed/hermes-config |
| Mission Control | https://github.com/moazzamsaeed/mission-control |
