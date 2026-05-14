"""Truth Social news integration.

Monitors Trump's Truth Social posts for market-moving content.
Trump's posts frequently move SPY, QQQ, TSLA, and tech broadly —
e.g. April 9 2025 "BUY" post preceded a 90-day tariff pause → SPY +10.5%.

Uses `truthbrush` library (pip: truthbrush). No auth required for public profiles.
Falls back gracefully (empty list) if unavailable — never blocks trading.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from trademaster.logging import get_logger

log = get_logger(__name__)

TRUMP_USERNAME = "realDonaldTrump"

# Keywords that suggest the post may be market-relevant
MARKET_KEYWORDS = [
    "tariff", "tariffs", "trade", "stock", "market", "economy", "economic",
    "buy", "sell", "deal", "tax", "rate", "fed", "inflation", "gdp",
    "energy", "oil", "gas", "tech", "ai", "china", "america", "jobs",
    "tsla", "tesla", "nvda", "nvidia", "apple", "amazon",
]


async def get_recent_trump_posts(minutes: int = 60) -> list[str]:
    """Return Trump's Truth Social posts from the last `minutes` minutes
    that contain market-relevant keywords.

    Returns an empty list on any error (fail-open so trading is never blocked).
    Runs in a thread to avoid blocking the async event loop.
    """
    try:
        return await asyncio.to_thread(_fetch_posts, minutes)
    except Exception as e:  # noqa: BLE001
        log.debug("truth_social_fetch_failed", error=str(e))
        return []


def _fetch_posts(minutes: int) -> list[str]:
    """Synchronous inner fetch — called via asyncio.to_thread."""
    try:
        from truthbrush import Api  # type: ignore[import]
    except ImportError:
        return []

    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    recent: list[str] = []

    try:
        api = Api()
        # pull_statuses is a generator; stop after first page (~20 posts)
        for status in api.pull_statuses(TRUMP_USERNAME, created_after=cutoff):
            content: str = status.get("content", "") or ""
            # Strip HTML tags (Truth Social returns HTML)
            import re
            clean = re.sub(r"<[^>]+>", " ", content).strip()
            clean = re.sub(r"\s+", " ", clean)
            if not clean:
                continue
            # Filter to market-relevant posts
            lower = clean.lower()
            if any(kw in lower for kw in MARKET_KEYWORDS):
                recent.append(clean)
            if len(recent) >= 5:
                break
    except Exception as e:  # noqa: BLE001
        log.debug("truth_social_api_error", error=str(e))

    if recent:
        log.info("truth_social_posts_found", count=len(recent), lookback_min=minutes)

    return recent
