"""Deterministic SPY 0DTE iron-condor decision engine — the condor's *brain*.

Pure, reproducible, backtestable, auditable — the options-side analogue of
agents/directional/signal_engine.py. Mirrors scripts/backtest_wide_condor.py
EXACTLY so the live decision matches the validated backtest:

  • REGIME FILTER: trade only on calm days — prior-day Wilder ADX < 25 AND
    VIX1D < 40 (vol points). Trending / crisis days → HOLD (the 43% of days the
    edge avoids; see docs/STRANGLE_STRATEGY_DESIGN.md §4d).
  • STRIKES: short put / short call at spot ∓ 0.5 × (VIX1D expected move),
    long wings $5 further OTM. Expected move EM = spot · VIX1D · √T, T in
    TRADING time (252×390 min/yr) — MUST match VIX1D's trading-time annualization
    (the calendar-vs-trading bug placed strikes 2.3× too tight in backtest_vrp).
  • The engine outputs TARGET STRIKES + the regime decision only. Credit /
    max-loss / fill come from the live chain at execution (strategist), never
    modelled here — so the engine stays pure and I/O-free.

VIX1D has no production feed, so vix1d_from_chain() derives the 1-day implied
vol live from the SPY 0DTE chain: prefer the ATM options' quoted IV, fall back
to inverting the ATM straddle mid via the small-T ATM approximation.

Bump CONDOR_VERSION on any rule change so persisted decisions stay auditable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

CONDOR_VERSION = "vrp_condor_v1"

# --- rules (promote to settings.* when tuning; kept as constants for parity) ---
ADX_MAX = 25.0            # prior-day Wilder ADX ≥ this → trending → HOLD
VIX1D_MAX = 40.0          # VIX1D (vol points) ≥ this → crisis vol → HOLD
K_SHORT = 0.5             # short strikes at 0.5 × expected move
WING = 5.0                # long-wing width ($)
STOP_MULT = 1.5           # intraday stop: exit when buy-back ≥ (1 + STOP_MULT)×credit
YEAR_MIN = 252 * 390      # trading-time annualization (MUST match VIX1D)
ATM_STRADDLE_K = 0.7978845608  # 2/√(2π): ATM straddle ≈ K·S·σ·√T (small-T approx)


@dataclass(frozen=True)
class CondorDecision:
    """Deterministic condor decision. action ∈ {SELL_CONDOR, HOLD}."""
    action: str
    reason: str
    short_put: float | None = None
    long_put: float | None = None
    short_call: float | None = None
    long_call: float | None = None
    expected_move: float | None = None
    vix1d: float | None = None      # vol points (e.g. 12.5)
    prior_adx: float | None = None
    version: str = CONDOR_VERSION

    @property
    def is_trade(self) -> bool:
        return self.action == "SELL_CONDOR"


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def vix1d_from_chain(chain, spot: float, minutes_to_close: float) -> float | None:
    """Derive VIX1D (vol POINTS, e.g. 12.5 == 12.5%) from a 0DTE option chain.

    Primary: average the ATM call & put quoted implied_volatility.
    Fallback: invert the ATM straddle mid via straddle ≈ K·S·σ·√T (small-T ATM).
    Returns None if neither is recoverable. `chain` is a list of OptionQuote.
    """
    spot = _f(spot)
    if not spot or spot <= 0 or not chain:
        return None
    calls = [q for q in chain if q.option_type == "call"]
    puts = [q for q in chain if q.option_type == "put"]
    if not calls or not puts:
        return None
    atm_call = min(calls, key=lambda q: abs(float(q.strike) - spot))
    atm_put = min(puts, key=lambda q: abs(float(q.strike) - spot))

    # primary — quoted IV (alpaca returns it as a decimal fraction, e.g. 0.125)
    ivs = [_f(atm_call.implied_volatility), _f(atm_put.implied_volatility)]
    ivs = [iv for iv in ivs if iv and iv > 0]
    if ivs:
        return (sum(ivs) / len(ivs)) * 100.0

    # fallback — invert ATM straddle mid: straddle = K·S·σ·√T → σ = straddle/(K·S·√T)
    cm = _f(atm_call.mid)
    pm = _f(atm_put.mid)
    T = max(_f(minutes_to_close) or 0.0, 1.0) / YEAR_MIN
    if cm is not None and pm is not None and T > 0:
        straddle = cm + pm
        denom = ATM_STRADDLE_K * spot * math.sqrt(T)
        if denom > 0 and straddle > 0:
            return (straddle / denom) * 100.0
    return None


def decide_condor(
    spot,
    vix1d,            # vol POINTS (e.g. 12.5), as returned by vix1d_from_chain
    prior_adx,        # prior-day Wilder ADX (known at entry; no lookahead)
    minutes_to_close,
) -> CondorDecision:
    """Pure function: market state → CondorDecision. No I/O, no chain, no LLM.

    Identical regime/strike logic to scripts/backtest_wide_condor.py.
    """
    spot = _f(spot); vix1d = _f(vix1d); prior_adx = _f(prior_adx)
    mtc = _f(minutes_to_close)

    def hold(reason):
        return CondorDecision("HOLD", f"{CONDOR_VERSION}: {reason}",
                              vix1d=vix1d, prior_adx=prior_adx)

    if None in (spot, vix1d, prior_adx, mtc) or spot <= 0:
        return hold("inputs missing (spot/vix1d/adx/time)")
    if vix1d <= 0:
        return hold(f"invalid VIX1D ({vix1d})")
    if vix1d >= VIX1D_MAX:
        return hold(f"crisis vol (VIX1D {vix1d:.1f} ≥ {VIX1D_MAX:.0f}) — stand aside")
    if prior_adx >= ADX_MAX:
        return hold(f"trending (prior-day ADX {prior_adx:.1f} ≥ {ADX_MAX:.0f}) — stand aside")

    T = max(mtc, 1.0) / YEAR_MIN
    em = spot * (vix1d / 100.0) * math.sqrt(T)
    if em <= 0:
        return hold(f"degenerate expected move ({em})")

    short_put = round(spot - K_SHORT * em)
    long_put = short_put - WING
    short_call = round(spot + K_SHORT * em)
    long_call = short_call + WING
    if not (long_put < short_put < short_call < long_call):
        return hold(f"degenerate strikes ({long_put}/{short_put}/{short_call}/{long_call})")

    reason = (f"{CONDOR_VERSION}: calm (ADX {prior_adx:.1f}<{ADX_MAX:.0f}, "
              f"VIX1D {vix1d:.1f}<{VIX1D_MAX:.0f}); EM ${em:.2f} → condor "
              f"{long_put:.0f}/{short_put:.0f} - {short_call:.0f}/{long_call:.0f}")
    return CondorDecision(
        "SELL_CONDOR", reason,
        short_put=float(short_put), long_put=float(long_put),
        short_call=float(short_call), long_call=float(long_call),
        expected_move=em, vix1d=vix1d, prior_adx=prior_adx,
    )


def stop_breached(credit: float, current_mark: float) -> bool:
    """Intraday stop: True when buy-back cost ≥ credit + STOP_MULT·credit.
    `credit` and `current_mark` are the condor's net entry credit and current
    buy-to-close mark (same units, e.g. $/share or $/contract)."""
    credit = _f(credit); current_mark = _f(current_mark)
    if credit is None or current_mark is None or credit <= 0:
        return False
    return current_mark >= credit * (1.0 + STOP_MULT)
