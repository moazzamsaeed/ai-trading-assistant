"""Unit tests for the deterministic directional signal engine.

The whole point of the platform-first refactor is that the decision is now a pure,
testable function. These lock in the validated trend-follow + sweet-spot rules.
"""
from agents.directional.signal_engine import decide, forming_signal, ENGINE_VERSION


def _snap(price, vwap, ema20, adx, **extra):
    s = {"last_close": price, "vwap": vwap, "ema20": ema20, "adx": adx,
         "rsi9": 55, "ema50": ema20, "atr10": 0.5}
    s.update(extra)
    return s


def test_uptrend_sweet_spot_is_high_call():
    # price above VWAP+EMA, ADX in [30,40), VWAP-dist ~0.18% → HIGH BUY_CALL
    d = decide("SPY", _snap(745.0, 743.7, 743.5, 34.0))
    assert d.action == "BUY_CALL"
    assert d.conviction == "HIGH"
    assert d.strike == 745.0  # ATM reference
    assert d.expiry == "0DTE"
    assert ENGINE_VERSION in d.reasoning


def test_downtrend_sweet_spot_is_high_put():
    d = decide("SPY", _snap(745.0, 746.3, 746.5, 35.0))
    assert d.action == "BUY_PUT"
    assert d.conviction == "HIGH"


def test_in_trend_but_outside_sweet_spot_is_medium():
    # uptrend, ADX 27 (in-trend but below the 30-40 HIGH band) → MEDIUM
    d = decide("SPY", _snap(745.0, 743.5, 743.3, 27.0))
    assert d.action == "BUY_CALL"
    assert d.conviction == "MEDIUM"


def test_weak_trend_holds():
    # ADX below 25 → no tradeable trend
    d = decide("SPY", _snap(745.0, 743.5, 743.3, 18.0))
    assert d.action == "HOLD"


def test_overextended_adx_holds():
    # ADX >= 50 → overextended, reverts → HOLD even though it's "trending hard"
    d = decide("SPY", _snap(745.0, 743.0, 742.8, 60.0))
    assert d.action == "HOLD"


def test_far_from_vwap_holds():
    # dist > 0.5% → overextended → HOLD
    d = decide("SPY", _snap(745.0, 740.0, 739.5, 35.0))
    assert d.action == "HOLD"


def test_too_close_to_vwap_holds():
    # dist < 0.10% → no separation / no edge → HOLD
    d = decide("SPY", _snap(745.0, 744.7, 744.6, 35.0))
    assert d.action == "HOLD"


def test_no_trend_holds():
    # price above VWAP but below EMA (conflicting) → no clean trend → HOLD
    d = decide("SPY", _snap(745.0, 744.0, 746.0, 35.0))
    assert d.action == "HOLD"


def test_missing_indicators_holds():
    d = decide("SPY", {"last_close": 745.0, "vwap": None, "ema20": 744.0, "adx": 30.0})
    assert d.action == "HOLD"


def test_puts_only_skips_calls(monkeypatch):
    import agents.directional.signal_engine as se
    monkeypatch.setattr(se, "_puts_only", lambda: True)
    # uptrend (would be a CALL) → HOLD under puts-only
    assert decide("SPY", _snap(745.0, 743.7, 743.5, 34.0)).action == "HOLD"
    # downtrend still produces a PUT
    assert decide("SPY", _snap(745.0, 746.3, 746.5, 35.0)).action == "BUY_PUT"


def test_forming_detects_near_put():
    # downtrend in place, ADX 22 building toward the 25 entry threshold
    fs = forming_signal("SPY", _snap(745.0, 746.2, 746.4, 22.0))
    assert fs and fs["would_be_action"] == "BUY_PUT" and fs["engine"] is True


def test_forming_none_when_overextended():
    assert forming_signal("SPY", _snap(745.0, 746.0, 746.2, 55.0)) is None


def test_forming_none_when_already_a_trade():
    # ADX 35 + good separation is an ENTRY, not a forming near-miss
    assert forming_signal("SPY", _snap(745.0, 746.3, 746.5, 35.0)) is None


def test_forming_puts_only_skips_call_forming(monkeypatch):
    import agents.directional.signal_engine as se
    monkeypatch.setattr(se, "_puts_only", lambda: True)
    assert se.forming_signal("SPY", _snap(745.0, 743.5, 743.3, 22.0)) is None


def test_decision_is_deterministic():
    s = _snap(745.0, 743.7, 743.5, 34.0)
    a = decide("SPY", s)
    b = decide("SPY", s)
    assert (a.action, a.conviction, a.strike) == (b.action, b.conviction, b.strike)
