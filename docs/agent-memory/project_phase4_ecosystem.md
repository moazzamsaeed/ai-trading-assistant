---
name: Phase 4 — Ecosystem platform design decisions
description: Hermes Agent + Mission Control — key decisions made before implementation, covers architecture, model choice, Discord setup
type: project
originSessionId: de5df9ae-1d5d-4c52-970a-ceb0cfe3562b
---
Phase 4 plan is fully designed and approved. Implementation begins next session. Full plan at `/home/moazzam/.claude/plans/let-s-discuss-a-few-purrfect-waffle.md`.

**Why:** User wants a plug-and-play ecosystem where new projects integrate in ~30 min with no changes to existing code. TradeMaster is one "lego piece"; future projects slot in the same way.

**How to apply:** When starting Phase 4 implementation, read the plan file first. Do not re-research or re-plan.

## Key decisions (already settled — do not re-debate)

**Discord architecture:** Single server (existing), Discord categories per project. NOT multiple servers — user is solo operator, single server with categories gives same visual separation with unified Hermes context.

**Access control:** Owner = full write/interact. Others = @Member role = read-only. Hermes bot and TradeMaster bot get their own roles with write permissions to their respective categories.

**Project registry:** `registry.yaml` in `hermes-config/` repo is the single source of truth. All projects register here. Mission Control sidebar auto-builds from it. Hermes reads it for cross-project status queries.

**Hermes model:** Claude Sonnet 4.6 via Anthropic API (`bring-your-own-model` config). Same model used to build TradeMaster. Opus 4.7 = overkill, Haiku = too limited.

**Hermes built-in dashboard:** Port 9119 — for Hermes admin (skills, sessions, Kanban, cron). NOT our Mission Control. They coexist on different ports.

**Hermes Kanban:** Used for Claude Code task delegation tracking — Hermes creates a Kanban card when it spawns a Claude Code sub-agent, closes it when done. Built-in heartbeat handles stalls.

**Communication layer:** Skills are transport-agnostic (return text; Hermes handles routing). WhatsApp/Telegram added via config only, no skill changes. Telegram is immediately available (Hermes native support).

**Build model for implementation:** Claude Sonnet 4.6 throughout.

## New repos to create
- `~/projects/hermes-config/` — registry, skills, scaffold script
- `~/projects/mission-control/` — Next.js dashboard

## TradeMaster repo: NO CHANGES for Phase 4
All Phase 4 work is in the two new repos. `ai-trading-assistant/` is read-only from the ecosystem's perspective.
