# VRP Strategy Design — Defined-Risk Premium Selling on SPY 0DTE

Status: **design / proposal** (not implemented). The one direction the evidence
still points toward after the trend-following engine formally failed the
validation gate (IS Sharpe +1.11 → OOS −0.25, DSR 1.3%, see
`docs/STRATEGY_REVIEW_2026-06-18.md`). This must clear the *same* gate
(`scripts/validate_strategy.py`) before any real capital.

---

## 1. The thesis — be the house, not the gambler
Every test this project ran showed that **buying** 0DTE premium loses to the
volatility risk premium (VRP): implied vol is persistently richer than realized,
so long-option holders overpay theta + the vol premium. We proved it directly —
our long-option backtests went negative *because* we were paying the VRP, and the
0DTE-pricing model flipped negative exactly as IV rose toward realistic levels.

**The symmetric, evidence-backed move is to SELL that premium.** This is a real
strategy that funds run; it's positive-carry; and it's the structural other side
of the exact trade we kept losing.

## 2. Structure — defined risk, NON-NEGOTIABLE
- **Iron condor** on SPY 0DTE: sell an OTM put spread + an OTM call spread.
  Collect a net credit; **max loss = spread width − credit** (capped both sides);
  profit if SPY closes between the short strikes.
- **Naked short 0DTE is forbidden** — a single 2% move = account ruin. The long
  wings cap each side. Defined risk is the whole point.
- **We already have iron-condor infrastructure** (`agents/options/`, currently
  `enable_iron_condor=False`, plus `backtests/cli` with a GBM + Black-Scholes
  synthetic engine). The plan revives and **determinizes** it (it was an LLM
  strategist) — fits platform-first, reuses the executor/risk-manager/gate.

## 3. Concrete deterministic rules (first cut — all to be VALIDATED, not assumed)
- **Entry:** once/day, after the opening range settles (~10:00–10:30 ET), so the
  day's expected move is established. Optional VRP filter: only enter when implied
  expected move is rich vs recent realized.
- **Strikes:** short strikes at ≈ 1 expected-move from spot (≈ delta 0.15–0.20);
  long wings a fixed width further out to define risk. E.g. SPY ~745, 3-wide:
  sell 740P / buy 737P, sell 750C / buy 753C.
- **Sizing:** risk/trade = (width − credit) × 100 × contracts ≤ a hard per-trade
  cap (start ~$250 of $5k). Tail risk is the killer, so size for the worst case.
- **Management / exit (deterministic):** take profit at ~50% of max credit; cut
  the tested side if a short strike is breached (or loss hits ~1.5–2× credit);
  else hold to 0DTE expiry (settles at intrinsic). All thresholds become gate-
  validated parameters, not hand-tuned knobs.
- **Regime filter (to test, not assume):** behavior around event days
  (FOMC/CPI) and high-VIX regimes — either skip (avoid fat realized moves) or
  lean in with smaller size (richer premium). Let the gate decide.

## 4. The edge profile — and why the gate matters MORE here
- **Positive expectancy IF IV > realized** (the VRP) — empirically true for SPY
  short-dated most of the time.
- **NEGATIVE skew:** many small wins (keep the credit), occasional max-width
  losses (breakout days). The *opposite* of the long-option lottery we tested.
- **This is the trap:** a plain Sharpe FLATTERS negative-skew strategies — smooth
  gains, rare cliffs — so it looks great right up until a vol spike. The Deflated
  Sharpe's **skew/kurtosis correction** + **walk-forward through the actual vol
  spikes** in 2023–2026 (e.g. any tariff/rate selloffs) is exactly what catches
  this. A premium seller's entire risk is the tail; the gate must stress it.

## 5. The hard part — validating it (the data challenge)
- We have **no historical OPRA/option data and no VIX feed** (Alpaca returns 0
  option bars; `get_recent_bars("VIX")` fails). So a backtest must **model**
  option prices: price the entry credit with Black-Scholes, settle at realized
  intrinsic from real SPY closes.
- **The crux is the entry implied vol** — the VRP only shows up if entry IV >
  realized. Three options, in order of honesty:
  1. **Get VIX1D / VIX** (1-day VIX is the real 0DTE implied) — best; need a data
     source (Cboe/another vendor) since Alpaca lacks it.
  2. **Proxy** IV = realized-vol × VRP-markup, and run **sensitivity on the
     markup** (like we did IV-sensitivity on the long side). Honest but the markup
     is the load-bearing assumption.
  3. Any source that gives the implied expected move directly.
- **Resolve the IV-data question first**, then run through `validate_strategy.py`:
  positive **OOS Sharpe after realistic 4-leg costs** + **DSR > 95%**, AND a
  dedicated **worst-fold tail test** (does the max-drawdown fold avoid ruin?).

## 6. The honest caveat
This is the *most promising* remaining direction — but **not a guaranteed
winner.** The research found **no evidence that VRP harvesting delivers a durable
RETAIL edge after slippage**, and an iron condor crosses the bid/ask on **4 legs**
round-trip — heavy cost drag for a tiny account. So this earns the same skepticism
as everything else: **it must pass the gate.** If it fails like the trend
strategy did, we stop. We deploy a *validated* edge or none.

## 7. Build plan
1. **Resolve IV data** (VIX1D/VIX source, or commit to the realized×markup proxy
   with sensitivity).
2. **Deterministic IC backtest** — model entry credit (BS at IV), 4-leg costs
   (spread + slippage per leg), settle at intrinsic; parameterize strikes/width/
   profit-take/stop/entry-time as the search grid.
3. **Run through `validate_strategy.py`** (extended for the condor payoff) —
   walk-forward OOS Sharpe + DSR + tail-fold test.
4. **Only on PASS:** revive + determinize `agents/options/` behind the
   `enable_iron_condor` flag, wire to executor/risk-manager, paper-test for
   architecture, then a tiny real-fill gate.
