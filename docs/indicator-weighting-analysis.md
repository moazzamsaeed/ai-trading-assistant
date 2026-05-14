# Indicator Weighting Analysis — TradeMaster Directional System

**Created:** 2026-05-14  
**Status:** Pending review after near-miss data collection (checkpoint: 2026-05-20)

---

## Current System (as of 2026-05-14)

Every indicator is **equal weight** — each counts as 1 point:

| Indicator | Points | Period | Notes |
|---|---|---|---|
| VWAP | 1 | Session | Price must be on correct side |
| RSI-9 | 1 | 9 bars | Changed from 14; thresholds 45–72 bull / 28–55 bear |
| EMA-20/50 | 1 | 20 & 50 bars | EMA50 unavailable first ~2.5h of RTH |
| Volume ratio >1.3× | 1 | 20-bar avg | Must exceed 130% of 20-bar average |

**Scoring:**
- 4/4 = HIGH conviction → ATM strike, 0DTE eligible (before 14:00 ET)
- 3/4 = MEDIUM conviction → 1-strike OTM, WEEKLY only
- 2/4 or fewer = LOW → HOLD

---

## The Problem: Equal Weighting Doesn't Match Reality

Research from institutional desks, prop firms (SMB Capital, T3), and options-specific educators
reveals a clear **hierarchy of evidence** for intraday options trading. Equal weighting ignores this.

### Empirical Indicator Hierarchy

**Rank 1 — VWAP (most important)**
- Every institutional algo executes orders benchmarked against VWAP all day, every day
- "Price above VWAP = smart money is net buyers. Below = net sellers." — universal institutional fact
- This is a **structural constraint**, not a signal to be averaged with others
- Source: SMB Capital, T3 Trading, TrendSpider, tastytrade execution docs

**Rank 2 — Volume (second most important)**
- SMB Capital: "Probably the single most important variable for an intraday trader"
- A price move without volume is thin, reversible, and likely to be faded
- This is why the 1.3× threshold exists — but even 1.0× is a hard minimum
- Source: SMB Capital, multiple prop firm training materials

**Rank 3 — RSI-9**
- Momentum confirmation — confirms but doesn't lead
- Can be in bullish range during a false breakout
- Useful when aligned, dangerous when treated as primary

**Rank 4 — EMA-20/50 (weakest)**
- Trend direction — slowest to respond
- Unavailable for first 2.5h of RTH (EMA-50 needs 50 bars × 5 min = 250 min)
- Confirming indicator only; should never override VWAP

---

## The Core Flaw: Two Setups That Shouldn't Be Equal

**Setup A:** VWAP ✓ + Volume ✓, RSI ✗ + EMA ✗ → 2/4 = HOLD  
**Setup B:** RSI ✓ + EMA ✓, VWAP ✗ + Volume ✗ → 2/4 = HOLD

Both score 2/4 and both HOLD — but they're **not equally risky**.

Setup A: you're aligned with institutional flow and market momentum, but indicators lag.  
Setup B: you're fighting the institutional bid/ask reference on thin volume — this is genuinely dangerous.

The equal-weight system treats these identically. It shouldn't.

---

## Proposed Architecture: Gates + Scores

Replace the equal-weight count with a two-tier structure:

### Tier 1 — GATES (both required, no substitution)
If either gate fails → **HOLD regardless of other indicators**

| Gate | Condition | Rationale |
|---|---|---|
| VWAP alignment | Price must be on correct side of VWAP | Non-negotiable institutional reference |
| Minimum volume | Volume ratio ≥ 1.0× | No dead-market entries |

### Tier 2 — SCORE (adds conviction beyond gates)

| Factor | Condition | Points |
|---|---|---|
| Strong volume | Volume ratio ≥ 1.3× | +1 |
| RSI-9 in range | 45–72 bullish / 28–55 bearish | +1 |
| EMA aligned | EMA-20 > EMA-50 (bull) or < (bear) | +1 |
| MACD aligned | MACD > signal (bull) or < (bear) | +1 |
| 15-min bias aligned | 15-min SPY regime matches direction | +1 |
| ORB breakout | Price broke above ORH (call) or below ORL (put) | +1 |

**New conviction scale:**
- Gates pass + 4–6 score points = HIGH conviction
- Gates pass + 2–3 score points = MEDIUM conviction
- Either gate fails = HOLD

**Effect:** VWAP becomes a hard requirement, not a tiebreaker. You can have 5 scoring factors perfect but if price is below VWAP → HOLD. This mirrors how institutional desks think.

---

## LLM as Final Decision-Maker: Assessment

**The current role:** The LLM reads all indicators + news + macro + relative strength and produces a JSON decision with conviction level. The count-based scoring constrains it but the LLM adds qualitative context.

**Where LLM judgment adds value:**
- Catalysts that override indicators (earnings, macro news, tariff announcements)
- Relative strength context (step 2 of hierarchy — no pure indicator captures it)
- Sector rotation and inter-market relationships
- Macro headline integration (Trump/Fed/China posts via Hermes)

**Where LLM judgment risks going wrong:**
- Inconsistency — same inputs can produce different outputs across runs
- Narrative fallacy — compelling reasoning can justify weak setups
- The equal-weight constraint is actually protecting against this

**Verdict:** Keep LLM as final judge, but upgrade the constraints it operates within:
1. Make VWAP + volume explicit gates in the prompt (not just equal criteria)
2. Present the score system (6 factors) instead of the count system (4 equal)
3. Let the LLM use qualitative judgment within that framework

---

## Implementation Plan (pending near-miss data review)

**Review trigger:** After 5 trading days (2026-05-20 checkpoint)

**Decision criteria for implementing the gate+score system:**
- If near-miss data shows patterns where VWAP+Volume pass but RSI+EMA fail and price
  moves in the predicted direction → evidence the current scoring underweights VWAP/Volume
- If MEDIUM signals (3/4 criteria) on Tue/Thu have poor outcomes → validates day filter
- If the current system is producing reasonable signal quality → defer and collect more data

**Files to change (when implementing):**
- `agents/directional/intraday.py` — PROMPT_TEMPLATE STEP 3 rewrite
- `agents/directional/intraday.py` — `_log_near_misses()` near-miss criteria update
- `trademaster/scheduler.py` — update conviction threshold language

---

## What's NOT Changing

- **VWAP remains the single most important level** — already in prompt as Step 3 primary
- **LLM remains the final decision-maker** — qualitative layer is genuinely valuable
- **Sequential 5-step hierarchy** — the structure is sound; the scoring within Step 3 is what changes
- **The 1.3× volume threshold** — may relax to 1.0× gate + 1.3× scoring bonus

---

## Sources

- Option Alpha 230k-trade dataset: Monday most profitable, Thursday worst
- Options Cafe ORB backtest (303 SPY 0DTE trades): 59.4% return, 5-min ORB optimal
- SMB Capital: RVOL as primary variable
- eplanetbrokers, goatfundedtrader, tradingsim: RSI-9 for 5-min charts
- T3 Trading, TrendSpider: VWAP as primary institutional reference
- MenthorQ: 0DTE entry window 10:00–10:30 ET optimal
- tradersdna: MACD 6-13-4 for intraday scalping
