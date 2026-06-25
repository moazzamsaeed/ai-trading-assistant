---
name: Futures integration parked
description: Futures (MES/MNQ via Tradovate) was researched and planned but deferred until after the options system is live
type: project
originSessionId: 09167020-876b-40fa-804e-6f0aeebee77a
---
Futures integration (MES/MNQ on Tradovate) is **parked** as of 2026-05-13.

**Why:** Focus is on getting the options system fully live and stable first. Adding a second broker + new asset class while the core options flow is still being tuned would spread attention too thin.

**How to apply:**
- Don't propose futures work or revisit the Tradovate plan until the user explicitly says options is live and stable.
- Plan file already exists at `/home/moazzam/.claude/plans/let-s-start-implementing-the-enumerated-thompson.md` — reuse it when futures is revived rather than re-researching brokers.
- Key decision already made: Tradovate (REST API, no desktop app) was selected over IBKR/Thinkorswim. Don't re-debate broker choice.
- Bar data plan: Alpaca's free market data API covers MES/MNQ bars — no separate data subscription needed for signals.
- Demo account is free; live trading would add ~$15–100/month in CME data + Tradovate platform fees.
