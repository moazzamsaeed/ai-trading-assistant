"""Tests for Black-Scholes pricing + synthetic-chain generation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from math import isclose

from backtests.synthetic_options import (
    bs_call_delta,
    bs_call_price,
    bs_put_delta,
    bs_put_price,
    generate_chain,
)

# ----------------- Black-Scholes math -----------------


def test_bs_call_at_expiry_pays_intrinsic():
    assert bs_call_price(110, 100, 0, 0.05, 0.2) == 10
    assert bs_call_price(90, 100, 0, 0.05, 0.2) == 0


def test_bs_put_at_expiry_pays_intrinsic():
    assert isclose(bs_put_price(90, 100, 0, 0, 0.2), 10, abs_tol=1e-9)
    assert isclose(bs_put_price(110, 100, 0, 0, 0.2), 0, abs_tol=1e-9)


def test_bs_put_call_parity():
    """C - P should equal S - K·e^(-rT) (no dividends)."""
    S, K, T, r, sigma = 100.0, 100.0, 0.5, 0.04, 0.20
    from math import exp
    call = bs_call_price(S, K, T, r, sigma)
    put = bs_put_price(S, K, T, r, sigma)
    assert isclose(call - put, S - K * exp(-r * T), abs_tol=1e-6)


def test_bs_call_delta_atm_is_near_half():
    """ATM call delta ≈ 0.5 (slightly above for positive r and T)."""
    d = bs_call_delta(100, 100, 0.05, 0.04, 0.20)
    assert 0.4 < d < 0.7


def test_bs_put_delta_far_otm_near_zero():
    d = bs_put_delta(500, 450, 0.05, 0.04, 0.20)  # 10% OTM put
    assert -0.1 < d < 0.0


# ----------------- chain generator -----------------


def test_generate_chain_returns_puts_and_calls():
    chain = generate_chain(
        spy_price=500.0,
        expiry=date(2026, 5, 11),
        hours_to_expiry=6.25,
        iv=0.20,
    )
    puts = [q for q in chain if q.option_type == "put"]
    calls = [q for q in chain if q.option_type == "call"]
    assert len(puts) > 10
    assert len(calls) > 10


def test_generate_chain_includes_wing_strikes_for_iron_condor():
    """A 16-delta short put at ~$497 needs a $492 wing to be quotable."""
    chain = generate_chain(
        spy_price=500.0, expiry=date(2026, 5, 11), hours_to_expiry=6.25, iv=0.20,
    )
    put_strikes = {float(q.strike) for q in chain if q.option_type == "put"}
    assert 492.0 in put_strikes  # wing strike for typical IC must exist


def test_generate_chain_strikes_are_decimal():
    chain = generate_chain(
        spy_price=500.0, expiry=date(2026, 5, 11), hours_to_expiry=6.25,
    )
    for q in chain[:5]:
        assert isinstance(q.strike, Decimal)
        assert isinstance(q.mid, Decimal)
        assert q.bid <= q.mid <= q.ask
        assert q.bid > 0


def test_generate_chain_deltas_sign_correctly():
    """Put deltas are negative, call deltas positive."""
    chain = generate_chain(
        spy_price=500.0, expiry=date(2026, 5, 11), hours_to_expiry=6.25,
    )
    for q in chain:
        if q.delta is None:
            continue
        if q.option_type == "put":
            assert q.delta <= 0
        else:
            assert q.delta >= 0
