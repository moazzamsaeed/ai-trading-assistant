"""Pin-risk monitor — evaluate the smart 15:45 condor force-close in the wild.

Two modes (run by systemd timers), each posts to #logs ONLY when there's
something to report (quiet on calm days):

  --mode close   (~15:47 ET, just after the smart 15:45 close): if today's condor
                 finished NEAR a short strike (pin/breach risk), report whether the
                 smart close CAUGHT it (closed in-market) or it fell through / is
                 still open (fill failed → assignment risk).
  --mode assign  (~09:25 ET, before the 10:00 entry): alert on any SPY-share
                 assignment residue sitting in the account.

Usage: python scripts/pin_risk_monitor.py --mode {close,assign} [--post]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
from decimal import Decimal

import integrations.alpaca_client as ac
from trademaster.config import get_settings
from trademaster.timeutils import today_et


def _smart_close_decision(tid: int) -> str:
    """What did the daemon's exit monitor actually DO for this condor today?

    Reads the daemon journal (the source of truth) rather than guessing from
    'still open'. A condor left open because it was comfortably INSIDE at 15:45
    (exit_monitor_expire_inside) is CORRECT behavior, not a failed fill.
    Returns: 'closed' | 'failed' | 'expired_inside' | 'unknown'.
    """
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "trademaster.service",
             "--since", "today", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — journal unavailable → unknown
        return "unknown"
    key = f'"trade_id": {tid}'
    lines = [ln for ln in out.splitlines() if key in ln]
    filled = any("exit_monitor_close_terminal" in ln and '"status": "filled"' in ln
                 for ln in lines)
    fail_events = ("exit_monitor_position_shortfall", "exit_monitor_intent_mismatch",
                   "exit_monitor_cancelled_unfilled_close")
    failed = any(ev in ln for ln in lines for ev in fail_events) or any(
        "exit_monitor_close_terminal" in ln and '"status": "filled"' not in ln
        for ln in lines)
    expired = any("exit_monitor_expire_inside" in ln for ln in lines)
    if filled:
        return "closed"
    if failed:
        return "failed"
    if expired:
        return "expired_inside"
    return "unknown"

NEAR_PCT = 0.003  # 0.3% band = the smart-close near-strike gate


def _occ_strike(occ: str | None) -> float | None:
    if not occ or len(occ) < 8 or not occ[-8:].isdigit():
        return None
    return float(Decimal(occ[-8:]) / Decimal("1000"))


async def _spot() -> float | None:
    """Last 1-min close (more reliable than the latest free quote, which can go stale)."""
    try:
        bars = await ac.get_recent_bars("SPY", timeframe_minutes=1, limit=3)
        return float(bars[-1].close) if bars else None
    except Exception:  # noqa: BLE001
        return None


def _todays_condor():
    c = sqlite3.connect("data/trademaster.db")
    d = today_et().isoformat()
    return c.execute(
        "SELECT id, closed_at, realized_pnl_usd, extra FROM trades "
        "WHERE strategy='spy_0dte_ic' AND date(opened_at)=? ORDER BY id DESC LIMIT 1",
        (d,),
    ).fetchone()


async def close_mode() -> str | None:
    row = _todays_condor()
    if not row:
        return None  # no condor today
    tid, closed_at, pnl, extra = row
    e = json.loads(extra) if extra else {}
    sp, sc = _occ_strike(e.get("short_put")), _occ_strike(e.get("short_call"))
    spot = await _spot()
    if spot is None or sp is None or sc is None:
        return f"⚠️ Pin-risk monitor #{tid}: couldn't read spot/strikes — check manually."
    near = spot <= sp * (1 + NEAR_PCT) or spot >= sc * (1 - NEAR_PCT)
    if not near:
        return None  # comfortably inside → minimal assignment risk → quiet, no alert

    where = f"SPY {spot:.2f} vs shorts {sp:.0f}P/{sc:.0f}C"
    decision = _smart_close_decision(tid)

    if decision == "closed":
        return (f"📌✅ PIN RISK today — condor #{tid}: {where}. Smart close CAUGHT it "
                f"(closed in-market) — assignment avoided.")
    if decision == "failed":
        return (f"📌⚠️ PIN RISK today — condor #{tid}: {where}. Smart close ATTEMPTED but did NOT fill — "
                f"assignment risk overnight. The 09:25 check will confirm.")
    if decision == "expired_inside":
        # NOT a failure — the close correctly stood down because SPY was inside at 15:45.
        return (f"📌ℹ️ Pin watch — condor #{tid}: near a short now ({where}), but the smart close "
                f"correctly let it EXPIRE (SPY was inside the range at 15:45 — no close was needed). "
                f"A late dip could still breach, so the 09:25 check confirms no assignment. Not a fill failure.")
    return (f"📌 Pin watch — condor #{tid}: {where} into the close, still open. Couldn't read the "
            f"smart-close decision from the daemon logs; watch for assignment overnight (09:25 check).")


async def assign_mode() -> str | None:
    try:
        pos = await ac.get_positions()
    except Exception as ex:  # noqa: BLE001
        return f"⚠️ Pin-risk monitor: couldn't fetch positions ({ex})."
    spy = [p for p in pos if getattr(p, "symbol", "") == "SPY"]
    if not spy:
        return None  # clean — no residue
    legs = ", ".join(
        f"{getattr(p, 'qty')} SPY (mv {getattr(p, 'market_value', '?')})" for p in spy
    )
    return (f"🚨 ASSIGNMENT RESIDUE before the 10:00 entry: {legs} — a condor short leg was "
            f"assigned. The reconciler should flatten it (07:45 boot / 16:03 settle); verify "
            f"buying power is freed BEFORE the condor entry. The smart 15:45 close did NOT prevent this.")


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
    ap.add_argument("--mode", choices=["close", "assign"], required=True)
    ap.add_argument("--post", action="store_true", help="post the alert to #logs")
    args = ap.parse_args()
    msg = asyncio.run(close_mode() if args.mode == "close" else assign_mode())
    if msg:
        print(msg)
        if args.post:
            asyncio.run(_post(msg))
    else:
        print(f"[{args.mode}] nothing to report (no pin risk / no residue).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
