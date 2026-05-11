"""Single-day iron-condor backtest simulator.

Walks one intraday price path. At each tick after entry, re-prices the
four IC legs from the current spot using Black-Scholes, computes net
exit debit, and applies the same exit rules the live system uses:

- 50% profit target: exit when net debit ≤ entry credit / 2
- 2× stop loss: exit when net debit ≥ entry credit × 3
- Force close at the configured cutoff (15:50 ET in production)

Reuses `build_iron_condor` from strategies/ so we test the actual
production leg-selection code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal

from backtests.price_paths import PriceTick
from backtests.synthetic_options import (
    DEFAULT_IV,
    DEFAULT_RISK_FREE,
    bs_call_price,
    bs_put_price,
    generate_chain,
)
from strategies.spy_0dte_iron_condor import (
    IronCondorBuildError,
    IronCondorPlan,
    build_iron_condor,
)

# Entry time: 9:45 ET, 6h 15m before close
ENTRY_TIME_ET = time(9, 45)
# Force-close: 15:50 ET, 10 minutes before close
FORCE_CLOSE_TIME_ET = time(15, 50)
MARKET_CLOSE_TIME_ET = time(16, 0)


@dataclass(frozen=True)
class SimulationResult:
    """One day's iron-condor backtest outcome."""

    sim_date: date
    entered: bool
    not_entered_reason: str | None
    entry_spy: float | None
    entry_credit_per_contract: Decimal | None
    short_put_strike: float | None
    short_call_strike: float | None
    exit_minute: int | None
    exit_spy: float | None
    exit_debit_per_contract: Decimal | None
    exit_reason: str | None
    pnl_per_contract: Decimal

    @property
    def is_winner(self) -> bool:
        return self.entered and self.pnl_per_contract > 0


def _current_exit_debit(
    *,
    spot: float,
    plan: IronCondorPlan,
    hours_to_expiry: float,
    iv: float,
    risk_free: float,
) -> Decimal:
    """Compute the per-contract debit needed to close the IC at the current spot."""
    T = max(hours_to_expiry, 0.0) / (24 * 365)
    sp_strike = float(plan.short_put.strike)
    lp_strike = float(plan.long_put.strike)
    sc_strike = float(plan.short_call.strike)
    lc_strike = float(plan.long_call.strike)
    sp_mid = bs_put_price(spot, sp_strike, T, risk_free, iv)
    lp_mid = bs_put_price(spot, lp_strike, T, risk_free, iv)
    sc_mid = bs_call_price(spot, sc_strike, T, risk_free, iv)
    lc_mid = bs_call_price(spot, lc_strike, T, risk_free, iv)
    # Closing the IC: buy back shorts, sell longs. Debit per share:
    debit_per_share = (sp_mid + sc_mid) - (lp_mid + lc_mid)
    return Decimal(f"{max(debit_per_share, 0.0) * 100:.2f}")


def _should_exit(
    *,
    credit_received: Decimal,
    exit_debit: Decimal,
    force: bool,
) -> tuple[bool, str]:
    if force:
        return True, "force_close"
    if exit_debit <= credit_received / Decimal("2"):
        return True, "profit_target_50pct"
    if exit_debit >= credit_received * Decimal("3"):
        return True, "stop_loss_2x"
    return False, ""


def _hours_to_close(minutes_from_open: int, total_minutes: int) -> float:
    remaining = max(total_minutes - minutes_from_open, 0)
    return remaining / 60.0


def simulate_one_day(
    *,
    sim_date: date,
    price_path: list[PriceTick],
    iv: float = DEFAULT_IV,
    risk_free: float = DEFAULT_RISK_FREE,
    target_short_abs_delta: Decimal = Decimal("0.16"),
    wing_width: Decimal = Decimal("5"),
    qty: int = 1,
    force_close_after_minutes: int | None = None,
) -> SimulationResult:
    """Run one day's iron-condor entry + exit simulation against `price_path`.

    `price_path[0]` is the entry tick (assumed 9:45 ET). The remaining ticks
    walk forward in time; `force_close_after_minutes` (default: until 15:50,
    i.e. 365 minutes after 9:45) overrides the live PT/stop logic.
    """
    if not price_path:
        return SimulationResult(
            sim_date=sim_date, entered=False,
            not_entered_reason="empty price path",
            entry_spy=None, entry_credit_per_contract=None,
            short_put_strike=None, short_call_strike=None,
            exit_minute=None, exit_spy=None,
            exit_debit_per_contract=None, exit_reason=None,
            pnl_per_contract=Decimal("0"),
        )

    total_minutes = price_path[-1].minutes_from_open
    if force_close_after_minutes is None:
        # 9:45 → 15:50 ET = 365 minutes
        force_close_after_minutes = 365

    entry = price_path[0]
    entry_chain = generate_chain(
        spy_price=entry.price,
        expiry=sim_date,
        hours_to_expiry=_hours_to_close(entry.minutes_from_open, total_minutes),
        iv=iv,
        risk_free=risk_free,
    )
    try:
        plan = build_iron_condor(
            entry_chain,
            qty=qty,
            target_short_abs_delta=target_short_abs_delta,
            wing_width=wing_width,
        )
    except IronCondorBuildError as e:
        return SimulationResult(
            sim_date=sim_date, entered=False,
            not_entered_reason=f"build failed: {e}",
            entry_spy=entry.price, entry_credit_per_contract=None,
            short_put_strike=None, short_call_strike=None,
            exit_minute=None, exit_spy=None,
            exit_debit_per_contract=None, exit_reason=None,
            pnl_per_contract=Decimal("0"),
        )

    credit = plan.credit_per_contract
    sp_strike = float(plan.short_put.strike)
    sc_strike = float(plan.short_call.strike)

    # Walk the remaining ticks.
    for tick in price_path[1:]:
        force = tick.minutes_from_open >= force_close_after_minutes
        hours = _hours_to_close(tick.minutes_from_open, total_minutes)
        exit_debit = _current_exit_debit(
            spot=tick.price, plan=plan, hours_to_expiry=hours,
            iv=iv, risk_free=risk_free,
        )
        do_exit, reason = _should_exit(
            credit_received=credit, exit_debit=exit_debit, force=force,
        )
        if do_exit:
            pnl = (credit - exit_debit).quantize(Decimal("0.01"))
            return SimulationResult(
                sim_date=sim_date,
                entered=True,
                not_entered_reason=None,
                entry_spy=entry.price,
                entry_credit_per_contract=credit,
                short_put_strike=sp_strike,
                short_call_strike=sc_strike,
                exit_minute=tick.minutes_from_open,
                exit_spy=tick.price,
                exit_debit_per_contract=exit_debit,
                exit_reason=reason,
                pnl_per_contract=pnl,
            )

    # Hit end of path without an exit signal — settle at intrinsic value.
    final = price_path[-1]
    intrinsic_debit = _current_exit_debit(
        spot=final.price, plan=plan, hours_to_expiry=0,
        iv=iv, risk_free=risk_free,
    )
    pnl = (credit - intrinsic_debit).quantize(Decimal("0.01"))
    return SimulationResult(
        sim_date=sim_date,
        entered=True,
        not_entered_reason=None,
        entry_spy=entry.price,
        entry_credit_per_contract=credit,
        short_put_strike=sp_strike,
        short_call_strike=sc_strike,
        exit_minute=final.minutes_from_open,
        exit_spy=final.price,
        exit_debit_per_contract=intrinsic_debit,
        exit_reason="expiry_settle",
        pnl_per_contract=pnl,
    )
