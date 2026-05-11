# Strategies

Each strategy must satisfy:

1. **Defined risk** — max loss known at entry. No naked options. No leverage.
2. **Cash-backed** — sufficient cash in account before order. Margin is forbidden.
3. **Backtested** on ≥2 years of historical Alpaca data before deployment to paper.
4. **Paper-traded** ≥30 days before any live capital.
5. **Documented** in this file with: rules, parameters, expected win rate, max drawdown.

---

## Options — SPY 0DTE Iron Condor

**Status:** Phase 2 build target.

**Thesis:** SPY 0DTE has the highest liquidity and tightest spreads of any options product. Iron condors profit from theta decay + IV crush when the market stays in a range.

**Entry rules (draft, to be refined via backtest):**
- Time: 9:45–10:30 AM ET (after open volatility settles)
- IV rank > 50 (high implied vol expected to revert)
- Width: ~1 standard deviation (delta ~16 on each short leg)
- Wings: $5 wide on SPY ($1 on SPX) for defined risk

**Exit rules:**
- 50% profit target on premium collected
- Stop loss: 2x premium received
- Forced close at 3:50 PM ET regardless

**Risk per trade:** Max loss = (wing width × 100) − premium collected. Cash secured = max loss.

**Cash requirement check:** `risk_manager` verifies cash ≥ max loss before order.

---

## Crypto — Trend-Follow with Regime Switch

**Status:** Phase 3 build target.

**Thesis:** Crypto trends persist on 4H/1D timeframes. In choppy regimes, grid trading on lower timeframes captures range-bound profit. Detect regime, switch strategy.

**Regime detection:**
- ATR-based: high ATR (>1.5× 30-day avg) → trend mode
- Low ATR (<0.7× 30-day avg) → grid mode

**Trend mode (high ATR):**
- Enter on EMA(20) > EMA(50) on 4H timeframe with volume confirmation
- Trailing stop at 2 ATR
- Position size: 5% of cash per asset, max 3 concurrent crypto positions

**Grid mode (low ATR):**
- 10 levels above and below current price, 0.5% apart
- Allocate 0.5% of cash per level
- Cancel and re-center when price exits the grid

**Cash requirement:** Spot only. No margin. No futures. No perps.

---

## Equity Alerts (manual trading) — VWAP Reclaim + Volume

**Status:** Phase 3 build target.

**Thesis:** Stocks reclaiming VWAP on above-average volume are momentum candidates the user can validate manually.

**Alert rules:**
- Price was below VWAP by ≥0.5% in the last 60 minutes, then crossed above
- 5-min volume > 1.5× 20-day average for that 5-min slot
- No earnings in the next 2 trading days
- Market cap > $1B (liquidity filter)

**Output:** Discord alert with ticker, current price, VWAP, volume context, and a chart link to TradingView.

**No automated execution** — user trades these manually.

---

## Pre-Market Research (daily briefing)

**Status:** Phase 1 build target.

Run by Gemini 3.1 Pro at 8 AM ET, posts to Discord `#research` channel:

- Overnight news (filtered through Alpaca news feed)
- Earnings reports released after close / pre-market
- Macro events for the day (FOMC, CPI, NFP, etc.)
- Futures action and gap analysis on watchlist tickers
- Sector rotation signals
- One-paragraph synthesis: "What does today look like, and what should TradeRouter pay attention to?"
