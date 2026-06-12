#!/usr/bin/env python3
"""Calibrate the ADX entry-gate thresholds from real trade outcomes.

Pairs each directional trade's entry-time ADX (persisted in extra.entry_adx as
of 2026-06-11) with its outcome, buckets by ADX, and reports win-rate + avg P&L
per bucket so the adx_block_below / adx_full_above thresholds can be set from
evidence instead of the first-pass 18/25 guess.

Usage:
    .venv/bin/python scripts/calibrate_adx.py                 # print report
    .venv/bin/python scripts/calibrate_adx.py --since 2026-06-12
    .venv/bin/python scripts/calibrate_adx.py --post          # also post to #logs

This does NOT change config — it recommends. Apply the thresholds by hand (or
ask Claude) once the sample is solid.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB = PROJECT_ROOT / "data" / "trademaster.db"
MIN_SAMPLE = 8
FAILED_PEAK_PCT = 10.0  # peaked below this + a loss = failed breakout
BUCKETS = [(0, 15), (15, 18), (18, 22), (22, 25), (25, 30), (30, 200)]


def _load(since: str) -> list[dict]:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT realized_pnl_usd, extra FROM trades "
        "WHERE strategy LIKE 'directional%' AND closed_at IS NOT NULL "
        "AND opened_at >= ? ORDER BY opened_at",
        (since,),
    ).fetchall()
    con.close()
    out: list[dict] = []
    for pnl_raw, extra_raw in rows:
        e = json.loads(extra_raw) if extra_raw else {}
        if e.get("exit_reason") == "position_not_in_broker":
            continue  # phantom — infra noise
        adx = e.get("entry_adx")
        if adx is None:
            continue
        try:
            adx = float(adx)
        except (TypeError, ValueError):
            continue
        total = float(pnl_raw or 0) + float(e.get("partial_realized_pnl_usd", 0) or 0)
        peak = float(e.get("peak_pnl_pct", 0.0) or 0.0)
        out.append({
            "adx": adx, "pnl": total, "peak": peak,
            "win": total > 0,
            "failed_breakout": peak < FAILED_PEAK_PCT and total < 0,
        })
    return out


def _report(trades: list[dict]) -> str:
    n = len(trades)
    lines = [f"📐 ADX gate calibration — {n} trades with entry_adx"]
    if n < MIN_SAMPLE:
        lines.append(
            f"⚠️ Sample too small (<{MIN_SAMPLE}). Keep current thresholds "
            f"(18/25) and recalibrate in a few more trading days."
        )
        return "\n".join(lines)

    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    def _stat(rows: list[dict]) -> str:
        if not rows:
            return "n/a"
        a = sorted(t["adx"] for t in rows)
        med = a[len(a) // 2]
        return f"n={len(a)} adx[min={a[0]:.1f} med={med:.1f} max={a[-1]:.1f}]"

    lines.append(f"  winners:  {_stat(wins)}")
    lines.append(f"  losers:   {_stat(losses)}")
    lines.append("  bucket    trades  win%   avg P&L   failed-breakout%")
    for lo, hi in BUCKETS:
        b = [t for t in trades if lo <= t["adx"] < hi]
        if not b:
            continue
        win_pct = 100 * sum(t["win"] for t in b) / len(b)
        fb_pct = 100 * sum(t["failed_breakout"] for t in b) / len(b)
        avg = sum(t["pnl"] for t in b) / len(b)
        lines.append(
            f"  {lo:>2}-{hi:<3}    {len(b):>4}   {win_pct:>4.0f}%  ${avg:>+8.0f}   {fb_pct:>4.0f}%"
        )

    # Recommendation: block below the highest bucket-top that is still net-losing
    # AND mostly failed breakouts; full size above the lowest bucket-top that is
    # net-positive.
    block = None
    full = None
    for lo, hi in BUCKETS:
        b = [t for t in trades if lo <= t["adx"] < hi]
        if len(b) < 2:
            continue
        net = sum(t["pnl"] for t in b)
        win_pct = 100 * sum(t["win"] for t in b) / len(b)
        if net < 0 and win_pct < 40:
            block = hi  # this band loses → block up to its top
        if full is None and net > 0 and win_pct >= 50:
            full = lo  # first profitable band → full size from here up
    lines.append("")
    lines.append(
        f"  → recommend: adx_block_below = {block if block else '(no clear loss band — keep 18)'}"
        f" , adx_full_above = {full if full else '(no clear win band — keep 25)'}"
    )
    lines.append("  (apply in trademaster/config.py, then commit + restart.)")
    return "\n".join(lines)


def _post_to_logs(text: str) -> None:
    import asyncio

    import aiohttp

    from trademaster.config import get_settings
    s = get_settings()
    token = s.discord_bot_token.get_secret_value()
    cid = s.discord_channel_logs
    if not token or not cid:
        print("(no discord token/channel — skipping post)")
        return

    async def _send() -> None:
        url = f"https://discord.com/api/v10/channels/{cid}/messages"
        headers = {"Authorization": f"Bot {token}"}
        async with aiohttp.ClientSession(headers=headers) as sess:
            await sess.post(url, json={"content": f"```\n{text[:1900]}\n```"})

    asyncio.run(_send())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-12", help="opened_at lower bound (YYYY-MM-DD)")
    ap.add_argument("--post", action="store_true", help="post the report to #logs")
    args = ap.parse_args()

    trades = _load(args.since)
    report = _report(trades)
    print(report)
    if args.post:
        _post_to_logs(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
