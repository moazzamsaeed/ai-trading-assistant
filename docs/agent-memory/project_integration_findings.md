---
name: Live-API integration gotchas
description: Bugs found when first running against real Alpaca/Discord/Gemini APIs that aren't obvious from SDK docs
type: project
originSessionId: 957d0221-caf9-44af-930a-3e1e3bb9202b
---
These all caused real outages or wrong behavior on first live runs. The fixes are committed but worth remembering for similar code in new agents.

**Why:** Future-me may write new SDK wrappers (e.g., when wiring crypto, equity bars, or live order types). These patterns will resurface.

**How to apply:** When adding any new alpaca-py call, audit for enum stringification and sign conventions before assuming the SDK returns flat strings/decimals.

## 1. alpaca-py returns enums, not strings
`TradeAccount.status`, `Order.status`, `TradeAccount.multiplier` (etc.) come back as `Enum` subclasses. `str(enum)` produces `"AccountStatus.ACTIVE"`, not `"ACTIVE"`. Cost us an hour of head-scratching.

Fix pattern lives in `integrations/alpaca_client._enum_str(v)` — call it on every enum-shaped field. Use it in any new `_to_*` model converter.

## 2. discord.py commands.Bot has its own `_ready` attribute
Subclassing `commands.Bot` and adding `self._ready = asyncio.Event()` in `__init__` silently SHADOWS the Bot's internal one. Bot.start() then reassigns its internal _ready, and your code's `await self._ready.wait()` waits on a stale dead reference forever.

Fix: name your event something else. We use `self._app_ready` in `integrations/discord_bot.TradeMasterBot`. Don't use `_ready` for anything in a `commands.Bot` subclass.

## 3. Alpaca's options data feeds give no greeks at our tier
Both `OptionsFeed.OPRA` (requires signed paid agreement) and `OptionsFeed.INDICATIVE` (default) return `greeks=None`, `implied_volatility=None` for every contract on SPY 0DTE. We do not have OPRA agreement (re-confirmed 2026-06-04: `OptionChainRequest(feed=OPRA)` → `APIError: "OPRA agreement is not signed"`). **But the indicative feed DOES return live bid+ask** on near-the-money strikes (20/20 mid-session) — sufficient for entry/exit; only greeks are missing. Don't blame the feed for "missing quotes" without probing it first (see #11).

Fix: compute greeks ourselves via Black-Scholes inversion. `trademaster/options_math.py.delta_from_market_mid()` solves IV from mid price (bisection, 60 iter) then derives delta. Called from `agents/options/strategist._enrich_chain_with_bs_greeks` after the chain fetch. D-017.

## 4. Credit fills come back with negative per-share price
For a credit spread (net seller), Alpaca returns `filled_avg_price = -0.18` (means $0.18 credit/share = $18/contract credit). Naive code multiplies by 100 and records `entry_price = -$18` — totally wrong.

Fix: `abs(filled_avg_price * 100)` in `agents/options/executor._net_credit_per_contract_at_fill()` and in `agents/options/exit_monitor` close fills. Apply to any new credit-spread code path.

## 5. Alpaca paper account defaults to margin (multiplier=4)
Cash-only risk-manager check (D-001) refuses anything other than `multiplier=1`. Paper accounts ship as margin by default; user must reset paper account via Alpaca dashboard → "Reset Paper Account" → choose Cash.

We can't fix this in code — it's an Alpaca account config. Just document it (already noted in RUNBOOK). If the integration test fails on multiplier check, that's the cause.

## 6. Gemini 3.1 Pro Preview is unreliable
Chronically returns 503 "high demand" even on tiny prompts. D-016 swapped pre-market research to `gemini-2.5-pro` (stable). If anyone proposes "use the newest Gemini model", verify it's not in preview status first by hitting it 3-5 times.

## 8. StockBarsRequest defaults to SIP feed (paid) — must specify IEX explicitly
`StockBarsRequest(...)` without `feed=DataFeed.IEX` hits the SIP consolidated tape endpoint. If the account lacks SIP subscription, it silently returns `{"message":"subscription does not permit querying recent SIP data"}`. The code catches this as a generic exception → `bars = []` → the LLM gets empty indicator data and HALLUCINATES bullish/bearish setups. No crash, no alert — just wrong decisions.

Fix: always pass `feed=DataFeed.IEX` on `StockBarsRequest`. Already applied in `integrations/alpaca_client.get_recent_bars()`. For any new bar fetch code, do the same.

The IEX websocket stream uses `/v2/iex` explicitly and was always correct. Only the REST historical endpoint had this problem.

## 9. Alpaca does NOT support IOC for option orders
`MarketOrderRequest(time_in_force=TimeInForce.IOC)` on an option sell returns error code `42210000` with message "order_time_in_force provided not supported for options trading". The IOC fill-or-kill pattern that works for equity trades silently fails for options.

Fix: use `TimeInForce.DAY` for option sells. Market+DAY fills immediately at best bid during RTH (same intent as IOC for liquid options); auto-cancels at 4 PM if not filled. Applied in `integrations/alpaca_client.submit_single_option_sell()`.

Side effect: error code 42210000 is used for BOTH "position not in broker" AND "IOC not supported". Auto-close whitelist in `agents/directional/exit_monitor.py` now checks the error message text, not just the code, to avoid auto-closing DB rows when the position is still live.

## 10. SQLite + DateTime(timezone=True) loses tzinfo on read
SQLAlchemy 2.0 with SQLite drops tzinfo on read, even with `DateTime(timezone=True)`. Comparing a stored `expires_at` with `datetime.now(UTC)` raises `TypeError: can't compare offset-naive and offset-aware`.

Fix pattern: helper `_as_aware_utc(dt)` that re-adds `tzinfo=UTC` if missing. Used in `trademaster/pending_orders` and the executor's expiry check. Apply to any other DateTime(timezone=True) column we add.

## 11. `no_affordable_strike` / `spread_filtered_count=0` means WRONG STRIKES SEARCHED, not missing quotes
Discovered 2026-06-04 (KB incident I8). Every BUY_PUT since the rebuild failed at execution with `directional_no_qualifying_strike` (`spread_filtered_count=0`). First diagnosis blamed the indicative feed ("no asks") and shipped a chain-retry mitigation — which did nothing. **Actual cause: `select_best_strike` searched the wrong strike range for puts** (`[target-30, target-10]`, entirely $10-30 OTM), landing only on deep-OTM 0DTE strikes priced under the $0.30 `MIN_ASK` floor. The ATM put the LLM wanted (~$0.40+) was never in the requested range. Calls were fine because their range `[target-10, target+30]` includes ATM. Fixed by direction-aware offsets (put `[target-30, target+10]`), commit `f2d2170`.

**Lesson:** this is the INVERSE of the silent-failure reflex (#3/#8/I4/I7). Here the data was fine and the bug was ours. Before blaming a feed for "no quotes," probe it directly (request the chain, count strikes with asks) AND check what strike range the code actually requested. `spread_filtered_count=0` specifically means "nothing passed the budget+MIN_ASK pre-filter" → usually the searched strikes were too cheap/illiquid, not absent.
