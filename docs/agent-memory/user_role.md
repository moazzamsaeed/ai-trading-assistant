---
name: User profile — retail trader + dev
description: Who the user is, how they want to collaborate on the TradeMaster project
type: user
originSessionId: 957d0221-caf9-44af-930a-3e1e3bb9202b
---
Retail trader who is also building a personal-projects ecosystem. TradeMaster (the AI options/equity trading system) is one "lego piece" — they plan to add other unrelated projects later, all coordinated by Nous Hermes Agent at the ecosystem layer (D-010, deferred to Phase 4).

Capital posture:
- Paper Alpaca account ($100k default) capped to **$5k working capital** via `TRADING_CAPITAL_USD=5000` — paper P&L maps 1:1 to a real $5k live account they plan to fund later.
- Plan: prove edge on $5k → compound to $50k via consistent 10–15%/month → at $50k, 10–15% becomes meaningful ($5–7.5k/month) as secondary income.
- Cash-only, defined-risk only (D-001). Refuses margin and naked options.

Trading approach:
- Trades manually too — wants Discord `#signals` to give broker-ready buy/sell instructions for each leg, separately from what the bot auto-executes (which goes to `#trades`). D-013.
- Iron condors are paused as of 2026-05-11 — per-trade capital usage (~9%) too high for the meager expectancy ($50/mo expected on $5k).
- New direction: aggressive directional options first, then mix in selective strategy if aggressive doesn't deliver. See `project_current_focus.md`.

Collaboration style:
- Has given Claude broad autonomy: "go ahead and commit/push", "make it best-possible-robust", "use whichever model is best".
- Pushes back when realistic — has correctly called out wrong day-of-week, the dummy "no signals all day" gap, options jargon, and tiny IC expectancy. Worth taking those calls seriously without arguing.
- Wants frank answers including "this is unrealistic" — explicitly told Claude not to sell false hope on a 50%/month target.
- Tools enabled: Anthropic Pro subscription + direct API keys, Google AI Studio, DeepSeek API, Alpaca paper, Discord bot with 6 channels.

Goals reflected in build:
- Long-term durable ecosystem (multiple projects), not a one-off bot.
- Wants the trading bot to behave like a disciplined risk-manager, not a YOLO gambler.
- Will go live only after 30+ days of paper-trade validation (D-005).
