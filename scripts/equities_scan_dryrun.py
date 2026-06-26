"""Read-only preview of the equities signal scanner — prints what WOULD be posted
to #stock-signals, without touching Discord. Run during market hours.

Usage: .venv/bin/python -m scripts.equities_scan_dryrun
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from agents.equities.scanner import (
    CONVICTIONS_POSTED, format_equities_signal, run_equities_scan,
)
from trademaster.timeutils import to_et


async def main() -> None:
    now = datetime.now(UTC)
    print(f"\n═══ EQUITIES SCAN DRY-RUN — {to_et(now).strftime('%Y-%m-%d %H:%M ET')} ═══\n")
    decisions = await run_equities_scan(now)
    if not decisions:
        print("  (no decisions — market closed, empty watchlist, or no bars)")
        return

    print(f"{'':3}{'ticker':<7}{'action':<10}{'conv':<8} reasoning")
    print("  " + "-" * 78)
    for d in decisions:
        postable = d.action != "HOLD" and d.conviction in CONVICTIONS_POSTED
        mark = "🔔" if postable else "  "
        print(f"{mark} {d.ticker:<7}{d.action:<10}{d.conviction:<8} {d.reasoning[:60]}")

    posts = [d for d in decisions if d.action != "HOLD" and d.conviction in CONVICTIONS_POSTED]
    print(f"\n── would post {len(posts)} signal(s) ──\n")
    for d in posts:
        price = (d.analysis or {}).get("spy_price")
        print(format_equities_signal(d, price=price))
        print()


if __name__ == "__main__":
    asyncio.run(main())
