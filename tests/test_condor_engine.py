"""Unit tests for the deterministic iron-condor engine.

Locks in the validated regime filter + strike rules (parity with
scripts/backtest_wide_condor.py) and the live VIX1D derivation.
"""
from dataclasses import dataclass
from decimal import Decimal

from agents.options.condor_engine import (
    CONDOR_VERSION, decide_condor, vix1d_from_chain, stop_breached,
    ADX_MAX, VIX1D_MAX, K_SHORT, WING,
)

# typical 0DTE entry: ~6h to close = 360 min
MTC = 360.0


def test_calm_day_sells_condor():
    d = decide_condor(spot=600.0, vix1d=12.0, prior_adx=18.0, minutes_to_close=MTC)
    assert d.is_trade and d.action == "SELL_CONDOR"
    # strikes ordered, wings exactly $5, short strikes straddle spot
    assert d.long_put < d.short_put < d.short_call < d.long_call
    assert d.short_put - d.long_put == WING
    assert d.long_call - d.short_call == WING
    assert d.short_put < 600.0 < d.short_call
    assert CONDOR_VERSION in d.reason


def test_trending_day_holds():
    d = decide_condor(spot=600.0, vix1d=12.0, prior_adx=ADX_MAX, minutes_to_close=MTC)
    assert d.action == "HOLD" and "trending" in d.reason
    assert d.short_put is None


def test_crisis_vol_holds():
    d = decide_condor(spot=600.0, vix1d=VIX1D_MAX, prior_adx=15.0, minutes_to_close=MTC)
    assert d.action == "HOLD" and "crisis" in d.reason


def test_missing_inputs_hold():
    assert decide_condor(None, 12.0, 18.0, MTC).action == "HOLD"
    assert decide_condor(600.0, None, 18.0, MTC).action == "HOLD"
    assert decide_condor(600.0, 12.0, None, MTC).action == "HOLD"
    assert decide_condor(0.0, 12.0, 18.0, MTC).action == "HOLD"


def test_higher_vix_widens_strikes():
    lo = decide_condor(600.0, 8.0, 18.0, MTC)
    hi = decide_condor(600.0, 20.0, 18.0, MTC)
    lo_width = lo.short_call - lo.short_put
    hi_width = hi.short_call - hi.short_put
    assert hi_width > lo_width  # more implied vol → wider short strikes


def test_expected_move_uses_trading_time():
    # EM = spot * (vix1d/100) * sqrt(mtc/YEAR_MIN); sanity: ~ a few $ for SPY-ish
    d = decide_condor(600.0, 12.0, 18.0, MTC)
    assert 0.5 < d.expected_move < 15.0


def test_stop_breached_rule():
    # credit 1.00, stop at 1.5x loss → buy-back >= 2.50 triggers
    assert not stop_breached(1.00, 2.49)
    assert stop_breached(1.00, 2.50)
    assert stop_breached(1.00, 3.00)
    assert not stop_breached(0.0, 5.0)  # degenerate credit → no stop


# ---- VIX1D derivation from chain ----

@dataclass(frozen=True)
class FakeQuote:
    option_type: str
    strike: Decimal
    mid: Decimal
    implied_volatility: Decimal | None


def _chain_with_iv(spot, iv):
    return [
        FakeQuote("call", Decimal(str(spot)), Decimal("3.0"), Decimal(str(iv))),
        FakeQuote("put", Decimal(str(spot)), Decimal("3.0"), Decimal(str(iv))),
        FakeQuote("call", Decimal(str(spot + 5)), Decimal("1.0"), Decimal(str(iv))),
    ]


def test_vix1d_from_quoted_iv():
    # ATM IV 0.125 (decimal) → 12.5 vol points
    v = vix1d_from_chain(_chain_with_iv(600, 0.125), spot=600.0, minutes_to_close=MTC)
    assert v is not None and abs(v - 12.5) < 1e-6


def test_vix1d_straddle_fallback_when_no_iv():
    # no IV → invert straddle. ATM call+put mid = 6.0 total.
    chain = [
        FakeQuote("call", Decimal("600"), Decimal("3.0"), None),
        FakeQuote("put", Decimal("600"), Decimal("3.0"), None),
    ]
    v = vix1d_from_chain(chain, spot=600.0, minutes_to_close=MTC)
    assert v is not None and v > 0  # recovers a positive vol estimate


def test_vix1d_none_when_unusable():
    assert vix1d_from_chain([], 600.0, MTC) is None
    assert vix1d_from_chain(None, 600.0, MTC) is None


def test_end_to_end_chain_to_decision():
    chain = _chain_with_iv(600, 0.12)
    v = vix1d_from_chain(chain, 600.0, MTC)
    d = decide_condor(600.0, v, prior_adx=15.0, minutes_to_close=MTC)
    assert d.is_trade
