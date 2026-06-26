---
name: project_equities_scanner
description: "Isolated alert-only equities signal scanner (#stock-signals) — runs a stock watchlist through the existing trend engine, posts buy-call/put signals. Built+live 2026-06-26."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**ISOLATED ALERT-ONLY EQUITIES SIGNAL SCANNER — built + activated 2026-06-26 (`0016a15`, on main, 588 tests green).** A SEPARATE feature the user requested, kept fully apart from the SPY condor/trend strategies: monitors a stock watchlist with the EXISTING deterministic trend engine and posts plain-language buy-call/buy-put SIGNALS to a new Discord channel. **Signals only — NEVER executes, no positions, no shared capital/risk with the SPY strategies.**

**Watchlist (9):** META, QQQ, AMZN, MSFT, NVDA, GOOGL (user confirmed GOOGL not GOOG), SNOW, PLTR, MU. Stored `data/watchlist_equities.json` (gitignored like the SPY `data/watchlist.json`; lives on disk, NOT in git — re-seed from this list if NUC dies). Edit that file to change tickers.

**How it works:** `agents/equities/scanner.py` → per ticker each scan: `get_recent_bars(t, 5min, limit=60, warmup_days=1)` → `indicators.snapshot()` → `_ticker_market_ctx()` (local per-ticker S/R: prev day hi/lo/close + MA5/10 from daily bars, ORB + session hi/lo from today) → `signal_engine.decide(t, snap, ctx, now)` (the SAME ticker-agnostic engine — ADX[25,50), VWAP, EMA, S/R headroom). Reuses the engine WITHOUT modifying any SPY code. HIGH+MEDIUM posted (LOW/HOLD skipped), plain-language (no options jargon), in-memory dedup so the same signal doesn't repost every 15 min (posts on action/conviction CHANGE; HOLD clears).

**Wiring (all additive/isolated):** `_equities_scan_job` in `trademaster/scheduler.py` (CronTrigger mon-fri hour 9-15 minute :05/:20/:35/:50 — offset from the SPY scan's :00/:15/:30/:45), registered ONLY when `enable_equities_scanner` AND a poster is wired; `make_scheduler(..., stock_signal_poster=)`; `bot.post_stock_signal` → `#stock-signals` (config `discord_channel_stock_signals`); orchestrator passes the poster. Config flags `enable_equities_scanner` (default False) + `discord_channel_stock_signals` in `trademaster/config.py`.

**LIVE CONFIG (.env, NOT in git):** `ENABLE_EQUITIES_SCANNER=true`, `DISCORD_CHANNEL_STOCK_SIGNALS=1520182662042615982` (user's #stock-signals channel). Boot smoke-test 2026-06-26 confirmed `equities_scan` job registers + 0 errors. **GOES LIVE Mon 2026-06-29 7:45 AM ET auto-restart** → posts during RTH. Dry-run preview (read-only, no Discord): `.venv/bin/python -m scripts.equities_scan_dryrun` (verified 06-26: META→BUY_PUT MEDIUM, others HOLD).

**MISSION CONTROL "Stock Signals" SECTION (built 2026-06-26, `~/projects/mission-control` repo commit `03dff91`).** New top-level dashboard section (sidebar link 📊 + `/stock-signals` route, separate from the AI-Trading section) = a TABLE of the 9 tickers with LIVE share price + latest signal per row. Data flow: the scanner writes `data/equities_signals.json` (latest decision per ticker incl HOLDs + scan price, atomic, every scan — trading repo `9d7c8b7`, `scanner.write_signals_snapshot`, gitignored runtime data); MC reads it via `/api/stock-signals/signals` (`src/lib/stockSignals.ts` resolves `<trademaster repo>/data/equities_signals.json` via registry), and fetches LIVE prices via `/api/stock-signals/prices` (Alpaca latest-trade IEX; keys in MC `.env.local`, gitignored — user chose "reuse bot's Alpaca keys"). Component `StockSignalsTable.tsx` polls prices 15s / signals 30s. Next.js 15 build verified, live-tested end-to-end (real prices returned). **User must restart their Mission Control server to pick up the new routes + .env.local.** MC stack: Next.js 15 app-router, React 19, Tailwind v4, better-sqlite3 (reads the trading DB read-only). Keep `src/lib/stockTickers.ts` (MC) in sync with `data/watchlist_equities.json` (bot) if tickers change.

**Known minor coupling:** `decide()` reads the global `DIRECTIONAL_PUTS_ONLY` flag (currently false → both calls+puts fire); if ever set true for SPY it'd also suppress equities calls. Tests: `tests/test_equities_scanner.py` (market_ctx, plain-language/no-jargon, dedup, fail-open). See [[project_condor_vix_gate]], [[feedback_signal_jargon]] (plain-language signal rule honored).
