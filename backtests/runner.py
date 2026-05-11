"""Multi-day backtest runner + summary statistics.

Iterates a date range, runs `simulate_one_day` against a fresh GBM path
per date, aggregates results into a pandas DataFrame, and produces a
small dict of summary stats (win rate, expectancy, max drawdown).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd

from backtests.price_paths import PriceTick, generate_gbm_path
from backtests.simulator import SimulationResult, simulate_one_day
from backtests.synthetic_options import DEFAULT_IV

# 9:45 ET → 16:00 ET = 375 minutes
TOTAL_MINUTES_IN_SESSION = 375


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    spy_start: float = 500.0
    annual_vol: float = 0.15
    annual_drift: float = 0.0
    iv: float = DEFAULT_IV
    seed: int = 42
    qty: int = 1
    wing_width: Decimal = Decimal("5")
    target_short_abs_delta: Decimal = Decimal("0.16")


def _trading_days(start: date, end: date) -> list[date]:
    """Mon–Fri only. Doesn't filter US holidays; close enough for synthetic backtests."""
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _path_for_day(
    sim_date: date,
    *,
    spy_start: float,
    annual_vol: float,
    annual_drift: float,
    seed: int,
) -> list[PriceTick]:
    return generate_gbm_path(
        start_price=spy_start,
        minutes=TOTAL_MINUTES_IN_SESSION,
        annual_vol=annual_vol,
        annual_drift=annual_drift,
        seed=seed,
        start_time=datetime.combine(sim_date, time(9, 45)),
    )


def run_backtest(cfg: BacktestConfig) -> tuple[list[SimulationResult], pd.DataFrame]:
    """Run the backtest day by day. Returns (raw results, summary DataFrame)."""
    days = _trading_days(cfg.start_date, cfg.end_date)
    results: list[SimulationResult] = []
    # Carry-over price between days simulates continuous SPY movement.
    spy_price = cfg.spy_start
    base_seed = cfg.seed

    for i, d in enumerate(days):
        path = _path_for_day(
            d,
            spy_start=spy_price,
            annual_vol=cfg.annual_vol,
            annual_drift=cfg.annual_drift,
            seed=base_seed + i,
        )
        r = simulate_one_day(
            sim_date=d,
            price_path=path,
            iv=cfg.iv,
            qty=cfg.qty,
            wing_width=cfg.wing_width,
            target_short_abs_delta=cfg.target_short_abs_delta,
        )
        results.append(r)
        spy_price = path[-1].price  # carry forward

    df = results_to_dataframe(results)
    return results, df


def results_to_dataframe(results: list[SimulationResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append(
            {
                "date": r.sim_date,
                "entered": r.entered,
                "not_entered_reason": r.not_entered_reason,
                "entry_spy": r.entry_spy,
                "credit": (
                    float(r.entry_credit_per_contract)
                    if r.entry_credit_per_contract
                    else None
                ),
                "short_put_strike": r.short_put_strike,
                "short_call_strike": r.short_call_strike,
                "exit_minute": r.exit_minute,
                "exit_spy": r.exit_spy,
                "exit_debit": (
                    float(r.exit_debit_per_contract)
                    if r.exit_debit_per_contract
                    else None
                ),
                "exit_reason": r.exit_reason,
                "pnl_per_contract": float(r.pnl_per_contract),
            }
        )
    return pd.DataFrame(rows)


def compute_stats(df: pd.DataFrame) -> dict:
    """Summary stats: win rate, avg win/loss, expectancy, max drawdown, etc."""
    n_total = len(df)
    if n_total == 0:
        return {
            "n_days": 0,
            "n_entered": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
        }
    entered = df[df["entered"]]
    n_entered = len(entered)
    if n_entered == 0:
        return {
            "n_days": n_total,
            "n_entered": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
        }

    wins = entered[entered["pnl_per_contract"] > 0]
    losses = entered[entered["pnl_per_contract"] <= 0]
    win_rate = len(wins) / n_entered
    avg_win = float(wins["pnl_per_contract"].mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses["pnl_per_contract"].mean()) if len(losses) > 0 else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    total_pnl = float(entered["pnl_per_contract"].sum())

    equity = entered["pnl_per_contract"].cumsum()
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0

    # Exit reason distribution
    reason_counts = entered["exit_reason"].value_counts().to_dict()

    return {
        "n_days": n_total,
        "n_entered": n_entered,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "exit_reasons": {k: int(v) for k, v in reason_counts.items()},
    }
