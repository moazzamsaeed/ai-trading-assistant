"""Tests for the Alpaca WebSocket stream trigger logic.

These tests exercise the debounce, volume surge, and news detection logic
without making real WebSocket connections.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from integrations.alpaca_stream import (
    DEBOUNCE_SECONDS,
    GLOBAL_NEWS_DEBOUNCE_SECONDS,
    MIN_HISTORY_BARS,
    VOLUME_SURGE_RATIO,
    DirectionalStreamTrigger,
)


def _make_trigger(fired: list) -> DirectionalStreamTrigger:
    """Build a trigger that appends (ticker, reason) to `fired` when it fires."""
    loop = asyncio.get_event_loop()

    async def on_trigger(ticker: str, reason: str) -> None:
        fired.append((ticker, reason))

    return DirectionalStreamTrigger(
        main_loop=loop,
        on_trigger=on_trigger,
        watchlist=("SPY", "NVDA", "TSLA"),
    )


def _bar(symbol: str, volume: int) -> MagicMock:
    b = MagicMock()
    b.symbol = symbol
    b.volume = volume
    return b


def _news(symbols: list[str], headline: str) -> MagicMock:
    n = MagicMock()
    n.symbols = symbols
    n.headline = headline
    return n


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


async def test_can_trigger_first_time():
    fired: list = []
    t = _make_trigger(fired)
    assert t._can_trigger("SPY") is True


async def test_can_trigger_after_cooldown():
    fired: list = []
    t = _make_trigger(fired)
    t._last_trigger["SPY"] = datetime.now(UTC) - timedelta(seconds=DEBOUNCE_SECONDS + 1)
    assert t._can_trigger("SPY") is True


async def test_cannot_trigger_within_cooldown():
    fired: list = []
    t = _make_trigger(fired)
    t._last_trigger["SPY"] = datetime.now(UTC) - timedelta(seconds=DEBOUNCE_SECONDS - 10)
    assert t._can_trigger("SPY") is False


async def test_fire_respects_debounce():
    fired: list = []
    t = _make_trigger(fired)
    t._last_trigger["SPY"] = datetime.now(UTC)
    t._fire("SPY", "test")
    assert fired == []


async def test_fire_triggers_when_allowed():
    fired: list = []
    t = _make_trigger(fired)
    t._fire("SPY", "volume_surge_2.5x")
    await asyncio.sleep(0)  # let the coroutine run
    assert len(fired) == 1
    assert fired[0] == ("SPY", "volume_surge_2.5x")


# ---------------------------------------------------------------------------
# Volume surge detection
# ---------------------------------------------------------------------------


async def test_no_surge_below_threshold():
    fired: list = []
    t = _make_trigger(fired)
    # Fill history with 10000-vol bars
    for _ in range(MIN_HISTORY_BARS):
        await t._handle_bar(_bar("SPY", 10_000))
    # Now a bar at 1.5× avg — below VOLUME_SURGE_RATIO (2.0)
    await t._handle_bar(_bar("SPY", 15_000))
    await asyncio.sleep(0)
    assert fired == []


async def test_surge_fires_above_threshold():
    fired: list = []
    t = _make_trigger(fired)
    for _ in range(MIN_HISTORY_BARS):
        await t._handle_bar(_bar("NVDA", 10_000))
    # 2.5× avg — above threshold
    await t._handle_bar(_bar("NVDA", 25_000))
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0][0] == "NVDA"
    assert "volume_surge" in fired[0][1]


async def test_no_surge_before_min_history():
    fired: list = []
    t = _make_trigger(fired)
    # Send fewer than MIN_HISTORY_BARS total bars (including the surge bar).
    for _ in range(MIN_HISTORY_BARS - 2):
        await t._handle_bar(_bar("SPY", 10_000))
    await t._handle_bar(_bar("SPY", 999_999))  # extreme surge as the last bar
    await asyncio.sleep(0)
    # Total bars = MIN_HISTORY_BARS - 1 → still below threshold, no trigger.
    assert fired == []


async def test_non_watchlist_bar_ignored():
    fired: list = []
    t = _make_trigger(fired)
    for _ in range(MIN_HISTORY_BARS + 2):
        await t._handle_bar(_bar("AAPL", 50_000))  # AAPL not in watchlist
    await asyncio.sleep(0)
    assert fired == []


# ---------------------------------------------------------------------------
# News trigger — Tier 1: watchlist ticker
# ---------------------------------------------------------------------------


async def test_news_tier1_watchlist_ticker_fires():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news(["TSLA"], "Tesla beats Q1 earnings"))
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0][0] == "TSLA"
    assert "Tesla beats" in fired[0][1]


async def test_news_tier1_returns_after_watchlist_hit():
    """Watchlist match short-circuits — no second global trigger for same article."""
    fired: list = []
    t = _make_trigger(fired)
    # Article tags both a watchlist ticker and has a macro keyword
    await t._handle_news(_news(["NVDA"], "NVDA earnings beat on tariff relief news"))
    await asyncio.sleep(0)
    assert len(fired) == 1  # only one trigger, not two
    assert fired[0][0] == "NVDA"


async def test_news_tier1_fires_once_per_debounce():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news(["SPY"], "SPY breaks resistance"))
    await asyncio.sleep(0)
    await t._handle_news(_news(["SPY"], "SPY continues rally"))
    await asyncio.sleep(0)
    assert len(fired) == 1  # second blocked by per-ticker debounce


# ---------------------------------------------------------------------------
# News trigger — Tier 2: macro keywords
# ---------------------------------------------------------------------------


async def test_news_tier2_macro_keyword_fires():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news([], "Fed raises interest rate by 25 bps"))
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0][0] == "MARKET"
    assert "macro:" in fired[0][1]


async def test_news_tier2_tariff_fires():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news([], "Trump announces new tariffs on Chinese goods"))
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0][0] == "MARKET"


async def test_news_tier2_macro_bypasses_debounce():
    """Macro news fires even within the normal debounce window."""
    fired: list = []
    t = _make_trigger(fired)
    # Set debounce as if we just fired
    from datetime import UTC, datetime
    t._last_trigger["MARKET"] = datetime.now(UTC)
    await t._handle_news(_news([], "Powell signals emergency rate cut"))
    await asyncio.sleep(0)
    assert len(fired) == 1  # force=True bypasses the cooldown


async def test_news_tier2_jobs_data_fires():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news([], "Nonfarm payrolls miss expectations by 50k"))
    await asyncio.sleep(0)
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# News trigger — Tier 3: general financial news
# ---------------------------------------------------------------------------


async def test_news_tier3_general_news_fires():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news(["AAPL"], "Apple announces new product line"))
    await asyncio.sleep(0)
    assert len(fired) == 1
    assert fired[0][0] == "MARKET"


async def test_news_tier3_global_debounce_blocks_second():
    fired: list = []
    t = _make_trigger(fired)
    await t._handle_news(_news(["AAPL"], "Apple product launch"))
    await asyncio.sleep(0)
    await t._handle_news(_news(["AMZN"], "Amazon warehouse expansion"))
    await asyncio.sleep(0)
    assert len(fired) == 1  # second blocked by global debounce


async def test_news_tier3_fires_after_global_debounce_expires():
    fired: list = []
    t = _make_trigger(fired)
    from datetime import UTC, datetime, timedelta
    t._last_news_scan = datetime.now(UTC) - timedelta(seconds=GLOBAL_NEWS_DEBOUNCE_SECONDS + 1)
    await t._handle_news(_news(["AMZN"], "Amazon AWS outage"))
    await asyncio.sleep(0)
    assert len(fired) == 1


async def test_news_headline_truncated_to_80_chars():
    fired: list = []
    t = _make_trigger(fired)
    long_headline = "A" * 200
    await t._handle_news(_news(["SPY"], long_headline))
    await asyncio.sleep(0)
    _, reason = fired[0]
    assert len(reason) <= 80 + len("news:")
