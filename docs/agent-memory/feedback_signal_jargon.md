---
name: Manual signals must use plain language, no options jargon
description: User-facing #signals output should be broker-ready buy/sell actions, not "iron condor / credit spread / theta / 2× stop"
type: feedback
originSessionId: 957d0221-caf9-44af-930a-3e1e3bb9202b
---
When formatting messages posted to `#signals` (the manual-trading channel), use **plain English buy/sell call/put language**. Each leg gets its own numbered line so the user can copy each into their broker.

**Why:** The user trades these manually through their own broker app. Options-spread terminology ("credit", "debit", "net credit", "wing", "delta", "profit target threshold") is friction. They want "Buy SPY $740 CALL" type instructions.

**Concretely — forbidden in #signals messages:**
- "iron condor", "credit spread", "credit", "debit", "net credit", "net debit"
- "Profit target", "Stop loss", "PT", "2× stop", "wing", "leg"
- "delta", "theta", "vega", "gamma", "IV rank"

**Use instead:**
- `Sell SPY $738 PUT (about $0.65)`
- `Buy back SPY $738 PUT` (for closes)
- `Hold all four until you see an EXIT message here, or close everything by 15:50 ET`
- `You'll collect about $80 cash. About $420 of your cash gets tied up until you close.` (when explaining IC entry)

**Multi-leg actions** (IC entry/exit): list each leg as a numbered step (1./2./3./4.).

**Single-leg directional** (BUY_CALL/BUY_PUT from the directional agent): `BUY a CALL on SPY · strike $745 · expiry today (2026-05-12)`.

**Exit signals** use `🚨 SPY EXIT now — <plain reason>` headers and number the close legs. Reason mapping: `profit_target_50pct` → "✅ profit target hit", `stop_loss_2x` → "🛑 stop loss — cap the loss now", `force_close` → "⏰ closing before market close".

**Exception — `#trades` channel** (automated execution telemetry) can keep technical terms ("filled", "limit", "order_id"). That channel is for monitoring what the bot did, not for human-actionable trades.

**How to apply:** When editing or adding signal formatters in `agents/options/strategist.py`, `agents/options/exit_monitor.py`, `agents/directional/intraday.py`, or any new agent: keep `#signals` text scannable by someone who doesn't know options spreads. If the user can't paste a numbered line into their broker, the format is wrong.
