"""Daemon watchdog — alert to #logs when TradeMaster is NOT running.

The startup heartbeat (orchestrator.build_startup_heartbeat) proves a GOOD
boot. It cannot report a bad one: if the daemon dies at startup, nothing
posts and the day looks identical to a quiet no-trade day. That is exactly
what happened 2026-09-23 — 447 silent crash-loops from 06:45, the 10:00 ET
condor entry missed, and the only symptom was an absent trade.

This runs OUTSIDE the daemon (systemd timer) so it survives the daemon being
dead, and fires early enough to fix things before the 10:00 ET condor entry.

Silent when healthy — it only speaks when something is wrong.

    python scripts/daemon_watchdog.py           # print verdict, exit 0/1
    python scripts/daemon_watchdog.py --post    # also alert #logs
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess

from trademaster.config import get_settings

SERVICE = "trademaster.service"

# The condor entry is the thing we are protecting; name it in the alert so the
# urgency is obvious ("you have N minutes to fix this", not "a service is down").
CONDOR_ENTRY_ET = "10:00 ET"


def service_props() -> dict[str, str]:
    """Read systemd's view of the unit. Empty dict if systemctl is unavailable."""
    try:
        out = subprocess.run(
            [
                "systemctl", "--user", "show", SERVICE,
                "--property=ActiveState,SubState,NRestarts,ExecMainStatus,ActiveEnterTimestamp",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # pragma: no cover - environment dependent
        return {}
    props: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return props


def recent_restarts(window_min: int = 30) -> int:
    """Restarts in the last `window_min` minutes — i.e. is it flapping *now*.

    Deliberately a rolling window, not a since-midnight count: once a morning
    crash-loop is fixed the daemon is healthy, and a cumulative counter would
    keep crying wolf for the rest of the session.
    """
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE,
             "--since", f"-{window_min}min", "--no-pager"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # pragma: no cover - environment dependent
        return 0
    return out.stdout.count("Scheduled restart job")


def last_error(max_lines: int = 3) -> str:
    """The tail of today's traceback, for a self-explaining alert."""
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", SERVICE, "--since", "today",
             "--no-pager", "-p", "err", "-n", "40"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # pragma: no cover - environment dependent
        return ""
    lines = [ln.split("]: ", 1)[-1].strip() for ln in out.stdout.splitlines() if ln.strip()]
    # The exception line is the useful one; tracebacks bury it at the end.
    meaty = [ln for ln in lines if ln and not ln.startswith(("File \"", "  ", "~", "^"))]
    return "\n".join(meaty[-max_lines:])


def build_alert(props: dict[str, str], restarts: int = 0, error: str = "") -> str | None:
    """Return the #logs alert, or None when the daemon is healthy.

    Pure function of the inputs so the failure modes are unit-testable.
    """
    state = props.get("ActiveState", "unknown")
    sub = props.get("SubState", "unknown")

    healthy = state == "active" and sub == "running"
    # "active/running" but flapping is still broken — it may die again before 10:00.
    flapping = healthy and restarts >= 3

    if healthy and not flapping:
        return None

    if flapping:
        headline = f"⚠️ **TradeMaster is UNSTABLE** — restarted **{restarts}×** in 30 min"
    elif state == "activating" and sub == "auto-restart":
        headline = f"🚨 **TradeMaster is CRASH-LOOPING** — {restarts} restarts in 30 min"
    elif state == "failed":
        headline = "🚨 **TradeMaster has FAILED** and is not running"
    elif not props:
        headline = "🚨 **TradeMaster status UNKNOWN** — could not query systemd"
    else:
        headline = f"🚨 **TradeMaster is NOT RUNNING** — `{state}/{sub}`"

    lines = [
        "@here " + headline,
        f"No condor entry will fire at **{CONDOR_ENTRY_ET}** unless this is fixed.",
    ]
    if error:
        lines.append(f"```\n{error[:600]}\n```")
    lines.append(
        "Check: `systemctl --user status trademaster.service` · "
        "Restart: `systemctl --user restart trademaster.service`"
    )
    return "\n".join(lines)


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
        await sess.post(
            url,
            json={
                "content": text[:1900],
                # @here only pings if the bot has Mention Everyone; harmless if not.
                "allowed_mentions": {"parse": ["everyone"]},
            },
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="alert #logs when unhealthy")
    args = ap.parse_args()

    props = service_props()
    restarts = recent_restarts()
    state = props.get("ActiveState", "unknown")
    alert = build_alert(props, restarts, last_error() if state != "active" else "")

    if alert is None:
        print(f"OK — {state}/{props.get('SubState')}, {restarts} recent restarts")
        return 0

    print(alert)
    if args.post:
        asyncio.run(_post(alert))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
