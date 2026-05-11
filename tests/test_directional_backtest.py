"""Tests for the directional options backtest engine."""

from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd
import pytest

from backtests.directional_runner import (
    DirectionalBacktestConfig,
    _choose_strike,
    _option_price,
    _rule_decision,
    _ticks_to_bars,
    compute_directional_stats,
    run_directional_backtest,
    simulate_directional_day,
)
from backtests.price_paths import PriceTick, constant_path, generate_gbm_path


def _ticks(prices: list[float]) -> list[PriceTick]:
    start = datetime(2026, 1, 2, 9, 45)
    return [
        PriceTick(i, start.replace(minute=start.minute + i % 60, hour=start.hour + i // 60), p)
        for i, p in enumerate(prices)
    ]


# ---------------------------------------------------------------------------
# _ticks_to_bars
# ---------------------------------------------------------------------------


def test_ticks_to_bars_ohlcv():
    prices = [100.0, 100.1, 100.3, 100.2, 100.5,  # bar 0: o=100, h=100.5, l=100, c=100.5
              100.4, 100.0, 99.8, 100.1, 100.2]   # bar 1: o=100.4, h=100.4, l=99.8, c=100.2
    ticks = _ticks(prices)
    bars = _ticks_to_bars(ticks, bar_size_min=5)
    assert len(bars) == 2
    b0 = bars[0]
    assert float(b0.open) == pytest.approx(100.0)
    assert float(b0.high) == pytest.approx(100.5)
    assert float(b0.low) == pytest.approx(100.0)
    assert float(b0.close) == pytest.approx(100.5)


def test_ticks_to_bars_uses_supplied_volumes():
    prices = [100.0] * 10
    ticks = _ticks(prices)
    bars = _ticks_to_bars(ticks, bar_size_min=5, volumes=[500, 1500])
    assert bars[0].volume == 500
    assert bars[1].volume == 1500


def test_ticks_to_bars_incomplete_last_chunk_dropped():
    prices = [100.0] * 11  # 11 ticks → 2 full bars (10 used), 1 leftover tick
    ticks = _ticks(prices)
    bars = _ticks_to_bars(ticks, bar_size_min=5)
    assert len(bars) == 2


# ---------------------------------------------------------------------------
# _choose_strike
# ---------------------------------------------------------------------------


def test_choose_strike_aggressive_atm():
    assert _choose_strike(500.3, "BUY_CALL", "aggressive") == 500.0
    assert _choose_strike(500.7, "BUY_PUT", "aggressive") == 501.0


def test_choose_strike_selective_1otm():
    assert _choose_strike(500.3, "BUY_CALL", "selective") == 501.0
    assert _choose_strike(500.3, "BUY_PUT", "selective") == 499.0


# ---------------------------------------------------------------------------
# _option_price
# ---------------------------------------------------------------------------


def test_option_price_call_positive():
    price = _option_price("BUY_CALL", 500.0, 500.0, 200, 0.18)
    assert 0.0 < price < 50.0


def test_option_price_put_positive():
    price = _option_price("BUY_PUT", 500.0, 500.0, 200, 0.18)
    assert 0.0 < price < 50.0


def test_option_price_decreases_with_less_time():
    more_time = _option_price("BUY_CALL", 500.0, 500.0, 300, 0.18)
    less_time = _option_price("BUY_CALL", 500.0, 500.0, 100, 0.18)
    assert more_time > less_time


def test_option_price_itm_call_more_than_otm():
    itm = _option_price("BUY_CALL", 505.0, 500.0, 200, 0.18)
    otm = _option_price("BUY_CALL", 495.0, 500.0, 200, 0.18)
    assert itm > otm


# ---------------------------------------------------------------------------
# _rule_decision
# ---------------------------------------------------------------------------


def _make_bar(close: float, vol: int = 10_000) -> object:
    """Quick Bar-like for rule_decision tests."""
    from decimal import Decimal

    from integrations.alpaca_client import Bar

    return Bar(
        timestamp=datetime(2026, 1, 2, 10, 0),
        open=Decimal(f"{close - 0.1:.2f}"),
        high=Decimal(f"{close + 0.3:.2f}"),
        low=Decimal(f"{close - 0.3:.2f}"),
        close=Decimal(f"{close:.2f}"),
        volume=vol,
        vwap=Decimal(f"{close:.2f}"),
    )


def test_rule_hold_when_too_few_bars():
    bars = [_make_bar(500.0)] * 5  # way below MIN_BARS
    assert _rule_decision(bars) == "HOLD"


def test_rule_hold_on_flat_path():
    # Constant price → vol_ratio = 1.0 (< 1.3), never triggers
    bars = [_make_bar(500.0, vol=10_000)] * 50
    assert _rule_decision(bars) == "HOLD"


# ---------------------------------------------------------------------------
# simulate_directional_day
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> DirectionalBacktestConfig:
    defaults = dict(
        start_date=date(2026, 1, 2),
        end_date=date(2026, 1, 2),
    )
    defaults.update(kwargs)
    return DirectionalBacktestConfig(**defaults)


def test_simulate_no_entry_on_flat_path():
    path = constant_path(
        start_price=500.0, minutes=375,
        start_time=datetime.combine(date(2026, 1, 2), time(9, 45)),
    )
    r = simulate_directional_day(sim_date=date(2026, 1, 2), price_path=path, cfg=_cfg())
    assert not r.entered
    assert r.exit_reason == "no_entry"
    assert r.pnl_usd == 0.0


def test_simulate_result_fields_when_entered():
    path = generate_gbm_path(
        start_price=500.0, minutes=375, annual_vol=0.15, seed=42,
        start_time=datetime.combine(date(2026, 1, 2), time(9, 45)),
    )
    # Use high-variance volumes to make signals more likely
    from random import Random
    rng = Random(99)
    volumes = [max(100, int(rng.gauss(10_000, 5_000))) for _ in range(75)]
    r = simulate_directional_day(
        sim_date=date(2026, 1, 2), price_path=path, cfg=_cfg(), volumes=volumes
    )
    if r.entered:
        assert r.action in ("BUY_CALL", "BUY_PUT")
        assert r.exit_reason in ("profit_target", "stop_loss", "force_close")
        assert r.entry_premium is not None and r.entry_premium > 0
        assert r.exit_premium is not None and r.exit_premium > 0
        expected_pct = (r.exit_premium - r.entry_premium) / r.entry_premium
        assert abs(r.pnl_pct - expected_pct) < 0.01


def test_simulate_profit_target_caps_gain():
    path = generate_gbm_path(
        start_price=500.0, minutes=375, annual_vol=0.15, seed=42,
        start_time=datetime.combine(date(2026, 1, 2), time(9, 45)),
    )
    from random import Random
    rng = Random(99)
    volumes = [max(100, int(rng.gauss(10_000, 5_000))) for _ in range(75)]
    cfg = _cfg(profit_target_pct=1.0, stop_loss_pct=0.5)
    r = simulate_directional_day(sim_date=date(2026, 1, 2), price_path=path, cfg=cfg, volumes=volumes)
    if r.entered and r.exit_reason == "profit_target":
        assert r.pnl_pct >= 0.95  # near or at 100%


def test_simulate_stop_loss_limits_loss():
    path = generate_gbm_path(
        start_price=500.0, minutes=375, annual_vol=0.15, seed=42,
        start_time=datetime.combine(date(2026, 1, 2), time(9, 45)),
    )
    from random import Random
    rng = Random(99)
    volumes = [max(100, int(rng.gauss(10_000, 5_000))) for _ in range(75)]
    cfg = _cfg(profit_target_pct=1.0, stop_loss_pct=0.5)
    r = simulate_directional_day(sim_date=date(2026, 1, 2), price_path=path, cfg=cfg, volumes=volumes)
    if r.entered and r.exit_reason == "stop_loss":
        assert r.pnl_pct <= -0.45  # near or at -50%


# ---------------------------------------------------------------------------
# run_directional_backtest
# ---------------------------------------------------------------------------


def test_run_backtest_returns_one_row_per_trading_day():
    cfg = DirectionalBacktestConfig(
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 9),  # Mon–Fri = 5 trading days
    )
    results, df = run_directional_backtest(cfg)
    assert len(results) == 5
    assert len(df) == 5
    assert set(df.columns) >= {"date", "entered", "action", "pnl_usd", "exit_reason"}


def test_run_backtest_skips_weekends():
    # Jan 4-5 2026 = Sat/Sun
    cfg = DirectionalBacktestConfig(
        start_date=date(2026, 1, 3),  # Sat
        end_date=date(2026, 1, 4),   # Sun
    )
    _results, df = run_directional_backtest(cfg)
    assert len(df) == 0  # no trading days


def test_run_backtest_carries_spy_price():
    cfg = DirectionalBacktestConfig(
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 9),
        spy_start=400.0,
        seed=7,
    )
    results, _df = run_directional_backtest(cfg)
    # Day 1 entry_spy should be near 400; subsequent days reflect carry-over
    spys = [r.entry_spy for r in results if r.entered]
    if spys:
        assert any(abs(s - 400.0) < 50.0 for s in spys)  # not wildly different


