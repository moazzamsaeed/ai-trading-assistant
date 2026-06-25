---
name: broker-options-ceiling-alpaca-level3
description: Alpaca caps at options Level 3 (defined-risk spreads) — no naked/uncovered options. Blocks the validated naked short-strangle VRP strategy on the current broker.
metadata: 
  node_type: memory
  type: project
  originSessionId: 9ccabac8-4145-4996-9003-8761355bfe4e
---

**CRITICAL BROKER CONSTRAINT (verified 2026-06-19 against the live account object):** The Alpaca account **cannot trade naked/uncovered options**, which blocks the gate-passing **naked short-strangle** VRP strategy (see [[strategy-rethink-go-live-hold-2026-06-18]]).

Checked via `ac._trading_client().get_account()`:
- `options_approved_level = 3`, `options_trading_level = 3` — **Level 3 is Alpaca's CEILING for everyone** (L1=covered calls/cash-secured puts, L2=long options, **L3=defined-risk multi-leg spreads**). Alpaca does **NOT** offer Level 4 / uncovered (naked) options to any retail account.
- `multiplier = 1` (CASH account, no margin), `shorting_enabled = False`, buying_power=cash=equity (~$98k paper).
- Only a **PAPER** key is configured (`PK…` prefix; live keys are `AK…`). No live account wired up — so "check live" is moot, AND Alpaca's L3 ceiling applies to live too.

**Implication:** the short strangle (naked, undefined risk, needs margin + L4) is **NOT deployable on Alpaca**. Two real paths (decision pending):
1. **Defined-risk workaround** = wide iron condor (strangle + far-OTM wings), Level-3-executable here — BUT re-introduces the 4-leg execution cost that FAILED the condor's cost-robustness test. MUST re-run `backtest_strangle.py`-style gate WITH wings before trusting it; likely lands back in the condor's marginal zone.
2. **Switch broker** (Tastytrade / IBKR / Schwab — Level 4 + margin) = only way to run the actual validated naked strangle. Real account + API infra work.

Don't re-propose deploying the naked strangle on Alpaca — the broker ceiling, not account approval, is the blocker.

**✅ RESOLVED 2026-06-19 — WIDE IRON CONDOR is the deployable winner; NO broker switch needed.** `scripts/backtest_wide_condor.py` = the gate-passing strangle recipe (k=0.5, ADX<25 filter, real VIX1D, intraday stop) + far-OTM long wings → 4-leg defined-risk (`OrderClass.MLEG`), Alpaca-Level-3-executable. Expected it to re-break on 4-leg cost (orig condor `backtest_vrp.py` was DSR 56% realistic) but it **PASSES at all costs: DSR 100%/99.9%/97.7% (opt/real/cons), OOS Sharpe +3.10, 76% win.** Why it works when the orig condor failed: the ADX regime filter + intraday stop the orig LACKED — **leg count was never the real problem; trading indiscriminately + holding to the wing was.** Walk-forward picks NARROW $5 wings → **tail CAPPED: worst −104% of risk (vs naked −558%), maxDD −6%@3% (vs −22%).** STRICTLY BETTER than naked for our case: deployable on Alpaca NOW (no naked approval, no switch, ~$300 defined risk/condor so works on a few $k, no $14-25k floor), capped/known tail, higher risk-adj return. **DECISION: build the WIDE CONDOR on Alpaca, not the naked strangle.** Naked strangle = theoretical ceiling for a future broker move only. Remaining unknown (both): real multi-leg fills → paper then tiny real. Full detail in `docs/STRANGLE_STRATEGY_DESIGN.md` §4d.

**CONDOR ECONOMICS + CAPITAL (doc §4e, scripts `condor_vs_actual.py` + analysis):** avg credit $164/lot, defined max-loss (risk) ~$336/lot, worst trade −104% of risk (wing-capped), 74% win, avg +11% of risk/trade full-sample (+17% on the 2026 calm subset). Capital: bare min to hold 1 lot ≈ $336 (no $14-25k naked floor). Fixed-fraction modeled (2023-2026): 3%→+64% CAGR/−6% maxDD, 5%→+126%/−11%, 10%→+385%/−20%/−10.4% worst day (10% over-bets a neg-skew edge; the +23,090% compounded total is a sequence-risk illusion — DON'T bank it). Sizing guidance: 3% default, 5% aggressive ceiling, 10% only after real-fill data. **On $10k @5%: ~$8.4k/yr MODELED, realistic ~$3-5k/yr after fill/regime discount. To hit the user's GOAL of ~$2k/mo ($24k/yr) avg year-on-year: conservative capital ≈ $60k (~40%/yr realistic), $75-80k for buffer. $10-15k is far too little for that goal. Deploy STAGED: prove real yield on $10-15k → scale to capital that hits target at the PROVEN rate.**

**TRADE FREQUENCY:** condor trades ~57% of days (calm: ADX<25 & VIX1D<40), sits out ~43% (almost all the ADX/trend filter; VIX1D crisis filter only 5 days in 3.5yr). Sit-outs CLUSTER — longest ~93 trading days (~19 weeks). Income is lumpy with multi-month dry spells; the downtime is AVOIDED RISK, not lost income.

**TREND-DAY SLEEVE (experimental, doc §4f, `backtest_trend_sleeve.py`) — user wanted a strategy for the idle volatile days.** Must be a defensive premium SELLER (buying = −EV, §4c). Wider defensive condor (k=1.0, half size) on ADX≥25 days + user's weekly rules (BIG LOSS→park week, BIG WIN→halt week). Modeled on $60k: cuts downtime 57%→97%, is +EV (+2.5%/trade, 78% win) BUT adds only +$316/mo (+7.5%) — trend edge ~4× thinner than calm (+2.5% vs +11%); calm condor stays ~93% of income. Park/halt rules barely fire (wings already cap loss). 🚩 COST-FRAGILE: nets ~$10/condor vs calm ~$37, volatile days = wider spreads → real fills likely push to ~0/neg. **VERDICT: tiny experimental sleeve to measure live, NOT a second engine. Don't plan capital around it. Downtime = avoided risk not lost income.** Don't re-propose a long/buying trend leg (−EV, §4c).

**BROKER ALTERNATIVES VALIDATED via web research 2026-06-19 — all three support API trading AND naked options:**
- **Tastytrade** (best fit): Open API, **retail self-service OAuth** (account mgmt → API dropdown), free, supports equity options + multi-leg; short strangle/straddle is a first-class documented order type — platform is built for premium sellers. The natural choice.
- **Interactive Brokers**: TWS API + Client Portal/Web API; **Level 4 = all strategies incl. naked**; requires experience/net-worth assessment, **~$25k+ min**, age 21+. Most powerful API, steepest to integrate.
- **Charles Schwab**: Trader API (ex-TDA, now GA; register app on developer.schwab.com); supports multi-leg options; **Schwab "Level 3" = uncovered/naked** (Schwab uses 0–3 scale). Needs app registration/approval + "substantial funds".
- **Recurring ~$25k minimum** for naked/Level-4 approval (IBKR explicit, Schwab "substantial funds") — matches our sizing math (~$14k margin/lot + buffer). $5k/$15k won't get naked approval anywhere.
- **Cost = integration work, not feasibility**: whole codebase is on `alpaca-py` (`integrations/alpaca_client.py`); a switch needs a parallel broker-client module (orders, account/positions, chains, stop-monitor). Strategy logic (`decide()`) stays broker-agnostic.
