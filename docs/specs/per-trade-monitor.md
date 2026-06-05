# Spec: Per-Trade Monitor Coroutine (Option D)

**Status:** Proposed — not yet built. Build only if the 1-min LLM exit-check
(shipped 2026-06-05, commit `8d57577`) proves too slow after a few sessions.
**Author:** drafted 2026-06-05.

## 1. Motivation

0DTE exit decisions are a seconds game. Today's exit management is two shared
APScheduler jobs that loop over *all* open positions:

- `trailing_stop_tick` — every 30s, mechanical only (peak update, scale-out,
  hard floor, trailing stop). No LLM.
- `directional_exit` — every 1 min (was 5), full LLM hold/sell decision.

Limitations this spec addresses:
- Fixed shared cadence — can't poll *faster* when a position is near its stop
  or moving fast, or *slower* when calm.
- The LLM judgment loop is capped at 1-min, and the mechanical stop at 30s.
- Trade #50 (2026-06-05) ran +195% → +242% inside a single monitoring window.

Option D gives each open trade a dedicated coroutine that polls and decides on
an **adaptive** cadence for that one position, start to finish.

## 2. Decision gate (build trigger)

Do NOT build until BOTH hold:
1. The 1-min LLM check + 30s mechanical tick + perpetual trailing stop have run
   for ≥1–2 weeks and we have evidence the cadence is still too slow (e.g.,
   give-back between checks on real trades).
2. We specifically want adaptive sub-30s polling near the stop.

The perpetual trailing stop (commit `040cd22`) already provides seconds-level
*mechanical* protection; D's marginal value is the *adaptive LLM* loop. Weigh
that against the operational complexity in §6.

## 3. Design overview

When `execute_directional_signal` fills a trade, spawn one long-lived task:

```python
task = asyncio.create_task(monitor_trade(trade_id, posters=...))
_MONITORS[trade_id] = task   # registry for supervision + dedupe
```

`monitor_trade` owns that position for its entire life:

```
async def monitor_trade(trade_id):
    async with per_trade_lock(trade_id):  # reuse the scale-out lock
      loop:
        row = reload trade from DB
        if row.closed_at is not None: return        # someone else closed it
        quote = await get_single_option_quote(occ)  # REST (no OPRA streaming)
        if quote is corrupt/stale: sleep(short); continue   # don't act on garbage

        # --- mechanical (every cycle, fast, no LLM) ---
        ratchet trailing stop; maybe scale-out; check hard floor / trailing
        if mechanical exit fired: close, post, return
        if past 15:45 ET and expiry==today: force-close, post, return

        # --- LLM judgment (sub-cadence / gated) ---
        if due_for_llm(pnl, last_llm_at) or reversal_rule_fired:
            decision = await llm_exit_confirm(...)
            if EXIT: close, post, return

        sleep(adaptive_interval(pnl, stop_distance, volatility))
```

