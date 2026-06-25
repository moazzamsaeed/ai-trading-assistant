---
name: Lessons learned — paper-trading reality check (May 14-22 2026)
description: Real failure modes observed across the first 2 weeks of paper trading. Each lesson is paired with the fix that's now in production. Don't re-introduce removed protections without understanding what they prevented.
type: project
originSessionId: 204ac897-9804-44a1-9643-645984448da4
---
## The big picture

After 2 weeks of paper trading: 33+ closed trades, **21% win rate**, net realized **−$2,600+**. The LLM analyzing standard indicators (VWAP, RSI, EMA, MACD) has no proven directional edge over a coin flip. Most fixes have been risk management, not signal quality. The win-rate problem is NOT solved by these fixes.

**Why save:** These specific failures will tempt re-introduction of "convenience" features that were removed for cause. Don't.

**How to apply:** Before adding any new rule or removing an existing one, check this list — many of these are scars from real losses.

---

## Failure mode #1 — Trading individual stocks against macro trends (week 1)

Pattern: System opened puts on NVDA/QQQ on a day SPY was grinding higher. "rel_vs_spy = −0.7%" looked like a put signal but the stock was still going UP, just slower than SPY. Lost hundreds across multiple trades.

**Fix:** SPY-only watchlist. Don't go back to multi-ticker until there's evidence individual-stock direction can be predicted better than a coin flip.

---

## Failure mode #2 — Trade #37 (May 22): all-or-nothing trailing stop

Single trade lost $952. Setup was reasonable (VWAP+RSI+EMA+volume all aligned, 1.65x volume), but:
- 56 contracts of cheap deep-OTM 0DTE ($0.53/share) — high contract count amplified loss
- Peak hit ~+27% intraday but 5-min polling captured only +5.66% peak (peak was between checks)
- No partial profit-taking — went +27% → −32% in 40 min, all-or-nothing
- First trailing tier was +20%, position never sustained long enough to register

**Fix (committed):**
- **30-second trailing tick** (`run_trailing_stop_tick`) catches peaks between 5-min sweeps
- **Scale-out tiers** sell 25% chunks at +15/+30/+50% so 75% is incrementally locked in
- **First tier dropped** +20% → +15% for faster reversals
- **Hard 5s/20s timeouts** on the tick — one tick hung today for 4+ minutes
- Persisted via `original_qty`, `scale_out_tiers_fired`, `partial_realized_pnl_usd` in trade.extra

---

## Failure mode #3 — Cron silently not firing premarket briefing

Multiple mornings the 7:45 AM cron-based daemon start didn't fire. No error in logs, no entry in CRON syslog for the user crontab. Linger was enabled, cron was running, but `systemctl --user` jobs weren't executing reliably.

**Fix:** Switched to **systemd user timers** (`trademaster-start.timer`, `trademaster-stop.timer`) with `Persistent=true`. Cron entries left as backup. Service file now has `Restart=always` + `RestartPreventExitStatus=0` so crash recovery is automatic but intentional cron-driven stops don't trigger restarts.

---

## Failure mode #4 — Removing a protection without understanding the failure it prevented

Removed `medium_conviction_0dte_blocked` from executor because it was blocking valid SPY MEDIUM 0DTE trades. Then trade #37 happened — exactly the kind of cheap-OTM-high-contract-count disaster that block was preventing. The block was originally written for illiquid PLTR $0.15 options but the underlying failure mode (OTM 0DTE has negative EV without strong conviction + cheap premium → high contract count → big absolute loss) still applies to SPY.

**Lesson:** When removing a protection, check what original failure mode it was designed for. The "this was for individual stocks, doesn't apply to SPY" reasoning was wrong here — the failure mode is identical.

**If reintroducing per-trade dollar caps OR strike-delta minimums:** this is the reason.

---

## Failure mode #5 — Polling frequency too coarse for 0DTE

