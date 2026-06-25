---
name: project_1min_backtest_verdict
description: "1-min trend-follow 0DTE backtest WITH costs (2026-06-23) — decisively −EV; faster timeframe is WORSE, not better. Settles the \"drop to 1-min/30s\" question."
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**SETTLED 2026-06-23: dropping the trend engine to a 1-min/30s timeframe is −EV and WORSE than slower — do not re-litigate.** User wanted to drop from the 5-min-indicator/15-min-scan setup to 1-min/30s (motivated by "theta is aggressive on 0DTE" + a YouTube scalper video). Built `scripts/backtest_trend_1min_costs.py` — a proper option-P&L sim (not just hit-rate): prices the 0DTE ATM option via Black-Scholes at entry (ASK) and exit (BID) after fixed holds, so delta gain, THETA decay, and the bid/ask SPREAD are all modeled. 31k+ signals on real SPY 1-min IEX bars, 2025-01→2026-06.

**RESULT (robust across all assumptions):** NET −$1.9 to −$12.5/contract, Sharpe −4 to −10, total −$60k to −$390k. The decomposition is the whole story:
- `gross/ct` (delta only, no theta/spread) = SMALL POSITIVE (+1.6 to +8.5) → there IS a tiny raw directional edge (what "looks good" on a chart/video).
- `+theta` (theta, ZERO spread) = NEGATIVE in every row → **theta alone erases the edge; even at zero transaction cost, buying loses.**
- `NET` (theta + spread) = deeply negative → the spread buries it ($0.01 spread = $1/ct round-trip).

**KEY (counterintuitive) FINDING: faster = WORSE.** 5-min hold had the WORST Sharpe (−10), because the spread is a FIXED per-trade cost while the move shrinks with the timeframe → spread/move ratio is worst at the fastest timeframe. Chart timeframe does NOT reduce theta (theta is wall-clock, independent of chart). So 1-min/30s amplifies spread drag and fixes nothing. Win rate 37–44% (below coin-flip). Loses even at an unrealistic penny-wide $0.01 spread + cheapest (realized-vol) IV.

This is the empirical proof of why long 0DTE directional is −EV (tiny edge < theta + spread), and directly refutes "smaller timeframe helps theta." Aligns with the deep-research "Profit Mirage" finding. Reinforces: the edge is SELLING premium (condor, theta works FOR you), not buying. See [[project_strategy_review_0618]], [[project_exit_theta_gap]], [[project_regime_dead_zone]]. Backtest caveats noted in-script: IEX 1-min ~2% volume (noisy), fixed-hold = no stop whipsaw (optimistic), realized-vol IV is a LOWER bound on real premium.
