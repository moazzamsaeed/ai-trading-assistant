#!/usr/bin/env python3
"""Validate recently-closed trades for silent-failure patterns.

Built after the W21 review surfaced two silent failures (missing
peak_pnl_pct on losing trades; missing original_qty/scale_out_tiers_fired
on trade #38). Designed to catch regressions early, before another weekly
review is needed.

Default behavior: read watermark file, scan trades closed since, output a
markdown report if any issues found, exit 1 — else exit 0 silently. Suited
for Hermes cron with `--no-agent --script`: empty stdout = silent delivery;
report = posted to Discord.

Usage:
    .venv/bin/python scripts/trade_health_check.py                   # cron-friendly
    .venv/bin/python scripts/trade_health_check.py --all             # full audit
    .venv/bin/python scripts/trade_health_check.py --since 2026-05-22
    .venv/bin/python scripts/trade_health_check.py --no-mark         # don't update watermark
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
DB = PROJECT_ROOT / "data" / "trademaster.db"
WATERMARK = PROJECT_ROOT / "data" / ".health_check_watermark"

# Trades opened on or after this date are expected to have the post-rewrite
# fields (original_qty, peak_pnl_pct initialized, conviction set, etc.).
# Set to the date the rebuilt daemon went live with the full architecture.
# Override per-run with --cutoff if you want to retroactively audit older trades.
DEFAULT_CUTOFF_DATE = date(2026, 5, 26)

ET = ZoneInfo("America/New_York")

# Tiers we expect to fire if peak crosses them. Mirrors the sell tiers in
# DEFAULT_TRAILING_STOP_LEVELS (agents/directional/exit_monitor.py) — kept in
# sync manually. Values are in PERCENT (25.0 = +25%), matching how peak_pnl_pct
# and scale_out_tiers_fired are persisted (exit_monitor appends raw trigger_pct).
# Retuned 2026-06-05: ladder moved 15/30/50 → 25/50 (sell tiers only).
TIER_THRESHOLDS = [25.0, 50.0]  # tiers that have sell_frac > 0


# ---------------------------------------------------------------------------
# Watermark — last checked trade ID, persisted between runs
# ---------------------------------------------------------------------------


def load_watermark() -> int:
    if not WATERMARK.exists():
        return 0
    try:
        return int(WATERMARK.read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def save_watermark(trade_id: int) -> None:
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK.write_text(str(trade_id))


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------


def fetch_closed_trades(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    since_dt: datetime | None,
) -> list[dict]:
    cur = conn.cursor()
    sql = """
        SELECT id, opened_at, closed_at, symbol, strategy, qty,
               entry_price, exit_price, realized_pnl_usd, extra
        FROM trades
        WHERE closed_at IS NOT NULL
          AND id > ?
    """
    params: list = [after_id]
    if since_dt is not None:
        sql += " AND closed_at >= ?"
        params.append(_sql_dt(since_dt))
    sql += " ORDER BY id"
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = []
    for raw in cur.fetchall():
        rec = dict(zip(cols, raw, strict=True))
        try:
            rec["extra"] = json.loads(rec["extra"]) if rec["extra"] else {}
        except json.JSONDecodeError:
            rec["extra"] = {}
        rows.append(rec)
    return rows


def _sql_dt(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _opened_date(trade: dict) -> date:
    return datetime.fromisoformat(trade["opened_at"]).date()


# ---------------------------------------------------------------------------
# Checks — each returns a list of issue messages (empty if OK)
# ---------------------------------------------------------------------------


def check_missing_original_qty(trade: dict, cutoff: date) -> list[str]:
    if _opened_date(trade) < cutoff:
        return []
    if trade["extra"].get("original_qty") is None:
        return [
            "MISSING `original_qty` at entry — _persist_entry regression? "
            "Scale-out math will fall back to current qty as denominator."
        ]
    return []


def check_missing_peak_pnl_pct(trade: dict, cutoff: date) -> list[str]:
    if _opened_date(trade) < cutoff:
        return []
    if "peak_pnl_pct" not in trade["extra"]:
        return [
            "MISSING `peak_pnl_pct` field entirely — Bug B init regression? "
            "Should be initialized to 0.0 at entry in _persist_entry."
        ]
    return []


def check_missing_conviction(trade: dict, cutoff: date) -> list[str]:
    if _opened_date(trade) < cutoff:
        return []
    conv = trade["extra"].get("conviction")
    if conv not in ("HIGH", "MEDIUM", "LOW"):
        return [
            f"INVALID `conviction`={conv!r} — should be HIGH/MEDIUM/LOW. "
            "Aggressive-mode gate may be misfiring."
        ]
    return []


def check_missing_exit_reason(trade: dict, cutoff: date) -> list[str]:
    # Applies to all trades regardless of cutoff — exit_reason should always be set.
    _ = cutoff
    if not trade["extra"].get("exit_reason"):
        return ["MISSING `exit_reason` on closed trade — observability gap."]
    return []


def check_scale_out_tier_misses(trade: dict, cutoff: date) -> list[str]:
    """If peak crossed a tier with sell_frac > 0 but we have no record of
    that tier firing, either the scale-out failed or the persistence failed.
    Either way, the trade locked less than designed."""
    if _opened_date(trade) < cutoff:
        return []
    peak = trade["extra"].get("peak_pnl_pct")
    if peak is None:
        return []  # caught by missing_peak_pnl_pct check
    peak_pct = float(peak)  # already in percent units (15.0 = +15%)
    # exit_monitor appends the raw trigger_pct (e.g. 15.0) — coerce to float so
    # equality holds regardless of whether JSON stored int or float.
    fired = trade["extra"].get("scale_out_tiers_fired") or []
    fired_set = {float(f) for f in fired}

    missed: list[str] = []
    for tier in TIER_THRESHOLDS:
        if peak_pct >= tier and tier not in fired_set:
            missed.append(f"{int(tier)}%")

    if not missed:
        return []
    return [
        f"PEAK +{float(peak):.1f}% crossed tier(s) {', '.join(missed)} "
        f"but tier(s) didn't fire — fired={fired}. "
        f"Either _maybe_scale_out failed silently OR fast reversal between "
        f"30s ticks. Check `scale_out_executed` / `scale_out_failed` log lines."
    ]


def check_peak_realized_gap(trade: dict, cutoff: date) -> list[str]:
    """Winner that peaked well above realized — sanity check on whether
    scale-out is doing its job. Informational, not necessarily a bug."""
    if _opened_date(trade) < cutoff:
        return []
    peak = trade["extra"].get("peak_pnl_pct")
    pnl = trade.get("realized_pnl_usd")
    entry = trade.get("entry_price")
    qty = trade["extra"].get("original_qty") or trade.get("qty")
    if peak is None or pnl is None or entry is None or qty is None:
        return []
    peak_f = float(peak)
    if peak_f < 30:
        return []  # only flag big-peak trades
    entry_f = float(entry)
    qty_i = int(qty)
    peak_usd_potential = (entry_f * (peak_f / 100.0)) * qty_i * 100
    pnl_f = float(pnl)
    if peak_usd_potential <= 0:
        return []
    capture_pct = pnl_f / peak_usd_potential * 100
    if capture_pct < 30:  # locked less than 30% of peak potential
        return [
            f"LARGE PEAK-VS-REALIZED GAP — peak +{peak_f:.1f}% "
            f"(~${peak_usd_potential:.0f} max), realized ${pnl_f:.2f} "
            f"({capture_pct:.0f}% of peak captured). "
            f"Worth a manual review — was scale-out timing the issue?"
        ]
    return []


CHECKS: list[Callable[[dict, date], list[str]]] = [
    check_missing_original_qty,
    check_missing_peak_pnl_pct,
    check_missing_conviction,
    check_missing_exit_reason,
    check_scale_out_tier_misses,
    check_peak_realized_gap,
]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(findings: list[tuple[dict, list[str]]], scanned: int) -> str:
    if not findings:
        return ""

    n_trades = len(findings)
    n_issues = sum(len(msgs) for _, msgs in findings)
    et_now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    lines = [
        f"⚠️ **Trade health check — {n_issues} issue(s) across {n_trades} trade(s)**",
        f"_Scanned {scanned} closed trades, checked at {et_now}_",
        "",
    ]
    for trade, msgs in findings:
        e = trade["extra"]
        ticker = e.get("ticker") or trade["symbol"]
        action = e.get("action") or trade.get("strategy")
        pnl = trade.get("realized_pnl_usd")
        pnl_s = f"${float(pnl):+.2f}" if pnl is not None else "?"
        lines.append(f"**#{trade['id']}** {ticker} {action} — realized {pnl_s}")
        for msg in msgs:
            lines.append(f"  - {msg}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO date/datetime — scan trades closed at/after this. Overrides watermark.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ignore watermark, scan everything since CUTOFF_DATE.",
    )
    parser.add_argument(
        "--no-mark",
        action="store_true",
        help="Don't update the watermark even on clean runs.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print a one-line clean-run summary to stderr (still empty stdout).",
    )
    parser.add_argument(
        "--cutoff",
        type=str,
        default=None,
        help=(
            f"ISO date — only enforce post-rewrite field checks on trades opened "
            f"on/after this date. Default: {DEFAULT_CUTOFF_DATE.isoformat()}."
        ),
    )
    args = parser.parse_args()

    cutoff_date = (
        date.fromisoformat(args.cutoff) if args.cutoff else DEFAULT_CUTOFF_DATE
    )

    since_dt: datetime | None = None
    after_id = 0
    if args.since:
        since_dt = datetime.fromisoformat(args.since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=ET)
    elif args.all:
        since_dt = datetime.combine(cutoff_date, datetime.min.time(), ET)
    else:
        after_id = load_watermark()
        if after_id == 0:
            # First run — scan since cutoff as the safe default
            since_dt = datetime.combine(cutoff_date, datetime.min.time(), ET)

    with sqlite3.connect(str(DB)) as conn:
        trades = fetch_closed_trades(conn, after_id=after_id, since_dt=since_dt)

    findings: list[tuple[dict, list[str]]] = []
    for trade in trades:
        msgs = []
        for check in CHECKS:
            msgs.extend(check(trade, cutoff_date))
        if msgs:
            findings.append((trade, msgs))

    report = format_report(findings, scanned=len(trades))

    if not args.no_mark and trades:
        save_watermark(max(t["id"] for t in trades))

    if report:
        print(report)
        return 1

    if args.verbose:
        print(
            f"# trade_health_check: clean ({len(trades)} trades scanned)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
