# TradeMaster Strategy Review — 2026-06-18

A fundamental review of the directional 0DTE strategy, triggered by a poor
$5k-era paper run. This is a **decision document**, not a change log — nothing
below has been implemented. Read it, sit with it, then decide direction.

All numbers are from the live SQLite trade history (`data/trademaster.db`,
directional_call + directional_put) and from replay/backtest scripts in
`scripts/` (`replay_model_comparison.py`, `backtest_macd_intraday.py`).

---

## TL;DR verdict

The strategy has **no demonstrated, durable edge.** It made money in one
favorable two-week window (early June, clean trends) and has given it back in
choppier tape. The profit came from **full-size trades in a good regime** and
the **put side**; the losses come from **calls, twitchy 0DTE exits, and an
operational sync bug.** Capital size is *not* the problem — it multiplies
expectancy, it doesn't create it. **Do not go live with real money on the
current design.**

The highest-leverage structural changes (in order): **(1) move off 0DTE to
weeklies, (2) stop sizing off LLM "conviction," (3) loosen the intraday exit
cuts, (4) fix or kill the call side, (5) fix the position-desync bug.**

---

## The evidence

### Performance by era
| Era | Trades | Win% | Net | Avg/trade |
|---|---|---|---|---|
| $25k (06-01→06-13) | 32 | 56% | **+$2,248** | +$70 |
| $5k (06-14→now) | 7 | 29% | **−$138** | −$20 |
| All-time directional | 77 | 36% | **−$2,721** | −$35 |

The $5k era is only 7 trades — **statistically meaningless on its own.** The
"decline since $25k→$5k" is not established by this sample; it may be variance
plus regime. The all-time −$2,721 is the more sobering number.

### Call vs put
| Side | Trades | Win% | Net | Avg/trade |
|---|---|---|---|---|
| BUY_CALL | 40 | 40% | **−$3,727** | −$93 |
| BUY_PUT | 37 | 32% | **+$1,006** | +$27 |

**The entire all-time loss is the call side.** Puts are net positive.

### Exit reasons (where the P&L actually comes from)
| Exit | n | Net |
|---|---|---|
| smart_exit | 22 | **−$5,235** |
| smart_profit_exit | 9 | **+$7,900** |
| trailing_stop | 11 | +$2,628 |
| thesis_invalidated | 2 | −$2,964 |
| hard_floor_stop | 4 | −$2,855 |
| position_not_in_broker / not_found_in_alpaca | 12 | **−$1,814** |
| kill_switch / manual / force / stop_loss | 15 | +$638 (net) |

### Conviction by model era
| Era (entry model) | HIGH | MEDIUM |
|---|---|---|
| DeepSeek (≤06-14) | 24 (+$2,822) | 12 (−$2,734) |
| Sonnet (≥06-15) | **0** | 7 (−$138) |

---

## Findings

### 1. Capital is not the lever
The $25k era was profitable because expectancy was positive *then*, not because
of the capital. At $5k the losses are small **because positions are small** —
the expectancy (−$20/trade) is what's broken. Scaling capital back up would
**magnify the current negative expectancy into bigger dollar losses.** Capital is
a multiplier on a number that is currently ≤ 0.

What capital *does* affect is **sizing granularity**: at $5k a MEDIUM trade floors
to 1 contract (~40% of the budget lost to integer rounding). Smooth sizing
(5–15 SPY contracts, minimal flooring) needs roughly **$15–25k**. But that only
matters *after* an edge exists — otherwise it just buys smoother losses.

### 2. The CALL side is structurally broken
Calls lose −$93/trade (−$3,727 total); puts make +$27/trade. The "put bias" the
strategy shows is plausibly **the model correctly avoiding its losing bullish
setups**, not a defect. The real defect is that the bullish entry logic (price >
VWAP, RSI 40–80, EMA, volume) **fires at tops in choppy/topping tape and gets
reversed.** Half the strategy loses money.

### 3. Exits cut correct-direction trades — the single biggest drain
`smart_exit` is −$5,235, the largest loss bucket. It fires on **intraday reversal
signals** (RSI ticking against you, price crossing VWAP, EMA cross,
`volume_fading`). On 0DTE these trip on a 15–30 min wiggle even when the trade is
right *on the day*. **Proof: 06-17, SPY closed −$10.25; the bot was correctly
bearish and its puts were cut intraday for small losses** before the move paid.
The exits that *work* are the ones that let winners run (smart_profit_exit
+$7,900, trailing_stop +$2,628).

