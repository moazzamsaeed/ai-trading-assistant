"""SPY 0DTE iron-condor strategy tests.

Pure-Python tests against a fabricated option chain — no Alpaca, no LLM.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from integrations.alpaca_client import OptionQuote, parse_occ_symbol
from strategies.spy_0dte_iron_condor import (
    IronCondorBuildError,
    build_iron_condor,
)
from trademaster.models import AssetClass, Side
from trademaster.risk_manager import _is_defined_risk

# ----------------- OCC parser -----------------


def test_parse_occ_symbol_put():
    underlying, expiry, kind, strike = parse_occ_symbol("SPY240315P00495000")
    assert underlying == "SPY"
    assert expiry == date(2024, 3, 15)
    assert kind == "put"
    assert strike == Decimal("495")


def test_parse_occ_symbol_call_fractional_strike():
    _, _, kind, strike = parse_occ_symbol("SPY260510C00499500")
    assert kind == "call"
    assert strike == Decimal("499.5")


def test_parse_occ_symbol_rejects_malformed():
    with pytest.raises(ValueError):
        parse_occ_symbol("not-an-occ")


# ----------------- chain fixture -----------------


def _opt(
    *,
    kind: str,
    strike: int,
    delta: str,
    bid: str = "1.00",
    ask: str = "1.10",
    expiry: date = date(2026, 5, 11),
    underlying: str = "SPY",
) -> OptionQuote:
    pad = f"{int(strike * 1000):08d}"
    yy, mm, dd = expiry.year % 100, expiry.month, expiry.day
    letter = "C" if kind == "call" else "P"
    occ = f"{underlying}{yy:02d}{mm:02d}{dd:02d}{letter}{pad}"
    b, a = Decimal(bid), Decimal(ask)
    return OptionQuote(
        occ_symbol=occ,
        underlying=underlying,
        strike=Decimal(strike),
        expiry=expiry,
        option_type=kind,
        bid=b,
        ask=a,
        mid=(b + a) / 2,
        delta=Decimal(delta),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.20"),
    )


def _normal_chain() -> list[OptionQuote]:
    """SPY @ ~$500, $1-wide strikes from 490 to 510, plausible deltas + prices."""
    puts = []
    # Put deltas roughly: 490 → -0.08, 495 → -0.16, 500 → -0.50, 505 → -0.84
    put_data = [
        (488, "-0.05", "0.15", "0.20"),
        (490, "-0.08", "0.25", "0.30"),
        (493, "-0.12", "0.40", "0.45"),
        (495, "-0.16", "0.65", "0.70"),
        (497, "-0.25", "1.10", "1.15"),
        (500, "-0.50", "2.50", "2.55"),
    ]
    for s, d, b, a in put_data:
        puts.append(_opt(kind="put", strike=s, delta=d, bid=b, ask=a))

    calls = []
    call_data = [
        (500, "0.50", "2.55", "2.60"),
        (503, "0.25", "1.05", "1.10"),
        (505, "0.16", "0.60", "0.65"),
        (507, "0.12", "0.35", "0.40"),
        (510, "0.08", "0.20", "0.25"),
        (512, "0.05", "0.10", "0.15"),
    ]
    for s, d, b, a in call_data:
        calls.append(_opt(kind="call", strike=s, delta=d, bid=b, ask=a))
    return puts + calls


# ----------------- build_iron_condor -----------------


def test_build_iron_condor_picks_correct_short_strikes():
    plan = build_iron_condor(_normal_chain(), wing_width=Decimal("5"))
    assert plan.short_put.strike == Decimal("495")  # |delta| 0.16
    assert plan.short_call.strike == Decimal("505")  # delta 0.16
    assert plan.long_put.strike == Decimal("490")
    assert plan.long_call.strike == Decimal("510")


def test_build_iron_condor_credit_and_max_loss_math():
    plan = build_iron_condor(_normal_chain(), wing_width=Decimal("5"))
    # Mids: short_put 0.675, long_put 0.275, short_call 0.625, long_call 0.225
    # net premium = (0.675 + 0.625) - (0.275 + 0.225) = 0.80 per share
    # × 100 = $80 credit per contract
    # max loss = $500 wing - $80 credit = $420 per contract
    assert plan.credit_per_contract == Decimal("80.00")
    assert plan.max_loss_per_contract == Decimal("420.00")
    assert plan.credit_received == Decimal("80.00")
    assert plan.max_loss == Decimal("420.00")


def test_build_iron_condor_supports_multi_contract():
    plan = build_iron_condor(_normal_chain(), qty=3, wing_width=Decimal("5"))
    assert plan.qty == 3
    assert plan.credit_received == Decimal("240.00")
    assert plan.max_loss == Decimal("1260.00")


def test_build_iron_condor_rejects_zero_qty():
    with pytest.raises(IronCondorBuildError):
        build_iron_condor(_normal_chain(), qty=0)


def test_build_iron_condor_rejects_chain_missing_calls():
    puts_only = [q for q in _normal_chain() if q.option_type == "put"]
    with pytest.raises(IronCondorBuildError):
        build_iron_condor(puts_only)


def test_build_iron_condor_rejects_chain_with_no_greeks():
    no_greeks = [
        OptionQuote(
            occ_symbol="SPY260511P00495000",
            underlying="SPY",
            strike=Decimal("495"),
            expiry=date(2026, 5, 11),
            option_type="put",
            bid=Decimal("0.65"),
            ask=Decimal("0.70"),
            mid=Decimal("0.675"),
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            implied_volatility=None,
        )
    ] * 4
    with pytest.raises(IronCondorBuildError):
        build_iron_condor(no_greeks)


def test_build_iron_condor_rejects_no_wing_within_tolerance():
    # Chain with shorts but no wing strikes nearby.
    chain = [
        _opt(kind="put", strike=495, delta="-0.16", bid="0.65", ask="0.70"),
        _opt(kind="put", strike=480, delta="-0.02", bid="0.05", ask="0.10"),  # too far
        _opt(kind="call", strike=505, delta="0.16", bid="0.60", ask="0.65"),
        _opt(kind="call", strike=520, delta="0.02", bid="0.05", ask="0.10"),  # too far
    ]
    with pytest.raises(IronCondorBuildError, match="wing"):
        build_iron_condor(chain, wing_width=Decimal("5"))


def test_build_iron_condor_rejects_non_positive_credit():
    # Chain where wings cost more than shorts collect.
    chain = [
        _opt(kind="put", strike=495, delta="-0.16", bid="0.10", ask="0.12"),
        _opt(kind="put", strike=490, delta="-0.10", bid="1.50", ask="1.55"),
        _opt(kind="call", strike=505, delta="0.16", bid="0.10", ask="0.12"),
        _opt(kind="call", strike=510, delta="0.10", bid="1.50", ask="1.55"),
    ]
    with pytest.raises(IronCondorBuildError, match="credit"):
        build_iron_condor(chain, wing_width=Decimal("5"))


# ----------------- TradeOrder integration -----------------


def test_to_trade_order_produces_valid_option_order():
    plan = build_iron_condor(_normal_chain(), wing_width=Decimal("5"))
    order = plan.to_trade_order()
    assert order.asset_class is AssetClass.OPTION
    assert order.symbol == "SPY"
    assert order.strategy == "spy_0dte_ic"
    assert len(order.legs) == 4
    assert order.notional_usd == plan.max_loss
    # net-credit posture
    assert order.side is Side.SELL


def test_iron_condor_passes_defined_risk_check():
    plan = build_iron_condor(_normal_chain(), wing_width=Decimal("5"))
    ok, _why = _is_defined_risk(plan.to_trade_order())
    assert ok
