"""Daily market bias — written by the premarket briefing, read by every scan.

The premarket briefing (Gemini 2.5 Pro, 8 AM ET) produces a full analysis.
After it runs, a compact bias summary is extracted and written here so every
intraday scan can incorporate the morning's macro/sentiment context without
re-running the full briefing or passing it through Discord.

Schema of data/daily_bias.json:
  {
    "date": "2026-05-18",
    "bias": "BULLISH" | "BEARISH" | "NEUTRAL",
    "summary": "one sentence — key thesis for the day",
    "catalysts": ["SPY reclaiming 200-day MA", "FOMC minutes dovish"],
    "risks": ["CPI above estimate", "NVDA earnings miss"],
    "written_at": "2026-05-18T08:04:22Z"
  }
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from trademaster.logging import get_logger

log = get_logger(__name__)

_BIAS_FILE = Path(__file__).parent.parent / "data" / "daily_bias.json"


def write_daily_bias(
    bias: str,
    summary: str,
    catalysts: list[str],
    risks: list[str],
    date_str: str | None = None,
) -> None:
    """Write today's bias to disk. Called by the premarket job after briefing."""
    now = datetime.now(UTC)
    payload = {
        "date": date_str or now.strftime("%Y-%m-%d"),
        "bias": bias.upper(),
        "summary": summary[:300],
        "catalysts": catalysts[:5],
        "risks": risks[:5],
        "written_at": now.isoformat(),
    }
    _BIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BIAS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("daily_bias_written", bias=bias, date=payload["date"])


def get_daily_bias() -> dict | None:
    """Read today's bias. Returns None if missing, stale, or unreadable."""
    try:
        if not _BIAS_FILE.exists():
            return None
        data = json.loads(_BIAS_FILE.read_text(encoding="utf-8"))
        from trademaster.timeutils import today_et
        if data.get("date") != today_et().isoformat():
            log.debug("daily_bias_stale", file_date=data.get("date"))
            return None
        return data
    except Exception as e:  # noqa: BLE001
        log.debug("daily_bias_read_failed", error=str(e))
        return None


def format_bias_block(bias: dict) -> str:
    """Format bias dict for inclusion in the scan prompt."""
    lines = [
        f"Today's market bias: {bias['bias']} — {bias['summary']}",
    ]
    if bias.get("catalysts"):
        lines.append("Tailwinds: " + " | ".join(bias["catalysts"]))
    if bias.get("risks"):
        lines.append("Headwinds: " + " | ".join(bias["risks"]))
    return "\n".join(lines)
