"""Signal / TradeOrder / OptionLeg validation tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from traderouter.models import (
    AssetClass,
    OptionLeg,
    Side,
    Signal,
    SignalAction,
    TradeOrder,
)


def _equity_order() -> TradeOrder:
    return TradeOrder(
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        qty=Decimal("10"),
        limit_price=Decimal("180.50"),
        strategy="vwap_reclaim",
        notional_usd=Decimal("1805.00"),
    )


def _iron_condor_order() -> TradeOrder:
    legs = [
        OptionLeg(
            occ_symbol="SPY240315P00495000",
            side=Side.SELL,
            qty=1,
            strike=Decimal("495"),
            expiry=date(2024, 3, 15),
            option_type="put",
        ),
        OptionLeg(
            occ_symbol="SPY240315P00490000",
            side=Side.BUY,
            qty=1,
            strike=Decimal("490"),
            expiry=date(2024, 3, 15),
            option_type="put",
        ),
        OptionLeg(
            occ_symbol="SPY240315C00505000",
            side=Side.SELL,
            qty=1,
            strike=Decimal("505"),
            expiry=date(2024, 3, 15),
            option_type="call",
        ),
        OptionLeg(
            occ_symbol="SPY240315C00510000",
            side=Side.BUY,
            qty=1,
            strike=Decimal("510"),
            expiry=date(2024, 3, 15),
            option_type="call",
        ),
    ]
    return TradeOrder(
        symbol="SPY",
        asset_class=AssetClass.OPTION,
        side=Side.SELL,
        qty=Decimal("1"),
        strategy="spy_0dte_ic",
        notional_usd=Decimal("380"),
        legs=legs,
    )


def test_equity_order_valid():
    o = _equity_order()
    assert o.asset_class is AssetClass.EQUITY


def test_iron_condor_order_valid():
    o = _iron_condor_order()
    assert len(o.legs) == 4


def test_option_order_without_legs_rejected():
    with pytest.raises(ValidationError) as exc:
        TradeOrder(
            symbol="SPY",
            asset_class=AssetClass.OPTION,
            side=Side.SELL,
            qty=Decimal("1"),
            strategy="spy_0dte_ic",
            notional_usd=Decimal("380"),
        )
    assert "legs" in str(exc.value).lower()


def test_equity_order_with_legs_rejected():
    with pytest.raises(ValidationError):
        TradeOrder(
            symbol="AAPL",
            asset_class=AssetClass.EQUITY,
            side=Side.BUY,
            qty=Decimal("10"),
            strategy="vwap_reclaim",
            notional_usd=Decimal("1805"),
            legs=[
                OptionLeg(
                    occ_symbol="X",
                    side=Side.BUY,
                    qty=1,
                    strike=Decimal("1"),
                    expiry=date(2024, 1, 1),
                    option_type="call",
                )
            ],
        )


def test_open_signal_requires_order():
    with pytest.raises(ValidationError):
        Signal(
            task_type="intraday_scan",
            agent="options",
            action=SignalAction.OPEN,
            reasoning="...",
        )


def test_alert_only_signal_must_not_have_order():
    with pytest.raises(ValidationError):
        Signal(
            task_type="intraday_scan",
            agent="equity_alerts",
            action=SignalAction.ALERT_ONLY,
            reasoning="VWAP reclaim",
            order=_equity_order(),
        )


def test_hold_signal_valid():
    s = Signal(
        task_type="intraday_scan",
        agent="options",
        action=SignalAction.HOLD,
        reasoning="IV rank too low",
    )
    assert s.order is None


def test_open_signal_with_order_valid():
    s = Signal(
        task_type="options_strategy",
        agent="options",
        action=SignalAction.OPEN,
        symbol="SPY",
        confidence=0.7,
        reasoning="IV rank 65, range-bound",
        order=_iron_condor_order(),
    )
    assert s.order is not None
    assert s.confidence == 0.7


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Signal(
            task_type="x",
            agent="x",
            action=SignalAction.HOLD,
            reasoning="...",
            confidence=1.5,
        )
