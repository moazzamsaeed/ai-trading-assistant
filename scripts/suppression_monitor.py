"""Distance-aware stop monitor — post condor stop SUPPRESSIONS to #logs.

When condor_distance_aware_stop is on, the exit monitor logs
`exit_monitor_stop_suppressed_inside` each time it HELD a 1.5x stop because SPY was
comfortably inside its shorts (the thin-credit OTM-approach whipsaw it's built to
avoid — see the 2026-09-01..04 week). Run post-close by a systemd timer, this reads
today's daemon journal for those events and, for each suppressed trade not already
reported, posts a verdict to #logs: what the suppression DID (expired inside =
whipsaw avoided / stop re-fired once SPY reached the strike / daily cap) + realized
P&L. So we learn on the FIRST real instance whether the change helps.

Idempotent via data/suppression_reported.txt (one line per reported trade id), so a
trade is announced once even though the timer runs daily. Quiet when nothing to say.

Usage: python scripts/suppression_monitor.py [--post]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import subprocess
from decimal import Decimal
from pathlib import Path

from trademaster.config import get_settings

STATE = Path("data/suppression_reported.txt")
SUPPRESS_EVT = "exit_monitor_stop_suppressed_inside"


def _journal_today() -> str:
    try:
        return subprocess.run(
            ["journalctl", "--user", "-u", "trademaster.service",
             "--since", "today", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — journal unavailable → treat as no events
        return ""


def _occ_strike(occ: str | None) -> str:
    if not occ or len(occ) < 8 or not occ[-8:].isdigit():
        return "?"
    return str(Decimal(occ[-8:]) / Decimal("1000")).rstrip("0").rstrip(".")


def _suppressed_ids(journal: str) -> list[int]:
    """Trade ids with a suppression event today, in first-seen order."""
    ids: list[int] = []
    for ln in journal.splitlines():
        if SUPPRESS_EVT in ln:
            m = re.search(r'"?trade_id"?[=:]\s*"?(\d+)', ln)
            if m and int(m.group(1)) not in ids:
                ids.append(int(m.group(1)))
    return ids


def _suppress_detail(journal: str, tid: int) -> dict:
    for ln in journal.splitlines():
        if SUPPRESS_EVT in ln and re.search(rf'"?trade_id"?[=:]\s*"?{tid}\b', ln):
            spot = re.search(r'"?spot"?[=:]\s*"?([\d.]+)', ln)
            sp = re.search(r'"?short_put"?[=:]\s*"?([A-Z0-9]+)', ln)
            sc = re.search(r'"?short_call"?[=:]\s*"?([A-Z0-9]+)', ln)
            return {
                "spot": spot.group(1) if spot else "?",
                "short_put": _occ_strike(sp.group(1)) if sp else "?",
                "short_call": _occ_strike(sc.group(1)) if sc else "?",
            }
    return {"spot": "?", "short_put": "?", "short_call": "?"}


def _refired(journal: str, tid: int) -> bool:
    """Did a stop/daily-cap close terminal-fire for this trade AFTER suppression?"""
    for ln in journal.splitlines():
        if "exit_monitor_close_terminal" in ln and re.search(rf'"?trade_id"?[=:]\s*"?{tid}\b', ln):
            if "stop_loss" in ln or "daily_loss_cap" in ln:
                return True
    return False


def _trade_final(tid: int) -> tuple[str | None, object]:
    """(closed_at, realized_pnl_usd) from the DB — post-settlement truth."""
    try:
        c = sqlite3.connect("data/trademaster.db")
        row = c.execute(
            "SELECT closed_at, realized_pnl_usd FROM trades WHERE id=?", (tid,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception:  # noqa: BLE001
        return (None, None)


def _already_reported() -> set[int]:
    if not STATE.exists():
        return set()
    return {int(x) for x in STATE.read_text().split() if x.strip().isdigit()}


def _mark_reported(tid: int) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.open("a") as f:
        f.write(f"{tid}\n")


def _verdict(journal: str, tid: int) -> str:
    closed_at, pnl = _trade_final(tid)
    if _refired(journal, tid):
        tail = f" → closed ${pnl}" if pnl is not None else ""
        return (f"the stop RE-FIRED later (SPY reached the strike after all){tail} — "
                f"suppression delayed the cut but the breach was real")
    if closed_at is None or pnl is None:
        return "trade still open / pending settlement — the 09:25 check confirms"
    pnl_d = Decimal(str(pnl))
    if pnl_d >= 0:
        return (f"the condor closed INSIDE for **+${pnl_d}** — the suppression AVOIDED a "
                f"whipsaw ✅ (the raw 1.5× stop would have booked a loss here)")
    return (f"the condor still closed at **${pnl_d}** despite holding — suppression didn't "
            f"save this one (it drifted back to/through a short by the bell)")


def build_messages() -> list[str]:
    journal = _journal_today()
    ids = _suppressed_ids(journal)
    if not ids:
        return []
    done = _already_reported()
    first_ever = not done
    out: list[str] = []
    for tid in ids:
        if tid in done:
            continue
        d = _suppress_detail(journal, tid)
        header = ("🎯 **FIRST distance-aware stop suppression** since the feature went live"
                  if first_ever and not out else "🎯 Distance-aware stop suppression")
        out.append(
            f"{header} — condor #{tid}: the 1.5× stop was SUPPRESSED intraday "
            f"(SPY ${d['spot']} was inside shorts {d['short_put']}P/{d['short_call']}C). "
            f"Outcome: {_verdict(journal, tid)}."
        )
    return out


async def _post(text: str) -> None:
    import aiohttp
    s = get_settings()
    token = s.discord_bot_token.get_secret_value()
    cid = s.discord_channel_logs
    if not token or not cid:
        print("(no discord token/channel — skipping post)")
        return
    url = f"https://discord.com/api/v10/channels/{cid}/messages"
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as sess:
        await sess.post(url, json={"content": text[:1900]})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="post to #logs and record state")
    args = ap.parse_args()
    msgs = build_messages()
    if not msgs:
        print("[suppression] nothing to report (no new suppressions today).")
        return 0
    ids = _suppressed_ids(_journal_today())
    done = _already_reported()
    for msg in msgs:
        print(msg)
        if args.post:
            asyncio.run(_post(msg))
    if args.post:
        for tid in ids:
            if tid not in done:
                _mark_reported(tid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
