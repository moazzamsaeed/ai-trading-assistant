"""Macro context integration for TradeMaster.

Reads market-moving macro headlines from data/macro_context.json,
written by the Hermes cron job (Trump/China/Fed news monitor).

The file is written externally by Hermes and consumed here on every scan.
Falls back gracefully (empty list) if the file is missing or stale.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trademaster.logging import get_logger

log = get_logger(__name__)

_MACRO_FILE = Path(__file__).parent.parent / "data" / "macro_context.json"
# Ignore context older than this (stale between Hermes cron runs)
_MAX_AGE_MINUTES = 60


def get_macro_headlines(max_age_minutes: int = _MAX_AGE_MINUTES) -> list[str]:
    """Return recent macro headlines from the Hermes-written context file.

    Returns an empty list if the file is missing, unreadable, or stale.
    Never raises — always fail-open so trading is never blocked.
    """
    try:
        if not _MACRO_FILE.exists():
            return []
        raw = _MACRO_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)

        updated_str = data.get("updated_at")
        if updated_str:
            updated = datetime.fromisoformat(updated_str)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            age = datetime.now(UTC) - updated
            if age > timedelta(minutes=max_age_minutes):
                log.debug(
                    "macro_context_stale",
                    age_minutes=round(age.total_seconds() / 60, 1),
                )
                return []

        headlines: list[str] = data.get("headlines", [])
        if headlines:
            log.info("macro_context_loaded", count=len(headlines))
        return headlines

    except Exception as e:  # noqa: BLE001
        log.debug("macro_context_read_failed", error=str(e))
        return []
