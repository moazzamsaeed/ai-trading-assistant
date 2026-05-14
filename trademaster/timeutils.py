"""Timezone helpers — Eastern Time is the canonical user-facing zone.

UTC stays as the storage format for `datetime` in the DB and internal logic.
Anything user-facing (Discord posts, dashboard, log lines humans read) must
render in ET so timestamps line up with the trading day regardless of host TZ.

Use these helpers instead of inlining `ZoneInfo("America/New_York")` everywhere.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current time as a tz-aware datetime in Eastern Time."""
    return datetime.now(ET)


def today_et() -> date:
    """Today's date according to the Eastern Time calendar."""
    return now_et().date()


def to_et(dt: datetime) -> datetime:
    """Convert any tz-aware datetime to Eastern Time."""
    return dt.astimezone(ET)


def fmt_et(dt: datetime, fmt: str = "%b %-d %-I:%M %p ET") -> str:
    """Render a datetime in Eastern Time using strftime."""
    return dt.astimezone(ET).strftime(fmt)
