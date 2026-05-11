"""Pydantic types shared across Hermes, agents, and the risk manager.

Every agent returns a `Signal`. If the signal proposes a trade, it carries
a `TradeOrder` (which may include `OptionLeg`s for defined-risk structures).
The risk manager rejects orders that violate hard constraints (D-001).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssetClass(StrEnum):
    EQUITY = "equity"
    OPTION = "option"
    CRYPTO = "crypto"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    HOLD = "hold"
    ALERT_ONLY = "alert_only"


class OptionLeg(BaseModel):
    model_config = ConfigDict(frozen=True)

    occ_symbol: str  # OCC option symbol, e.g. "SPY240315P00500000"
    side: Side
    qty: int = Field(gt=0, description="contracts, always positive; direction is `side`")
    strike: Decimal = Field(gt=0)
    expiry: date
    option_type: Literal["call", "put"]


class TradeOrder(BaseModel):
    """A proposed order, pre-risk-check.

    `notional_usd` is the cash the risk manager must reserve. For options
    spreads this is the max loss (defined risk). For spot crypto / equities
    it is qty × price.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    asset_class: AssetClass
    side: Side
    qty: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    legs: list[OptionLeg] | None = None
    strategy: str
    notional_usd: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _options_must_have_legs(self) -> TradeOrder:
        if self.asset_class is AssetClass.OPTION and not self.legs:
            raise ValueError("Option orders must declare legs (defined-risk only, D-001).")
        if self.asset_class is not AssetClass.OPTION and self.legs:
            raise ValueError("Legs are only valid for option orders.")
        return self


class Signal(BaseModel):
    """What every agent returns to Hermes."""

    model_config = ConfigDict(frozen=True)

    task_type: str
    agent: str
    action: SignalAction
    symbol: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning: str
    order: TradeOrder | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _order_required_for_open_close(self) -> Signal:
        if self.action in (SignalAction.OPEN, SignalAction.CLOSE) and self.order is None:
            raise ValueError(f"action={self.action} requires `order` to be set.")
        if self.action in (SignalAction.HOLD, SignalAction.ALERT_ONLY) and self.order is not None:
            raise ValueError(f"action={self.action} must not carry an `order`.")
        return self
