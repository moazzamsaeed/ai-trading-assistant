# High-Risk Short-Strangle Strategy — Design & Validation

**Date:** 2026-06-18
**Status:** Backtested through the gate — **PASSES, and is cost-robust where the iron condor was not.** Not yet implemented (no live/paper wiring). This is the design + evidence artifact.
**Supersedes nothing; extends** `docs/VRP_STRATEGY_DESIGN.md` (the defined-risk iron condor) with the high-risk variant the 06-18 strategy review asked for.

---

## 1. The core insight (why we pivoted to selling premium)

Our trend/LLM strategies **bought** options → paid the vol-risk-premium (VRP) **and** had to predict **direction** (proven coin-flip; trend engine OOS Sharpe −0.25, DSR 1.3%, formally rejected). The iron condor **sold** options → collected the VRP, needed **no** direction (profit from theta + IV>realized). The market structurally favors the seller. **We were on the wrong side of the trade.**

The condor had a **real, generalizing** edge — OOS Sharpe **+3.18**, DSR **99.8%**, only 0.36 IS→OOS decay (the first strategy to clear the hard part of the gate). But its edge **died on 4-leg execution cost**: optimistic +3.18/99.8% → realistic +2.04/56% → conservative +1.01/0.1% → pessimistic −0.73. It crossed the DSR>95% bar *inside* the plausible cost band. That was an **execution-cost problem, not an absence of edge.**

**The highest-leverage fix to an execution-cost problem is FEWER LEGS.**

## 2. The strategy

Deterministic, LLM-free SPY **0DTE short strangle** (sell OTM put + OTM call, **no wings**):

| Element | Rule |
|---|---|
| **Entry** | Once/day, ~10:00 ET. Sell short put @ `spot − k·EM`, short call @ `spot + k·EM`, where `EM = spot · VIX1D · √T` (real Cboe 1-day implied vol). `k=0` ⇒ ATM **straddle** (max credit, tighter zone); `k>0` ⇒ wider OTM strangle. **Backtest picks k=0.5.** |
| **Credit** | Full premium of both short legs — **no premium paid away for wings** (that's the condor's tax we're removing). |
| **Tail defense** | **MECHANICAL STOP** instead of wings: mark the strangle each 15-min bar; if buy-back cost − credit ≥ `stop_mult · credit`, **stop out**. **Backtest picks stop_mult=1.5×.** This is the high-risk-tolerance trade: *self-insure* the tail with a stop + sizing rather than buying wings. |
| **Regime filter** | Sell only when **prior-day Wilder ADX < adx_max** (range-bound) **AND VIX1D < vmax**. **Backtest picks adx_max=25, vmax=40.** This is where our trend/ADX work finally earns its keep — as a *vol-regime filter*, not a direction bet. Stand aside in trends / vol spikes, where realized vol blows up a short. |
| **Exit (no stop hit)** | Hold to expiry, settle at SPY close intrinsic. |
| **Sizing / risk** | Nominal max loss = `stop_mult · credit`. Risk a fixed fraction of capital per trade (backtest survivability run at 2/3/5%). |

**Winning config (most-picked by walk-forward, robust across all cost levels):**
`k=0.5 strangle · stop 1.5× credit · ADX<25 · VIX1D<40`.

## 3. Validation — through the same gate that PASSED the condor and FAILED the trend engine

`scripts/backtest_strangle.py` — real VIX1D pricing, **intraday stop modeling** on 15-min bars (the condor template settled close-only; the strangle's defining feature is the stop, so it had to be modeled), daily Wilder-ADX regime filter, embargoed walk-forward (252/5/21) + Deflated Sharpe + survivability.

**Headline: 866 SPY days (2023-01-03 → 2026-06-17), 36 configs, 29 folds, ~393–428 OOS trades, ~75% win.**

### Cost sensitivity — the deciding test (this is what killed the condor)

| Per-leg spread | OOS Sharpe-ish | DSR | Win | Verdict |
|---|---|---|---|---|
| $0.02 (optimistic) | strong | **99.5%** | 77% | PASS ✅ |
| $0.04 (realistic) | strong | **99.9%** | 75% | PASS ✅ |
| $0.06 (conservative) | solid | **99.8%** | 73% | PASS ✅ |
| $0.10 (pessimistic) | positive | **98.6%** | 71% | PASS ✅ |

**The strangle clears DSR>95% at EVERY cost level, including pessimistic.** The condor crossed that bar *inside* the band and went negative at pessimistic. **The execution-cost fragility that killed the condor is solved by halving the legs** (~3 crossings vs ~6). This is the whole point of the design, and it held up.

### Survivability (high-risk by construction)

At realistic cost, 3%/trade sizing: total **+526%** over the sample, **maxDD −22%**. Survives the gate's −35% maxDD bar.

## 4. The load-bearing caveat (be honest about what could still break it)

For the condor, the load-bearing assumption was **fill price**. For the strangle it shifts to **stop execution under vol expansion**:

- **The stop is modeled with CONSTANT entry vol.** In reality, when SPY moves toward the short strikes, IV spikes (vol-up on down-moves). So the real buy-back mark is **higher** than the constant-vol model → stops trigger **earlier and at worse prices**, and the gap-tail is **worse** than modeled. **This understates the tail.**
- **15-min stop grid** can't catch within-bar spikes; **worst modeled trade was −558% of nominal risk** (a single bar gapped clean through the 1.5× stop). At 3% sizing that's a **~−17% single-day account hit**. This is a high-risk strategy — *as requested* — and it can have a brutal day.
- Flat-IV/no-skew BS pricing; 2023–2026 sample has **no 2020-style crash**; $5k is too small (wants ~$12–15k for sane 3% sizing on one strangle).

**So: the edge is real and cost-robust. The remaining unknown is whether the mechanical stop fills as cleanly as modeled when vol expands.** That is exactly what paper-then-tiny-real measures.

## 5. Build plan (not started)

1. **Paper-test to MEASURE real stop fills** under live vol — the one assumption the backtest can't prove. Reuse the deterministic-engine pattern (pure `decide()` + rules exit) already shipped for directional.
2. Determinize a strangle executor (2-leg sell + the intraday stop-monitor loop). The disabled `agents/options/` iron-condor infra is the nearest starting point.
3. Re-run the gate on **real paper fills** (especially stop fills) before any real capital.
4. Only on PASS-with-real-fills: tiny real size. Capital is not the lever — expectancy is.

## 6. How this fits the bar set on 2026-06-18

The gate's bar for ANY strategy: **positive OOS Sharpe after costs AND DSR>95% AND survives the worst vol fold.** The trend engine failed it. The condor passed the statistics but failed the cost-robustness sub-test. **The strangle is the first to pass the statistics AND the cost-robustness test** — with the honest remaining caveat being stop execution, not edge.
