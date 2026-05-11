"""Tests for the single-day iron-condor backtest simulator."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backtests.price_paths import PriceTick, constant_path, generate_gbm_path
from backtests.simulator import simulate_one_day

# ----------------- price paths -----------------


def test_constant_path_doesnt_move():
    path = constant_path(start_price=500, minutes=60)
    assert all(t.price == 500 for t in path)
    assert path[0].minutes_from_open == 0
    assert path[-1].minutes_from_open == 60


def test_gbm_path_is_reproducible_with_seed():
    p1 = generate_gbm_path(start_price=500, minutes=60, annual_vol=0.15, seed=7)
    p2 = generate_gbm_path(start_price=500, minutes=60, annual_vol=0.15, seed=7)
    assert [t.price for t in p1] == [t.price for t in p2]


def test_gbm_path_first_tick_is_start_price():
    p = generate_gbm_path(start_price=500, minutes=60, annual_vol=0.15, seed=1)
    assert p[0].price == 500


# ----------------- simulator: constant SPY → profit target -----------------


def test_constant_spy_hits_profit_target():
    """When SPY doesn't move, theta decay shrinks the IC's exit debit
    smoothly toward zero and the 50% profit target should trigger before
    force-close."""
    path = constant_path(start_price=500, minutes=375)
    r = simulate_one_day(
        sim_date=date(2026, 5, 11), price_path=path, iv=0.20,
    )
    assert r.entered is True
    assert r.exit_reason == "profit_target_50pct"
    assert r.pnl_per_contract > 0


# ----------------- simulator: big up move → stop loss -----------------


def test_huge_up_move_triggers_stop_loss():
    """If SPY rips through the short call strike, the IC blows out and
    the 2× stop should fire."""
    base = constant_path(start_price=500, minutes=375)
    # Replace the second tick with a violent move so the stop fires early.
    new_ticks = [base[0]] + [
        PriceTick(
            minutes_from_open=t.minutes_from_open,
            timestamp=t.timestamp,
            price=520.0,  # 4% rip up, well through the short call
        )
        for t in base[1:]
    ]
    r = simulate_one_day(
        sim_date=date(2026, 5, 11), price_path=new_ticks, iv=0.20,
    )
    assert r.entered is True
    assert r.exit_reason == "stop_loss_2x"
    assert r.pnl_per_contract < 0


# ----------------- simulator: force-close -----------------


def test_force_close_when_no_threshold_hit():
    """A path that stays in a narrow range without hitting the 50% PT
    should force-close at the configured minute."""
    base = constant_path(start_price=500, minutes=375)
    # Walk price up just enough to slow theta decay → no PT hit, no stop.
    # We'll fake this by truncating force_close_after to fire early before
    # PT triggers.
    r = simulate_one_day(
        sim_date=date(2026, 5, 11),
        price_path=base,
        iv=0.20,
        force_close_after_minutes=1,  # force-close on the very first tick
    )
    assert r.entered is True
    assert r.exit_reason == "force_close"


# ----------------- simulator: empty path -----------------


def test_empty_path_returns_not_entered():
    r = simulate_one_day(sim_date=date(2026, 5, 11), price_path=[])
    assert r.entered is False
    assert r.pnl_per_contract == Decimal("0")


# ----------------- simulator: dataclass attrs -----------------


def test_winner_flag_matches_pnl_sign():
    path = constant_path(start_price=500, minutes=375)
    r = simulate_one_day(sim_date=date(2026, 5, 11), price_path=path, iv=0.20)
    assert r.is_winner == (r.pnl_per_contract > 0)
