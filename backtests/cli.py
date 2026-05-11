"""Command-line entry for the iron-condor backtest.

Usage:
  python -m backtests.cli --start 2025-01-01 --end 2025-12-31 \
      --spy 500 --vol 0.15 --iv 0.18 --seed 42 --csv out.csv

Writes per-day results to a CSV and prints summary stats to stdout.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from backtests.runner import BacktestConfig, compute_stats, run_backtest


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser(description="SPY 0DTE iron-condor backtest")
    ap.add_argument("--start", type=_parse_date, required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", type=_parse_date, required=True, help="YYYY-MM-DD")
    ap.add_argument("--spy", type=float, default=500.0, help="SPY starting price")
    ap.add_argument(
        "--vol", type=float, default=0.15, help="annual realized vol (SPY GBM)"
    )
    ap.add_argument("--drift", type=float, default=0.0, help="annual drift")
    ap.add_argument(
        "--iv", type=float, default=0.18, help="implied vol for synthetic options"
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--wing", type=str, default="5", help="wing width in dollars")
    ap.add_argument(
        "--delta", type=str, default="0.16", help="target |delta| for short legs"
    )
    ap.add_argument("--csv", type=Path, default=None, help="optional output CSV")
    args = ap.parse_args()

    cfg = BacktestConfig(
        start_date=args.start,
        end_date=args.end,
        spy_start=args.spy,
        annual_vol=args.vol,
        annual_drift=args.drift,
        iv=args.iv,
        seed=args.seed,
        qty=args.qty,
        wing_width=Decimal(args.wing),
        target_short_abs_delta=Decimal(args.delta),
    )
    _results, df = run_backtest(cfg)
    stats = compute_stats(df)

    print(json.dumps(stats, indent=2))
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nPer-day results written to {args.csv}")


if __name__ == "__main__":
    main()