# ---------------------------------------------------------------------------
# compute_directional_stats
# ---------------------------------------------------------------------------


def test_stats_empty():
    df = pd.DataFrame(columns=["entered", "pnl_usd", "exit_reason"])
    s = compute_directional_stats(df)
    assert s["n_days"] == 0
    assert s["win_rate"] == 0.0


def test_stats_no_entries():
    df = pd.DataFrame({"entered": [False] * 5, "pnl_usd": [0.0] * 5, "exit_reason": ["no_entry"] * 5})
    s = compute_directional_stats(df)
    assert s["n_entered"] == 0
    assert s["expectancy_usd"] == 0.0


def test_stats_all_wins():
    df = pd.DataFrame({
        "entered": [True] * 3,
        "pnl_usd": [100.0, 200.0, 150.0],
        "exit_reason": ["profit_target"] * 3,
    })
    s = compute_directional_stats(df)
    assert s["win_rate"] == pytest.approx(1.0)
    assert s["expectancy_usd"] == pytest.approx(150.0)
    assert s["total_pnl_usd"] == pytest.approx(450.0)
    assert s["max_drawdown_usd"] == pytest.approx(0.0)


def test_stats_mixed():
    df = pd.DataFrame({
        "entered": [True] * 4,
        "pnl_usd": [200.0, -100.0, 150.0, -50.0],
        "exit_reason": ["profit_target", "stop_loss", "profit_target", "stop_loss"],
    })
    s = compute_directional_stats(df)
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_win_usd"] == pytest.approx(175.0)
    assert s["avg_loss_usd"] == pytest.approx(-75.0)
    assert s["expectancy_usd"] == pytest.approx(50.0)
    assert s["exit_reasons"]["profit_target"] == 2
    assert s["exit_reasons"]["stop_loss"] == 2


def test_stats_max_drawdown():
    df = pd.DataFrame({
        "entered": [True] * 4,
        "pnl_usd": [100.0, -200.0, 50.0, -100.0],
        "exit_reason": ["profit_target", "stop_loss", "profit_target", "stop_loss"],
    })
    s = compute_directional_stats(df)
    # equity: 100, -100, -50, -150 → running_max: 100, 100, 100, 100 → dd: 0,-200,-150,-250
    assert s["max_drawdown_usd"] == pytest.approx(-250.0)
