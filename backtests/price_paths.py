"""Intraday SPY price-path generators for the backtest simulator.

Phase 2.4 ships a Geometric Brownian Motion generator only — fast,
deterministic-with-seed, no external data dependency. Once we have an
Alpaca historical-bars wrapper, a second `from_minute_bars()` generator
can drop in with the same `list[PriceTick]` shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import exp, sqrt
from random import Random


@dataclass(frozen=True)
class PriceTick:
    minutes_from_open: int  # minutes since the simulation's start time
    timestamp: datetime
    price: float  # plain float for fast inner-loop pricing; rounded for display

    @property
    def price_decimal(self) -> Decimal:
        return Decimal(f"{self.price:.2f}")


def generate_gbm_path(
    *,
    start_price: float,
    minutes: int,
    annual_vol: float,
    annual_drift: float = 0.0,
    step_minutes: int = 1,
    seed: int | None = None,
    start_time: datetime | None = None,
) -> list[PriceTick]:
    """Generate a GBM path for `minutes` total, sampled every `step_minutes`.

    `annual_vol` is the realized-vol estimate (~0.10–0.20 for SPY).
    `seed` makes the path reproducible.
    """
    rng = Random(seed) if seed is not None else Random()
    dt_years = (step_minutes / 60) / (24 * 365)
    sigma_step = annual_vol * sqrt(dt_years)
    drift_step = (annual_drift - annual_vol * annual_vol / 2) * dt_years

    start_time = start_time or datetime(2026, 5, 11, 13, 45, 0)
    n_steps = minutes // step_minutes
    ticks: list[PriceTick] = [
        PriceTick(minutes_from_open=0, timestamp=start_time, price=start_price)
    ]
    price = start_price
    for i in range(1, n_steps + 1):
        z = rng.gauss(0.0, 1.0)
        price *= exp(drift_step + sigma_step * z)
        ticks.append(
            PriceTick(
                minutes_from_open=i * step_minutes,
                timestamp=start_time + timedelta(minutes=i * step_minutes),
                price=price,
            )
        )
    return ticks


def constant_path(
    *, start_price: float, minutes: int, step_minutes: int = 1,
    start_time: datetime | None = None,
) -> list[PriceTick]:
    """Trivial path that doesn't move — useful for sanity testing the simulator."""
    start_time = start_time or datetime(2026, 5, 11, 13, 45, 0)
    n_steps = minutes // step_minutes
    return [
        PriceTick(
            minutes_from_open=i * step_minutes,
            timestamp=start_time + timedelta(minutes=i * step_minutes),
            price=start_price,
        )
        for i in range(n_steps + 1)
    ]
