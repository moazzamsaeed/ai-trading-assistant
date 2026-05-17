# Live Readiness Checklist

This file is the hard gate before enabling `TRADING_MODE=live`.
Do not enable live trading until every item is checked.

Last updated: 2026-05-17

---

## Documentation

- [x] Model names reconciled — actual API model IDs in ARCHITECTURE.md
- [x] Alpaca SDK vs MCP decision reconciled — D-003 marked superseded by D-009
- [x] Phase status corrected — RUNBOOK.md reflects actual build state
- [ ] Service names standardized across all runbooks
- [ ] Discord channel names standardized across all docs

---

## Risk Controls

- [x] Daily loss limit — halts trading at 15% of capital
- [x] Weekly loss limit — halts until Monday at 25% of capital
- [x] Max trades per day — blocks entries after 6 trades (configurable)
- [x] Event blackout calendar — blocks entries on FOMC/CPI/NFP days
- [x] Bid/ask spread filter — rejects illiquid options (spread > 50% of mid)
- [x] Broken quote guard — rejects stale/corrupted quotes (ask > 5× bid)
- [x] Startup reconciliation — repairs DB vs Alpaca mismatches on restart
- [x] Mode-aware hard floor — aggressive −50%, selective −30%
- [x] Exit monitor runs when paused — open positions always protected
- [ ] Slippage guard — reject fills where slippage > X% vs limit price
- [ ] Manual news halt command — granular pause (use `/pause` in Discord for now)

---

## Strategy Validation

- [ ] At least 30 trading days of paper logs collected
- [ ] Win rate calculated
- [ ] Average win calculated
- [ ] Average loss calculated
- [ ] Expectancy (EV per trade) calculated
- [ ] Max drawdown calculated
- [ ] Slippage measured (paper fill vs mid at signal time)
- [ ] Fill quality measured
- [ ] Performance by event vs non-event day measured
- [ ] Performance by day-of-week measured (Tue/Thu vs Mon/Wed/Fri)
- [ ] Iron condor: paper-only until real historical options-chain backtest completed

---

## Operations

- [x] TradeMaster daemon restarts automatically (systemd `trademaster.service`)
- [x] Crash recovery — reconciler runs on startup
- [x] Discord bot operational
- [ ] Kill switch tested end-to-end on paper account
- [ ] Discord approval flow tested (live mode only)
- [ ] Manual flattening procedure documented and tested
- [ ] Hermes read-only vs admin-approved permission boundary defined

---

## Live Mode Configuration

- [ ] `TRADING_MODE=live` tested with $0 positions first (read-only API check)
- [ ] First live phase uses very small capital allocation (< 20% of funded account)
- [ ] Live trading can be paused immediately via Discord `/pause`
- [ ] Daily review process documented
- [ ] No strategy uses only synthetic backtest results for live sizing
- [ ] Pending live orders expire after 15 minutes (Discord approval gate)

---

## Sign-Off

Live trading requires explicit sign-off after all items above are checked.
Record the date and a brief note on any items that were waived and why.

| Item | Status | Date | Notes |
|---|---|---|---|
| 30-day paper run | ⏳ In progress | 2026-05-13 start | — |
| Risk controls complete | ✅ | 2026-05-17 | See commit `cde0a4a` + `e8fa8f6` |
| Docs reconciled | ✅ | 2026-05-17 | ARCHITECTURE, DECISIONS, RUNBOOK updated |
