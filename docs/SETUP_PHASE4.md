# TradeMaster — Phase 4 Ecosystem Setup Guide

Documents the ecosystem layer built around TradeMaster on 2026-05-13. TradeMaster's own code was **not changed** in Phase 4 — this phase built the external tooling that manages and monitors it.

For TradeMaster's internal setup (Phases 1–3), see `ARCHITECTURE.md` and `DECISIONS.md`.

---

## What Phase 4 Added

| Component | Location | Purpose |
|---|---|---|
| Hermes Agent | `~/.hermes/` + `~/projects/hermes-config/` | Discord AI assistant that can query status, restart daemon, run backtests |
| Mission Control | `~/projects/mission-control/` | Next.js dashboard with live trades, signals, LLM cost analytics |
| Project registry | `~/projects/hermes-config/registry.yaml` | Single source of truth for all ecosystem projects |
| TradeMaster skills | `~/projects/hermes-config/skills/hermes/trademaster/` | Hermes skill doc for TradeMaster operations |

TradeMaster's repo, DB, systemd service, and cron schedule were unchanged.

---

## Prerequisites

- TradeMaster daemon already set up and paper trading (`trademaster.service` enabled)
- DB at `~/ai-trading-assistant/data/trademaster.db` with `trades`, `signals`, `agent_runs` tables
- Discord server exists and TradeMaster bot is already in it
- Anthropic API key already in `~/ai-trading-assistant/.env`

---

## Step 1 — Set Up hermes-config Repo

```bash
mkdir -p ~/projects/hermes-config
cd ~/projects/hermes-config && git init && git branch -m main
```

Create `registry.yaml` registering TradeMaster:

```yaml
projects:
  - id: trademaster
    name: AI Trading Assistant
    repo: ~/ai-trading-assistant
    service: trademaster
    discord:
      category: "🏦 TRADING"
      channels: [signals, trades, research, logs, watchlist, commands]
    health: null
    data:
      type: sqlite
      path: ~/ai-trading-assistant/data/trademaster.db
    skills: skills/trademaster/
    dashboard: dashboard/trademaster/
```

---

## Step 2 — Install Hermes Agent

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.bashrc
```

Configure `~/.hermes/config.yaml`:
- Set `model.default: claude-sonnet-4-6`
- Set `model.provider: anthropic`
- Comment out `base_url` (otherwise it overrides the provider and routes to OpenRouter)
- Add external skills directory:
  ```yaml
  skills:
    external_dirs:
      - ~/projects/hermes-config/skills/hermes/
  ```

Set secrets in `~/.hermes/.env`:
```bash
ANTHROPIC_API_KEY=<same key as TradeMaster>
DISCORD_BOT_TOKEN=<new Hermes bot token — separate from TradeMaster bot>
DISCORD_ALLOWED_USERS=<your Discord user ID>
DISCORD_GUILD_ID=<same guild ID as TradeMaster>
```

---

## Step 3 — Write TradeMaster Hermes Skill

Create `~/projects/hermes-config/skills/hermes/trademaster/SKILL.md` — a Markdown guide that teaches Hermes how to:
- Query daemon state via `systemctl --user is-active trademaster`
- Query today's P&L from SQLite using Python with ET-aware date boundaries (critical: `DATE('now')` in SQLite uses UTC, not ET)
- Restart the daemon
- View logs
- Run backtests via `backtests/directional_cli.py`
- Read/write `data/watchlist.json`

The ET-aware query pattern:
```python
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
today_et = datetime.now(ET).date().isoformat()
day_start_utc = datetime.fromisoformat(today_et).replace(tzinfo=ET).astimezone(timezone.utc).isoformat()
day_end_utc   = (datetime.fromisoformat(today_et).replace(tzinfo=ET) + timedelta(days=1)).astimezone(timezone.utc).isoformat()
# Use as: WHERE closed_at >= ? AND closed_at < ?
```

---

## Step 4 — Create a Second Discord Bot for Hermes

TradeMaster and Hermes each need their own bot to operate independently:

1. [discord.com/developers](https://discord.com/developers) → New Application → name: `Hermes` → Bot tab → Reset Token
2. Enable **Message Content Intent**
3. Add to the server:
   ```
   https://discord.com/api/oauth2/authorize?client_id=<APP_ID>&permissions=8&scope=bot&guild_id=<GUILD_ID>
   ```
4. Create the OVERVIEW Discord category:
   ```bash
   cd ~/projects/hermes-config
   HERMES_BOT_TOKEN=<token> DISCORD_GUILD_ID=<id> \
   python3 scripts/setup-discord.py --category "📊 OVERVIEW" --channels hermes status
   ```

---

## Step 5 — Start Hermes Gateway

```bash
hermes gateway install    # registers hermes-gateway.service
hermes gateway start
hermes gateway status     # verify: active (running), discord connected
```

Test from Discord: send `trademaster status` in **#hermes** → Hermes queries the live DB and responds.

Test from CLI:
```bash
hermes chat -q "trademaster status" -s trademaster --yolo -Q
```

---

## Step 6 — Set Up Mission Control Dashboard

```bash
cd ~/projects
npx create-next-app@15 mission-control --typescript --tailwind --app --src-dir --no-eslint --import-alias "@/*" --yes
cd mission-control
npm install better-sqlite3 @types/better-sqlite3 js-yaml @types/js-yaml recharts
```

Key implementation notes for TradeMaster integration:
- DB opened read-only: `new Database(dbPath, { readonly: true })`
- `serverExternalPackages: ["better-sqlite3"]` required in `next.config.ts`
- Decimal fields from SQLite returned as strings (avoid float precision loss in JS)
- Datetime fields re-suffixed with `Z` after read (SQLite drops timezone info)

Deploy:
```bash
npm run build
~/.local/lib/node_modules/pm2/bin/pm2 start npm --name mission-control -- start -- --port 3000
~/.local/lib/node_modules/pm2/bin/pm2 save
```

Dashboard at **http://192.168.1.96:3000/trademaster** shows:
- Overview tab: live daemon status, P&L today, open positions, LLM spend MTD
- Trades tab: full trade table + equity curve
- Signals tab: all 172+ signals with acceptance stats
- Agent Performance tab: LLM cost by provider, daily spend vs budget
- Reports tab: weekly breakdown

---

## How TradeMaster Connects to the Ecosystem

```
TradeMaster daemon
  │
  ├─ writes to ──► trademaster.db
  │                    │
  │                    ├─ read by ──► Mission Control dashboard (port 3000)
  │                    └─ read by ──► Hermes trademaster skill (via sqlite3 CLI or Python)
  │
  ├─ managed by ──► hermes-gateway.service
  │                    └─ listens in ──► Discord #hermes
  │
  └─ monitored by ──► systemd user service (trademaster.service)
                          └─ auto-start: cron 7:55 AM ET weekdays
                          └─ auto-stop:  cron 4:15 PM ET weekdays
```

---

## Nothing Changed in TradeMaster's Code

Phase 4 is purely additive infrastructure. The trading logic, risk manager, agents, DB schema, and Discord channels are untouched. The ecosystem layer reads TradeMaster's DB and service in read-only fashion.
