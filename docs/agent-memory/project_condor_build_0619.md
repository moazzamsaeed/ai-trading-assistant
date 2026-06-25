---
name: condor-build-monday-paper-0619
description: Deterministic VRP iron-condor + S/R trend engine BUILT 2026-06-19 for Monday 06-22 paper week at $10k. Code complete + unit-tested; live MLEG/chain validation pending Monday pre-open.
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**BUILD STATE (2026-06-19) — condor + S/R trend engine for Monday 2026-06-22 paper, $10k.** User chose: full build for Monday, derive VIX1D from Alpaca chain, add S/R to the deterministic trend engine AND build the wide condor. LLM directional path left PARKED (come back later with a different strategy). See [[broker-options-ceiling-alpaca-level3]] (why condor not naked strangle) + [[strategy-rethink-go-live-hold-2026-06-18]].

**SHIPPED (committed, full suite 573 green; NOT pushed):**
- `agents/options/condor_engine.py` (`9b36924`) — pure `decide_condor()` (regime filter ADX<25 & VIX1D<40, strikes at 0.5× VIX1D expected move, $5 wings) mirroring `scripts/backtest_wide_condor.py`. `vix1d_from_chain()` derives 1-day IV from the 0DTE ATM chain (quoted IV → straddle-inversion fallback) — solves the no-live-VIX1D blocker. `stop_breached()` = 1.5× stop. 11 tests.
- `agents/directional/signal_engine.py` → **trend_follow_v2** (`3bc935c`) — S/R-aware: blocks BUY_CALL into overhead resistance / BUY_PUT into support <0.15% ahead; caps HIGH→MEDIUM when room <0.30%. Levels from market_ctx (prior-day hi/lo/close, MA5/10, ORB, session hi/lo). Fail-open (no ctx → v1 behavior). 20 tests.
- `agents/options/strategist.py` `run_deterministic_condor()` (`47d5b21`) — LLM-free: prior-day Wilder ADX (get_daily_bars + indicators.adx, today excluded) + live VIX1D → decide_condor → `build_condor_at_strikes()` (new, in strategies/spy_0dte_iron_condor.py) → risk-validate → execute via existing MLEG `submit_iron_condor_entry`. Reuses `_enrich_chain_with_bs_greeks` (indicative feed has no greeks → BS-inverted). 2 wiring tests.
- `agents/options/exit_monitor.py` (`9adfc99`) — exit = **1.5× stop + 15:50 force-close, NO profit target** (matches backtest; a 50% PT would cap full-credit expiries and degrade the edge).
- `trademaster/scheduler.py` (`000fc80`) — `_iron_condor_entry_job` dispatches to run_deterministic_condor when DETERMINISTIC_ENGINE=true. Entry moved 9:45→**10:00 ET** (matches backtest 360-min strike calc). Exit 5-min sweep + 15:50 force-close.
- **`.env` (on disk, NOT in git — gitignored, contains secrets; do NOT commit it):** TRADING_CAPITAL_USD=10000, BASELINE_RESET_AT=2026-06-22 (clean $10k), ENABLE_IRON_CONDOR=true, DETERMINISTIC_ENGINE=true. So BOTH run Monday: condor (sells premium on calm days) + S/R trend engine (buys calls/puts on trend days) — share the $10k + risk caps. qty=1 condor (~$336 risk ≈ 3.4%).

**✅ TASK 5 DONE — LIVE VALIDATION PASSED (2026-06-22 ~11:17 ET, market open).** Ran `scripts.condor_preopen_check --submit`. Results: SPY $744; prior-day ADX **26.83 populated** (bug fix holds during market hours); chain 80 contracts, native IV **0/80** (indicative feed has none, as expected) → **BS-enrich filled 72/80** ✅; derived **VIX1D 22.0** (plausible); engine **HOLD** (ADX 26.8 ≥ 25 → reads today as trending, stands aside — correct, would've HELD at 10:00 entry too, so no condor placed today = expected, not a miss). MLEG: account **ACCEPTED + FILLED** a 4-leg defined-risk order → **Level-3 execution CONFIRMED live**.
- ⚠️ **LESSON — paper sim fills MLEG regardless of marketability.** The probe's "3× credit = non-marketable, will cancel" assumption was WRONG: the order filled instantly and stranded a live 0DTE condor. Closed it manually via `submit_iron_condor_close` (account back flat). **FIXED** the script (`828a3ae`, pushed to PR branch): probe now polls status after submit → if filled, auto-closes; only cancels if it rested; reports "flat after probe?". Re-ran: submit→fill→auto-close→flat ✅. Account confirmed 0 open positions.
- **Whole condor+trend build is now live-validated end to end.** PR #1 (`feat/vrp-condor-trend-engine`) holds it; `main` clean. Remaining: let it run the paper week + read real condor fills on the first ACTUAL calm day (ADX<25) — none yet on 06-22.

**🐛 BUG FOUND+FIXED by the dry-run (`integrations/alpaca_client.py`):** `get_daily_bars()` issued a bare `limit=40` request with NO start date → IEX returns EMPTY (esp. outside market hours) → prior-day ADX was None → condor would HOLD forever and never trade. Anchored an explicit lookback window (~2× sessions in calendar days). Now returns 40 bars, ADX computes. Also fixed prev-close/MA daily features sharing the bug. **Worth a Monday-live sanity check that ADX still populates during market hours.**

**HONEST FRAMING (unchanged):** condor = the real edge (paper to MEASURE real multi-leg fills, the one thing backtests can't settle). S/R trend engine = experiment; sharper entries but still BUYING premium = −EV on 0DTE (proven), so it's for paper data/architecture, NOT profit. Read condor paper fills as the go/no-go signal; don't read trend-engine P&L as edge. Capital to actually hit the user's ~$2k/mo goal ≈ $60k conservative (not $10k — $10k is the validation account).