### Adaptive cadence
- **Mechanical loop:** base 3–5s.
- **LLM decision:** gated — at most every ~15–20s, OR immediately when an
  indicator reversal rule fires or P&L crosses a threshold. (LLM latency is
  1–8s; never block the mechanical loop on it — run it on its own sub-timer or
  in a separate awaited step that doesn't starve the stop check.)
- Tighten the loop (→ ~2–3s) when: bid within ~10% of the stop, recent
  volatility high, or inside the final 15 min before force-close.
- Loosen (→ ~10–15s) when calm and far from the stop.

## 4. Single-owner principle

For directional trades, the coroutine becomes the **sole** manager. Retire the
shared `directional_exit` and `trailing_stop_tick` jobs **for directional**
(iron-condor keeps its own jobs). This avoids double-management and the
scale-out race we fixed in `1ab457e`. The per-trade `asyncio.Lock`
(`_scale_out_locks`) is retained as defense in depth.

## 5. The three non-negotiable safeguards

A scheduled-job loop is self-healing; a coroutine is not. These are mandatory:

1. **Startup recovery.** On daemon start (orchestrator), query all open
   directional trades and spawn a monitor for each. WITHOUT this, a restart
   leaves live positions with no stop. This is the single most dangerous
   failure mode D introduces — test it explicitly.

2. **Supervision / reconciler.** A periodic task (every ~1 min) that checks:
   for every open directional trade, is there a *live* monitor in `_MONITORS`?
   If a monitor task is done/crashed while its trade is still open → log loudly
   and re-spawn. Also attach a done-callback to each monitor that logs
   exceptions (never let a monitor die silently).

3. **Bounded teardown.** When a trade closes (any path), the monitor removes
   itself from `_MONITORS` and returns. The reconciler also reaps stale entries.

## 6. Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Daemon restart kills monitors | Startup recovery (§5.1) re-spawns from DB |
| Monitor crashes (unhandled exc) | done-callback logs; reconciler re-spawns (§5.2) |
| Missed spawn at entry | reconciler catches any open trade without a monitor |
| Stale/garbage quote (indicative feed) | skip cycle, don't act; reuse the tick's corrupt-quote guard (`ask > 5×bid`) |
| Two actors on one trade | single-owner (§4) + per-trade lock |
| LLM latency starves mechanical loop | mechanical and LLM on separate cadences; never block the stop check on the LLM |
| Quote-poll rate limits | base ≥3s; ≤4 concurrent monitors (SPY-only, 4 trades/day) → well within limits |

## 7. Data-feed constraint

No OPRA → **no option-quote streaming**. The monitor REST-polls
`get_single_option_quote` (same source the 30s tick uses), just faster and
per-position. True event-driven option streaming is infeasible without OPRA;
the closest alternative is streaming the SPY underlying and REST-fetching the
option on significant moves (a variant that could feed `due_for_llm`).

## 8. Reuse (don't duplicate exit logic)

Refactor the per-trade body out of `run_trailing_stop_tick` /
`run_directional_exit_monitor` into a shared `manage_open_trade(trade, quote,
...)` that both the (interim) jobs and the monitor call. The monitor must reuse
`_maybe_ratchet_trailing_stop`, `_maybe_scale_out`, `_check_exit_rules`,
`_llm_exit_confirm`, `_close_trade_row`, `format_scale_out`,
`_format_exit_combined` — no parallel implementations.

## 9. Phased rollout

1. **Phase 0** (done): 1-min LLM (`8d57577`) + 30s mechanical + perpetual stop.
2. **Phase 1:** build `monitor_trade` + registry + per-trade lock; run it
   **alongside** the shared jobs in shadow mode (log decisions, don't act) to
   validate cadence/decisions vs the live jobs.
3. **Phase 2:** flip the monitor to authoritative for directional; add startup
   recovery + reconciler; retire the shared directional jobs.
4. **Phase 3:** add adaptive cadence + (optional) SPY-stream-triggered LLM.

## 10. Testing

- Startup recovery spawns a monitor per open trade (integration).
- Monitor exits cleanly when trade closes; deregisters.
- Crashed monitor → reconciler re-spawns.
- Corrupt/stale quote → no action, loop continues.
- Mechanical exit (hard floor / trailing stop) fires without an LLM call.
- LLM EXIT closes; LLM HOLD keeps riding.
- Force-close at 15:45 ET.
- No double scale-out under concurrent monitor + (transitional) shared job.

## 11. Rollback

Monitor spawning is feature-flagged (`settings.enable_per_trade_monitor`,
default False). Flip off → fall back to the shared jobs (which stay in the code
until Phase 2 retires them). No schema changes, so rollback is config-only.

## 12. Open decisions

- Adaptive-cadence exact thresholds (stop-distance %, volatility measure).
- Whether to keep a slow shared mechanical tick as a redundant backstop even in
  single-owner mode (belt-and-suspenders vs. clean ownership).
- LLM call budget per position per minute (cost is trivial; latency/consistency
  is the real limit).
