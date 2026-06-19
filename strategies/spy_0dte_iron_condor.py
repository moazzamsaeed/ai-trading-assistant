"""SPY 0DTE iron-condor leg construction.

Pure strategy logic — no Alpaca calls, no LLM calls. Takes a snapshot of
the options chain and produces a defined-risk four-leg structure plus
the credit and max-loss estimates the risk manager uses.

Defined risk: every short leg is paired with a long leg one wing-width
further OTM. Iron condor = short put spread + short call spread.

Phase 2.1 scope: leg construction + plan dataclass. Entry-decision logic
(should we open now?) lives in agents/options/strategist.py (Phase 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from integrations.alpaca_client import OptionQuote
from trademaster.logging import get_logger
from trademaster.models import AssetClass, OptionLeg, Side, TradeOrder

log = get_logger(__name__)

STRATEGY_NAME = "spy_0dte_ic"
HUNDRED = Decimal("100")  # contract multiplier for SPY options


class IronCondorBuildError(Exception):
    """Raised when the chain cannot support a valid iron condor."""


@dataclass(frozen=True)
class IronCondorPlan:
    """A constructed iron condor before risk validation."""

    short_put: OptionQuote
    long_put: OptionQuote
    short_call: OptionQuote
    long_call: OptionQuote
    qty: int
    credit_per_contract: Decimal  # net credit received (premium in)
    max_loss_per_contract: Decimal  # wing_width * 100 - credit
    wing_width: Decimal

    @property
    def credit_received(self) -> Decimal:
        return self.credit_per_contract * self.qty

    @property
    def max_loss(self) -> Decimal:
        return self.max_loss_per_contract * self.qty

    def to_trade_order(self) -> TradeOrder:
        """Convert the plan to a TradeOrder the risk manager can validate."""
        legs = [
            OptionLeg(
                occ_symbol=self.short_put.occ_symbol,
                side=Side.SELL,
                qty=self.qty,
                strike=self.short_put.strike,
                expiry=self.short_put.expiry,
                option_type="put",
            ),
            OptionLeg(
                occ_symbol=self.long_put.occ_symbol,
                side=Side.BUY,
                qty=self.qty,
                strike=self.long_put.strike,
                expiry=self.long_put.expiry,
                option_type="put",
            ),
            OptionLeg(
                occ_symbol=self.short_call.occ_symbol,
                side=Side.SELL,
                qty=self.qty,
                strike=self.short_call.strike,
                expiry=self.short_call.expiry,
                option_type="call",
            ),
            OptionLeg(
                occ_symbol=self.long_call.occ_symbol,
                side=Side.BUY,
                qty=self.qty,
                strike=self.long_call.strike,
                expiry=self.long_call.expiry,
                option_type="call",
            ),
        ]
        return TradeOrder(
            symbol=self.short_put.underlying,
            asset_class=AssetClass.OPTION,
            side=Side.SELL,  # net credit posture
            qty=Decimal(self.qty),
            limit_price=self.credit_per_contract,
            legs=legs,
            strategy=STRATEGY_NAME,
            notional_usd=self.max_loss,
        )


def _pick_short_leg(
    options: list[OptionQuote],
    *,
    option_type: str,
    target_abs_delta: Decimal,
) -> OptionQuote:
    """Pick the option whose |delta| is closest to `target_abs_delta`.

    For puts, alpaca-py returns negative delta; we compare against |delta|.
    """
    candidates = [
        q for q in options
        if q.option_type == option_type and q.delta is not None and q.bid > 0
    ]
    if not candidates:
        raise IronCondorBuildError(
            f"no quoted {option_type}s with greeks available"
        )
    best = min(candidates, key=lambda q: abs(abs(q.delta) - target_abs_delta))
    return best


def _pick_wing(
    options: list[OptionQuote],
    *,
    short_strike: Decimal,
    option_type: str,
    wing_width: Decimal,
) -> OptionQuote:
    """Find the long leg one wing-width further OTM than `short_strike`.

    For puts (further OTM = lower strike): target = short_strike - wing_width.
    For calls (further OTM = higher strike): target = short_strike + wing_width.
    Picks the closest available strike; raises if nothing within ±$0.50 of target.
    """
    target = (
        short_strike - wing_width if option_type == "put" else short_strike + wing_width
    )
    candidates = [
        q for q in options
        if q.option_type == option_type and q.ask > 0
    ]
    if not candidates:
        raise IronCondorBuildError(
            f"no quoted {option_type}s available for wing"
        )

    best = min(candidates, key=lambda q: abs(q.strike - target))
    if abs(best.strike - target) > Decimal("0.50"):
        raise IronCondorBuildError(
            f"no wing strike within $0.50 of {target} (closest: {best.strike})"
        )
    return best


def _closest_strike(
    options: list[OptionQuote], *, option_type: str, target: Decimal, side: str,
) -> OptionQuote:
    """Pick the quoted option whose strike is closest to `target`.

    `side` ∈ {"short","long"} only affects which quote side must be live:
    shorts must have a bid (we sell), longs an ask (we buy). Raises if the
    closest strike is more than $1 from target (chain too sparse)."""
    need = (lambda q: q.bid > 0) if side == "short" else (lambda q: q.ask > 0)
    cands = [q for q in options if q.option_type == option_type and need(q)]
    if not cands:
        raise IronCondorBuildError(f"no quoted {side} {option_type}s near {target}")
    best = min(cands, key=lambda q: abs(q.strike - target))
    if abs(best.strike - target) > Decimal("1.0"):
        raise IronCondorBuildError(
            f"no {option_type} strike within $1 of {target} (closest {best.strike})"
        )
    return best


def build_condor_at_strikes(
    chain: list[OptionQuote],
    *,
    short_put: Decimal,
    long_put: Decimal,
    short_call: Decimal,
    long_call: Decimal,
    qty: int = 1,
) -> IronCondorPlan:
    """Build an iron condor at EXPLICIT target strikes (from the deterministic
    condor_engine), picking the closest available chain strike for each leg.

    Unlike build_iron_condor (which selects shorts by delta), this matches the
    VIX1D-expected-move strikes the engine computed. Max loss uses the ACTUAL
    selected strikes (put/call spread widths may differ if the chain is sparse).
    """
    if qty <= 0:
        raise IronCondorBuildError(f"qty must be > 0, got {qty}")
    puts = [q for q in chain if q.option_type == "put"]
    calls = [q for q in chain if q.option_type == "call"]
    if not puts or not calls:
        raise IronCondorBuildError("chain missing puts or calls")

    sp = _closest_strike(puts, option_type="put", target=short_put, side="short")
    lp = _closest_strike(puts, option_type="put", target=long_put, side="long")
    sc = _closest_strike(calls, option_type="call", target=short_call, side="short")
    lc = _closest_strike(calls, option_type="call", target=long_call, side="long")
    if not (lp.strike < sp.strike < sc.strike < lc.strike):
        raise IronCondorBuildError(
            f"degenerate strikes after fill: {lp.strike}/{sp.strike}/{sc.strike}/{lc.strike}"
        )

    credit = (sp.mid + sc.mid) - (lp.mid + lc.mid)
    if credit <= 0:
        raise IronCondorBuildError(f"non-positive credit: {credit}")
    # max loss = wider of the two spread widths, minus credit (defined-risk)
    actual_wing = max(sp.strike - lp.strike, lc.strike - sc.strike)
    credit_per_contract = credit * HUNDRED
    max_loss_per_contract = (actual_wing * HUNDRED) - credit_per_contract
    if max_loss_per_contract <= 0:
        raise IronCondorBuildError(
            f"max loss not positive: wing={actual_wing}, credit={credit_per_contract}"
        )

    plan = IronCondorPlan(
        short_put=sp, long_put=lp, short_call=sc, long_call=lc, qty=qty,
        credit_per_contract=credit_per_contract,
        max_loss_per_contract=max_loss_per_contract,
        wing_width=actual_wing,
    )
    log.info(
        "condor_built_at_strikes",
        short_put_strike=str(sp.strike), short_call_strike=str(sc.strike),
        wing_width=str(actual_wing), qty=qty,
        credit_per_contract=str(credit_per_contract),
        max_loss_per_contract=str(max_loss_per_contract),
    )
    return plan


def build_iron_condor(
    chain: list[OptionQuote],
    *,
    qty: int = 1,
    target_short_abs_delta: Decimal = Decimal("0.16"),
    wing_width: Decimal = Decimal("5"),
) -> IronCondorPlan:
    """Build an iron-condor plan from the supplied option chain.

    Picks short put + short call at ~`target_short_abs_delta`, then wings
    one `wing_width` further OTM. Returns the plan with credit and max-loss
    computed from current mid-prices.

    Raises IronCondorBuildError if the chain cannot support the structure
    (insufficient strikes, missing greeks, or credit ≤ 0).
    """
    if qty <= 0:
        raise IronCondorBuildError(f"qty must be > 0, got {qty}")

    puts = [q for q in chain if q.option_type == "put"]
    calls = [q for q in chain if q.option_type == "call"]
    if not puts or not calls:
        raise IronCondorBuildError("chain missing puts or calls")

    short_put = _pick_short_leg(puts, option_type="put", target_abs_delta=target_short_abs_delta)
    short_call = _pick_short_leg(
        calls, option_type="call", target_abs_delta=target_short_abs_delta
    )
    long_put = _pick_wing(
        puts, short_strike=short_put.strike, option_type="put", wing_width=wing_width,
    )
    long_call = _pick_wing(
        calls, short_strike=short_call.strike, option_type="call", wing_width=wing_width,
    )

    # Credit = (short put mid + short call mid) - (long put mid + long call mid)
    credit = (short_put.mid + short_call.mid) - (long_put.mid + long_call.mid)
    if credit <= 0:
        raise IronCondorBuildError(f"non-positive credit: {credit}")

    credit_per_contract = credit * HUNDRED  # options multiplier
    max_loss_per_contract = (wing_width * HUNDRED) - credit_per_contract
    if max_loss_per_contract <= 0:
        raise IronCondorBuildError(
            f"max loss not positive: wing={wing_width}, credit={credit_per_contract}"
        )

    plan = IronCondorPlan(
        short_put=short_put,
        long_put=long_put,
        short_call=short_call,
        long_call=long_call,
        qty=qty,
        credit_per_contract=credit_per_contract,
        max_loss_per_contract=max_loss_per_contract,
        wing_width=wing_width,
    )
    log.info(
        "iron_condor_built",
        underlying=short_put.underlying,
        expiry=str(short_put.expiry),
        short_put_strike=str(short_put.strike),
        short_call_strike=str(short_call.strike),
        wing_width=str(wing_width),
        qty=qty,
        credit_per_contract=str(credit_per_contract),
        max_loss_per_contract=str(max_loss_per_contract),
    )
    return plan
