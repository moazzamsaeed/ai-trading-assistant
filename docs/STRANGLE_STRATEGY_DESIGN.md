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

**WHERE the losses actually come from — the filter is a TREND filter, not a VOL filter.** The regime filter screens on *prior-day* ADX (was yesterday trending?), so it cannot see *today's* intraday behavior in advance. It correctly stays out of big trending/volatile days — across our directional history, **every day we moved ±$1k+ (both our +$2k wins and −$3k losses) had prior-day ADX ≥ 25, so the strangle stood aside on 100% of them.** But among the low-ADX days it *does* trade, outcomes split sharply by the day's realized intraday range (2026-YTD calm-day replay, 1 lot):

| intraday range (high−low / open) | days | win% | stop% | avg P&L |
|---|---|---|---|---|
| quiet (<0.6%) | 11 | 100% | 0% | +$128 |
| normal (0.6–1.0%) | 22 | 82% | 0% | +$115 |
| **choppy/wide (>1.0%)** | 28 | **64%** | **18%** | **+$34** |

So the precise risk statement: **the strangle wants QUIET (tight range-bound), not "choppy."** A genuinely choppy day — big intraday swings, even with no net direction — is its *enemy*: 18% of those stop out and average P&L collapses to +$34. It still *takes* those trades (the prior-day filter can't catch a day that looked calm yesterday and swings today), and **that surprise-intraday-swing day IS the gap-stop tail** above. The residual risk isn't trend (filtered) — it's the unforecastable intraday range expansion.

**So: the edge is real and cost-robust. The remaining unknown is whether the mechanical stop fills as cleanly as modeled when vol expands.** That is exactly what paper-then-tiny-real measures.

## 4b. Capital, lot-sizing & expected monthly return (real 2026-YTD replay)

Grounded in `scripts/strangle_calm_days.py` (+ per-month breakdown): the deterministic strangle on the **calm days its filter actually trades** across 2026 YTD (Jan 1 → Jun 18, ~5.6 months, 61 trades, real VIX1D + SPY path, realistic $0.04/leg cost, **1 contract**) returned **+$4,900 (77% win)**. Mirror image: our directional engine lost **−$4,846** over the same span trading the *trending* days the strangle sat out.

**Per-contract economics:** avg credit **$199**, avg nominal risk (1.5× credit stop) **$298**, on **~$69k notional** (SPY ~$694 × 100). Naked → capital is set by **broker margin (~$14k buying-power per lot, Reg-T ~20% of notional)**, NOT by trade structure.

**Monthly P&L is REAL but badly lumpy — do not trust the average:**

| month | trades | win | P&L (1 lot) |
|---|---|---|---|
| 2026-01 | 20 | 19/20 | +$2,450 |
| 2026-02 | 19 | 14/19 | +$2,097 |
| 2026-03 | 10 | 7/10 | +$671 |
| 2026-04 | 3 | 2/3 | −$7 |
| 2026-05 | 7 | 5/7 | +$311 |
| 2026-06 | 2 | 0/2 | −$622 |

**Two calm months (Jan/Feb) made nearly the whole result; four were flat-to-down.** A realistic "typical" month is **+2–4%**, a great calm month **+14–16%**, a bad/gap month **−4%** — arriving in lumps, not a smooth drip. **Do NOT annualize the ~70%/yr run-rate** off a 5.6-month, front-loaded, calm-regime sample.

**By account size (P&L scales linearly with lots; margin ~$14k/lot is the binding constraint):**

| Account | Prudent lots | Avg/month | ~5.6mo total | Worst month | Worst single day* |
|---|---|---|---|---|---|
| **$5k** | **0 — cannot hold one naked SPY lot** | — | — | — | — |
| **$15k** | 1 (~28% BP) | +$875 (+5.8%) | +$4,900 (+33%) | −$622 (−4.1%) | −$712 (−4.7%) |
| **$50k** | 2 (~56% BP) | +$1,750 (+3.5%) | +$9,800 (+20%) | −$1,244 (−2.5%) | −$1,424 (−2.8%) |
| **$50k aggressive** | 3 (~84% BP) | +$2,625 (+5.3%) | +$14,700 (+29%) | −$1,866 (−3.7%) | −$2,136 (−4.3%) |

\* worst single day = the **modeled** gap-stop at constant-vol fills — **real fills under a vol spike would be worse.**

**Two capital constraints that are easy to miss:**
1. **$5k can't run this at all** — one naked SPY lot needs ~$14k margin; the account is below the minimum to *hold* the position (would require portfolio margin or a defined-risk version, which reintroduces the leg-cost problem that killed the condor). **~$14–15k is the floor for a single lot.**
2. **Naked short-option margin EXPANDS as price runs at your strikes** — i.e. mid-trade, on a bad day, exactly when you're losing. Sizing near the BP ceiling risks a **forced liquidation at the worst moment**. The unused buffer *is* a risk control: on $50k, **2 lots is the prudent ceiling, 3 is the high-risk edge.**

**Bottom line:** ~**+2–4%/month "normal," mid-single-digits on average, in lumps**, on a **~$14k-per-lot** capital base — but every dollar of it still rides on the two unconfirmed assumptions (real stop-fill quality under vol expansion; whether the calm-regime edge holds through a trending/vol stretch — June was −4%). Capital sizes the dollars; it does not create or confirm the edge.

## 4c. Dual-strategy / regime-router verdict — RESOLVED ON EVIDENCE

The idea (good instinct): route by prior-day ADX — quiet day → short strangle, trend day → some other strategy — so there's "a rule for every regime." The two sides are genuine greek complements (short strangle = short gamma; a trend strategy = long gamma). We tested BOTH candidate trend-day legs through the same gate. Scripts: `backtest_trend_leg.py`, `backtest_modulated_seller.py`.

| Regime (prior-day ADX) | Candidate tested | OOS Sharpe | DSR | Verdict |
|---|---|---|---|---|
| **Quiet (<25)** | Short strangle (`backtest_strangle.py`) | +2.5 | 98–99% | **PASS** ✅ — the strategy |
| **Trend (≥25), long gamma** | Long straddle (`backtest_trend_leg.py`) | **−2.14** | **0%** | **FAIL** ❌ — −EV in *and* out of sample |
| **Trend (≥25), short premium** | Defensive/skewed strangle (`backtest_modulated_seller.py`) | +0.84 | **6%** | **MARGINAL** ⚠️ — +EV but uncertified |

**Three findings that close the question:**
1. **Long gamma on trend days is structurally −EV** (IS −2.05, OOS −2.14, DSR 0%, 26% win) — not overfitting (decay +0.09), not cost (same at optimistic). The VRP that *pays* the strangle *charges* the straddle: on a trend day IV is already rich, so the buyer rarely earns back the premium. **There is no regime where buying SPY 0DTE premium is +EV.** This also kills the original "deploy a trend strategy on volatile days" plan — and a 10%-of-capital halt can't rescue a −EV leg, it only bounds the bleed.
2. **Selling premium on trend days is mildly +EV but fails the gate** (OOS +0.84, 72% win, +18%/trade, but DSR 6% realistic / 0.1% conservative). Thin sample (364 high-ADX days → 5 folds / 105 OOS trades) and a *worse* negative tail than quiet days (worst −742% of risk vs −558%) keep it below the bar.
3. **Direction is unextractable even on trend days** — the modulated-seller walk-forward picked `skew=0.0`; recentering the strangle toward the prior-day trend added nothing. Only the *defensive* width + tight stop carried the small edge. (Third independent confirmation, after the original engine and the long leg.)

**VERDICT: the system is ONE gate-proven strategy + a default of CASH — not two active engines.**
- **Quiet day (ADX<25, VIX1D<40) → short strangle.** (the only certified edge)
- **Trend day (ADX≥25) → FLAT.** Long gamma is −EV; short premium is uncertified and tail-heavy. Per gate discipline (the same standard that correctly rejected the trend engine at DSR 1.3%), trend days are cash.
- *Optional, opt-in only:* a high-risk appetite MAY harvest trend-day premium as an **experimental sleeve at ~⅓ sizing**, explicitly logged as uncertified — but only after the quiet-day strangle proves real stop-fills in paper. Not part of the core system; do not size it as if it passed.

## 5. Build plan (not started)

1. **Paper-test to MEASURE real stop fills** under live vol — the one assumption the backtest can't prove. Reuse the deterministic-engine pattern (pure `decide()` + rules exit) already shipped for directional.
2. Determinize a strangle executor (2-leg sell + the intraday stop-monitor loop). The disabled `agents/options/` iron-condor infra is the nearest starting point.
3. Re-run the gate on **real paper fills** (especially stop fills) before any real capital.
4. Only on PASS-with-real-fills: tiny real size. Capital is not the lever — expectancy is.

## 6. How this fits the bar set on 2026-06-18

The gate's bar for ANY strategy: **positive OOS Sharpe after costs AND DSR>95% AND survives the worst vol fold.** The trend engine failed it. The condor passed the statistics but failed the cost-robustness sub-test. **The strangle is the first to pass the statistics AND the cost-robustness test** — with the honest remaining caveat being stop execution, not edge.
