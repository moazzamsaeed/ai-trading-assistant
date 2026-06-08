#!/usr/bin/env python3
"""Archive (and optionally clear) Discord channel history.

Uses the Discord REST API directly with the bot token — no gateway connection,
so it does NOT disturb the running daemon's bot. Archives every message to a
local JSONL file FIRST, verifies it, and only then deletes (when --clear is
passed). Archives live under data/channel_archives/ (gitignored) so old signals
are preserved for later research.

Usage:
    .venv/bin/python scripts/archive_clear_channels.py                      # archive only (safe)
    .venv/bin/python scripts/archive_clear_channels.py --channels signals,research
    .venv/bin/python scripts/archive_clear_channels.py --clear              # archive THEN delete
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from trademaster.config import get_settings

API = "https://discord.com/api/v10"
PROJECT_ROOT = Path(__file__).parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "data" / "channel_archives"
# Discord bulk-delete only accepts messages younger than 14 days.
BULK_DELETE_MAX_AGE_S = 14 * 24 * 3600 - 3600  # 14d minus a safety hour


def _channel_map() -> dict[str, str]:
    s = get_settings()
    return {
        "signals": s.discord_channel_signals,
        "research": s.discord_channel_research,
        "trades": s.discord_channel_trades,
        "logs": s.discord_channel_logs,
    }


async def _request(session, method, url, **kw):
    """One REST call with basic 429 rate-limit handling."""
    for _ in range(8):
        async with session.request(method, url, **kw) as resp:
            if resp.status == 429:
                retry = (await resp.json()).get("retry_after", 1.0)
                await asyncio.sleep(float(retry) + 0.1)
                continue
            if resp.status in (200, 204):
                return await resp.json() if resp.status == 200 else None
            raise RuntimeError(f"{method} {url} → {resp.status}: {await resp.text()}")
    raise RuntimeError(f"{method} {url} → rate-limited out")


async def fetch_all(session, channel_id: str) -> list[dict]:
    """Page through the full channel history (newest → oldest)."""
    out: list[dict] = []
    before = None
    while True:
        url = f"{API}/channels/{channel_id}/messages?limit=100"
        if before:
            url += f"&before={before}"
        batch = await _request(session, "GET", url)
        if not batch:
            break
        out.extend(batch)
        before = batch[-1]["id"]
        await asyncio.sleep(0.3)  # be gentle
    return out


def archive(name: str, messages: list[dict], stamp: str) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"{name}_{stamp}.jsonl"
    with path.open("w") as f:
        # oldest first in the archive for readability
        for m in reversed(messages):
            f.write(json.dumps({
                "id": m["id"],
                "timestamp": m.get("timestamp"),
                "author": (m.get("author") or {}).get("username"),
                "content": m.get("content", ""),
                "embeds": m.get("embeds", []),
            }) + "\n")
    return path


def _age_seconds(msg: dict, now: datetime) -> float:
    ts = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00"))
    return (now - ts).total_seconds()


async def clear(session, channel_id: str, messages: list[dict]) -> int:
    """Delete all messages: bulk for <14d (batches of 100), single for older."""
    now = datetime.now(UTC)
    recent = [m["id"] for m in messages if _age_seconds(m, now) < BULK_DELETE_MAX_AGE_S]
    old = [m["id"] for m in messages if _age_seconds(m, now) >= BULK_DELETE_MAX_AGE_S]
    deleted = 0
    # bulk-delete recent in chunks of 100 (min 2 per the API)
    for i in range(0, len(recent), 100):
        chunk = recent[i:i + 100]
        if len(chunk) == 1:
            old.append(chunk[0])
            continue
        await _request(session, "POST", f"{API}/channels/{channel_id}/messages/bulk-delete",
                       json={"messages": chunk})
        deleted += len(chunk)
        await asyncio.sleep(0.5)
    # individual-delete the old ones (rate-limited)
    for mid in old:
        await _request(session, "DELETE", f"{API}/channels/{channel_id}/messages/{mid}")
        deleted += 1
        await asyncio.sleep(0.35)
    return deleted


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default="signals,research")
    ap.add_argument("--clear", action="store_true", help="delete after archiving (irreversible)")
    args = ap.parse_args()

    token = get_settings().discord_bot_token.get_secret_value()
    if not token:
        print("ERROR: no DISCORD_BOT_TOKEN", file=sys.stderr)
        return 1
    cmap = _channel_map()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    headers = {"Authorization": f"Bot {token}"}

    async with aiohttp.ClientSession(headers=headers) as session:
        for name in [c.strip() for c in args.channels.split(",") if c.strip()]:
            cid = cmap.get(name)
            if not cid:
                print(f"skip {name}: no channel id configured")
                continue
            msgs = await fetch_all(session, cid)
            path = archive(name, msgs, stamp)
            # verify archive line count == fetched count before any delete
            n_lines = sum(1 for _ in path.open())
            ok = n_lines == len(msgs)
            print(f"#{name}: archived {len(msgs)} messages → {path} (verify={'OK' if ok else 'MISMATCH'})")
            if args.clear:
                if not ok:
                    print(f"  ABORT clear for #{name}: archive verify failed")
                    continue
                if not msgs:
                    print(f"  #{name}: already empty, nothing to clear")
                    continue
                deleted = await clear(session, cid, msgs)
                print(f"  #{name}: cleared {deleted} messages")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
