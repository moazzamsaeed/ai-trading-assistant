"""Command-line entry for the directional options backtest.

Usage:
  python -m backtests.directional_cli --start 2025-01-01 --end 2025-12-31 \\
      --mode aggressive --spy 500 --vol 0.15 --iv 0.18 --seed 42 --csv out.csv

Writes per-day results to a CSV (optional) and prints summary stats to stdout.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from backtests.directional_runner import (
    DirectionalBacktestConfig,
    compute_directional_stats,
    run_directional_backtest,
)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser(description="Directional options backtest (rule-based)")
    ap.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD")
    ap.add_argument("--mode", choices=["aggressive", "selective"], default="aggressive")
    ap.add_argument("--spy", type=float, default=500.0, help="SPY starting price")
    ap.add_argument("--vol", type=float, default=0.15, help="annual realized vol")
    ap.add_argument("--drift", type=float, default=0.0, help="annual drift")
    ap.add_argument("--iv", type=float, default=0.18, help="implied vol for BS pricing")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--size", type=float, default=750.0, help="position size USD per trade")
    ap.add_argument("--pt", type=float, default=None, help="profit target pct (e.g. 1.0 = 100 pct)")
    ap.add_argument("--sl", type=float, default=None, help="stop loss pct (e.g. 0.5 = 50 pct)")
    ap.add_argument("--csv", type=Path, default=None, help="optional output CSV path")
    args = ap.parse_args()

    # Mode-based exit defaults
    if args.mode == "aggressive":
        pt = args.pt if args.pt is not None else 1.0
        sl = args.sl if args.sl is not None else 0.5
    else:
        pt = args.pt if args.pt is not None else 0.5
        sl = args.sl if args.sl is not None else 0.3

    cfg = DirectionalBacktestConfig(
        start_date=args.start,
        end_date=args.end,
        mode=args.mode,
        spy_start=args.spy,
        annual_vol=args.vol,
        annual_drift=args.drift,
        iv=args.iv,
        seed=args.seed,
        position_size_usd=args.size,
        profit_target_pct=pt,
        stop_loss_pct=sl,
    )
    _results, df = run_directional_backtest(cfg)
    stats = compute_directional_stats(df)

    print(json.dumps(stats, indent=2))
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nPer-day results written to {args.csv}")


if __name__ == "__main__":
    main()
