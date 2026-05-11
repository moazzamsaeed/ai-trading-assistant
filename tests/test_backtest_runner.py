"""Multi-day runner + summary-stats tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backtests.runner import (
    BacktestConfig,
    compute_stats,
    results_to_dataframe,
    run_backtest,
)


def test_run_backtest_skips_weekends():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 4),  # Saturday
        end_date=date(2025, 1, 5),    # Sunday
    )
    results, df = run_backtest(cfg)
    assert results == []
    assert len(df) == 0


def test_run_backtest_one_week_produces_five_results():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 6),   # Monday
        end_date=date(2025, 1, 10),    # Friday
        spy_start=500,
        annual_vol=0.15,
        iv=0.20,
        seed=42,
    )
    results, df = run_backtest(cfg)
    assert len(results) == 5
    assert len(df) == 5
    # All five days should have entered with these parameters.
    assert df["entered"].sum() == 5


def test_dataframe_contains_required_columns():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        spy_start=500, annual_vol=0.15, iv=0.20, seed=42,
    )
    _, df = run_backtest(cfg)
    for col in (
        "date", "entered", "credit", "exit_reason", "pnl_per_contract",
        "short_put_strike", "short_call_strike",
    ):
        assert col in df.columns


def test_compute_stats_handles_empty_dataframe():
    df = results_to_dataframe([])
    stats = compute_stats(df)
    assert stats["n_entered"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl"] == 0.0


def test_compute_stats_full_dataset():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 2, 28),
        spy_start=500, annual_vol=0.15, iv=0.20, seed=42,
    )
    _, df = run_backtest(cfg)
    stats = compute_stats(df)
    assert stats["n_entered"] > 0
    assert 0.0 <= stats["win_rate"] <= 1.0
    # max_drawdown is non-positive by convention
    assert stats["max_drawdown"] <= 0
    # exit_reasons sums to n_entered
    total_reasons = sum(stats["exit_reasons"].values())
    assert total_reasons == stats["n_entered"]


def test_reproducibility_same_seed_same_results():
    cfg = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 31),
        spy_start=500, annual_vol=0.15, iv=0.20, seed=99,
    )
    _, df1 = run_backtest(cfg)
    _, df2 = run_backtest(cfg)
    assert list(df1["pnl_per_contract"]) == list(df2["pnl_per_contract"])


def test_higher_iv_widens_strikes():
    """At higher IV the synthetic chain quotes farther-OTM 16-delta strikes."""
    cfg_low = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        spy_start=500, iv=0.10, seed=42,
    )
    cfg_high = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        spy_start=500, iv=0.30, seed=42,
    )
    _, df_low = run_backtest(cfg_low)
    _, df_high = run_backtest(cfg_high)
    sp_low = df_low.iloc[0]["short_put_strike"]
    sp_high = df_high.iloc[0]["short_put_strike"]
    # higher IV → farther-OTM short (lower strike)
    assert sp_high < sp_low


def test_qty_scales_pnl_linearly_for_winners():
    """Increasing qty should multiply per-contract P&L unchanged."""
    base = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        spy_start=500, iv=0.20, seed=42, qty=1, wing_width=Decimal("5"),
    )
    big = BacktestConfig(
        start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        spy_start=500, iv=0.20, seed=42, qty=3, wing_width=Decimal("5"),
    )
    _, df_base = run_backtest(base)
    _, df_big = run_backtest(big)
    # per-contract P&L should be the same regardless of qty
    assert abs(df_base.iloc[0]["pnl_per_contract"] - df_big.iloc[0]["pnl_per_contract"]) < 0.01