### 4. 0DTE is the wrong instrument for this thesis
Backtests (`backtest_macd_intraday.py`, ~500 signals/side across NVDA/AMD/META):
the directional edge of classic TA signals lives on a **daily/multi-day horizon**
and is a **coin flip on 15-min intraday** — hit rates within ±3pp of baseline,
returns in single-digit basis points (below option spread + theta), and an
**ADX≥25 trend filter did not rescue it.** 0DTE forces intraday timing precision
the strategy doesn't have. **Weeklies decouple "right direction" from "right
intraday timing"** (slower theta, room to be early) — the exact failure mode in
Finding 3. Cost: more premium per contract (fewer contracts at $5k), less gamma.

### 5. LLM "conviction" is unreliable — and sizing is built on it
Replay (`replay_model_comparison.py`, identical prompts, vary only the model):

| Model | BUYs rated HIGH | rated MEDIUM |
|---|---|---|
| DeepSeek | ~89% | ~11% |
| Sonnet | ~13% | ~87% |

Same setups, same actions/directions — **conviction diverges entirely by model.**
This **confirms the model swap caused the HIGH-conviction collapse** in
production. BUT DeepSeek rates ~everything HIGH → "HIGH" under DeepSeek was a
**default, not a quality signal.** So the production finding "HIGH made +$2,822"
is an **artifact**: it means *full-size trades in a good regime made money*. The
conviction tier does no discriminative work, yet **position size (0.5× for
MEDIUM) is keyed to it.** Sizing rests on model-personality noise.

**Implication:** reverting to DeepSeek restores full-size sizing — **bigger bets,
not better bets** (would have lost more in the 06-11 chop / 06-17 whipsaw). The
fix is to **replace LLM-conviction sizing with a reproducible, rules-based
metric** (e.g. ADX / setup-quality score).

### 6. Other fundamental issues
- **No validated edge.** This is an LLM doing discretionary TA; the underlying
  signals are coin-flips intraday (Finding 4). It is regime-dependent.
- **Operational bug:** `position_not_in_broker` + `not_found_in_alpaca` = 12
  trades, **−$1,814 lost to DB/Alpaca desync** — reliability, not strategy.
- **Overfitting:** the loss-prevention package (chop filter, ADX gate, freshness,
  trailing params) was tuned on a *handful* of bad days. Many knobs, tiny
  samples → likely won't generalize.
- **0DTE cost drag** (spread + theta) is large vs. the small per-trade edge.
- **Aggressive mode executes MEDIUM** — the marginal/negative bucket.

### Research aside — the MACD + 200-EMA rule (tested 06-18)
A "trade with the 200-EMA trend, enter on a MACD cross on the counter-trend side
of zero" rule was tested across SPY/TSLA/NVDA/AMD/META: a weak,
**instrument-dependent** call edge on the *daily* chart (SPY/NVDA/META positive
at 1–5 days; TSLA/AMD negative), **no put edge**, and **no edge at all on 15-min
intraday** even with an ADX filter. Reinforces Finding 4: classic TA has no
exploitable intraday edge on these names.

---

## Can we promise a return? No.
- All-time record is **net −$2,721 (36% win, 77 trades).** The only sustained
  positive stretch is 32 trades in a favorable regime.
- 0DTE directional options are **inherently high-variance.**
- Expected value on current evidence is **break-even-to-negative after costs,**
  with a confidence interval that comfortably spans zero. **No consistent % can
  be promised.** Anyone quoting a reliable % on 0DTE SPY is wrong.

---

## Decision options (prioritized)

**A. Instrument — move off 0DTE.** Test weeklies (or allow 0DTE→weekly by
setup). Highest leverage; directly addresses Findings 3 & 4.

**B. Sizing — drop LLM conviction.** Replace the HIGH/MEDIUM 0.5× sizing with a
rules-based score (ADX, ATR, setup-quality). Addresses Finding 5. Do **not**
simply revert to DeepSeek.

**C. Exits — loosen intraday cuts** for trades aligned with the daily trend
(require more/stronger reversal signals before `smart_exit`; widen the trailing
gap on weeklies). Addresses the −$5,235 drain.

**D. Call side — fix or disable.** Investigate why bullish trades lose
(−$3,727); consider puts-only or a stricter bullish filter until calls are
validated.

**E. Reliability — fix the position-desync bug** (−$1,814, pure operational loss).

**F. Validation gate.** Whatever the config, **prove positive expectancy over
30–50 paper trades in a frozen config before any real money.** Be open to the
answer being "no edge — don't trade it live."

---

## Go-live gate (hard stop)
The plan was to go live with real $5k. **On this evidence, do not.** The paper
week is the gate and it is currently failing (−$138, 1W/5L, negative expectancy,
all in the unreliable MEDIUM bucket). Going live now would put real money on a
strategy with no demonstrated edge. Sequence: **fix structure (A–E) → freeze
config → prove expectancy on paper (F) → only then reconsider capital.**
