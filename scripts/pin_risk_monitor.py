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
from decimal import Decimal

import integrations.alpaca_client as ac
from trademaster.config import get_settings
from trademaster.timeutils import today_et

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
        return None  # comfortably inside → expires free → quiet, no alert

    where = f"SPY {spot:.2f} vs shorts {sp:.0f}P/{sc:.0f}C"
    closed_in_market = bool(e.get("close_order_id")) or str(
        e.get("exit_reason", "")
    ).startswith(("force_close", "stop", "daily"))
    settled = "reconciler" in str(e.get("exit_reasoning") or "").lower()

    if closed_in_market:
        return (f"📌✅ PIN RISK today — condor #{tid}: {where}. Smart 15:45 close CAUGHT it "
                f"(closed in-market, P&L ${float(pnl or 0):+,.0f}) — assignment avoided.")
    if settled or closed_at is not None:
        return (f"📌⚠️ PIN RISK today — condor #{tid}: {where}. It FELL THROUGH to settlement "
                f"(smart close didn't fill). WATCH FOR ASSIGNMENT overnight — the 09:25 check will confirm.")
    return (f"📌⚠️ PIN RISK today — condor #{tid}: {where}, and it's STILL OPEN at 15:47 — "
            f"the smart 15:45 close did not fill. Will settle at 16:03; assignment risk overnight.")


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