5-minute exit monitor missed a +21% peak swing on trade #37. The peak existed but happened between scheduled checks. Indicator data is on 5-min bars (no point checking faster for those), but the BID can move 30% in 2 minutes on 0DTE.

**Fix:** Separated concerns. Indicator+LLM smart_exit runs every 5 min (unchanged — indicators are still on 5-min bars). New 30-sec tick polls bid only, runs trailing/scale-out math. Costs nothing extra (LLM doesn't fire from the tick).

---

## Failure mode #6 — Force-close bug killed weekly positions

A global `force=True` 3:30 PM job was closing ALL positions, including weekly options with days remaining. Cost $110+ in unnecessary losses on QCOM/PLTR/AMZN weeklies.

**Fix:** Removed the global force-close job. The regular 5-min exit monitor's per-trade `expiry == today` check handles 0DTE force-close correctly. Added a 15:50 safety net but with `force=False` so the per-trade check still applies.

---

## Failure mode #7 — Ghost positions calling BUY instead of SELL

Executor was calling `submitter` (the BUY function) for ghost-position recovery instead of a SELL function. Would have doubled positions instead of recovering them. Caught in audit before causing damage.

**Fix:** Separate `seller` parameter in `execute_directional_signal`.

---

## Failure mode #8 — Exit monitor skipping when paused

When daily loss limit triggered a 24h pause, exit monitor ALSO skipped — leaving open positions completely unmonitored. Catastrophic if hit a hard stop during the pause.

**Fix:** Exit monitor now runs regardless of pause state. Pause only blocks NEW entries.

---

## Failure mode #9 — Silent failures (from week 1, still valid)

Three separate bugs all failed silently, producing wrong behavior with no alerts:
- **SIP bars**: empty `bars=[]` → no crash, LLM hallucinated indicators
- **IOC option sells**: order rejected → code auto-closed DB row, position still live in Alpaca
- **`get_today_realized_pnl` SQL**: always returned $0 due to string format mismatch

Pattern: whenever a critical function falls back to a zero/empty default, add explicit observability. Empty bars → log a warning AND block execution.

---

## Failure mode #10 — LLM needs hard guards, not just prompt instructions

The SIP bars bug proved the LLM cannot be trusted to self-police on missing data. Same applies to direction filters, regime filters, conviction filters.

Pattern: tell the LLM what to do in the prompt AND enforce it in code unconditionally. The no-bar-data override, scale-out logic, and force-close per-trade check all follow this pattern.

---

## Operational lessons

- **Multiple daemon processes are catastrophic** — websocket limits, race conditions, duplicate exit attempts. Use `pkill -f trademaster.orchestrator` between stop/start.
- **Alpaca error code `42210000`** can mean "position not in book" OR "TIF not supported". Never whitelist on error code alone — check message text.
- **IEX is sufficient** for liquid tickers. Don't pay for SIP. OPRA (options) is unaffected either way.
- **Dynamic capital accounting works correctly** — baseline reset gives clean slate; per-trade sizing scales with effective capital automatically.

---

## What's still not solved

The 21% win rate is the core unsolved problem. Risk management changes (trailing stop, scale-out, time windows, exposure caps) limit damage when the strategy is wrong, but they don't make the strategy right. Standard intraday indicators (VWAP, RSI-9, EMA, MACD) are widely known and don't appear to provide statistical edge.

**Path forward considerations** (not yet committed to):
1. **Iron condors instead of directional** — selling premium has structural advantage over buying it
2. **Real options-flow data** — current indicators are price-derived; unusual options activity is a leading signal not in the system
3. **More restrictive entry criteria** — accept fewer, higher-quality setups
4. **Different LLM use** — currently using DeepSeek to make directional calls; could shift to LLM only for sanity-check / news interpretation, with mechanical rules driving entries

None of these have been chosen. The Monday after this rebuild is to validate the new risk-management architecture; the strategy question remains open.
