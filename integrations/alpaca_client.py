"""Async wrapper around `alpaca-py` for market data and news.

Phase 1.3 only uses the news endpoint. Bars, quotes, and trading endpoints
land in Phase 2 alongside the options strategist.

`alpaca-py` is synchronous; we wrap calls in `asyncio.to_thread()` to avoid
blocking the event loop that Discord + the scheduler share.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from trademaster.config import get_settings
from trademaster.logging import get_logger

log = get_logger(__name__)

DEFAULT_WATCHLIST = ("SPY", "QQQ", "IWM", "DIA")


@dataclass(frozen=True)
class NewsArticle:
    headline: str
    summary: str
    url: str
    created_at: datetime
    symbols: tuple[str, ...]
    source: str


def _client() -> NewsClient:
    settings = get_settings()
    return NewsClient(
        api_key=settings.alpaca_api_key.get_secret_value(),
        secret_key=settings.alpaca_api_secret.get_secret_value(),
    )


def _to_article(raw) -> NewsArticle:
    """Normalize an alpaca-py news object to our dataclass."""
    return NewsArticle(
        headline=getattr(raw, "headline", "") or "",
        summary=getattr(raw, "summary", "") or "",
        url=getattr(raw, "url", "") or "",
        created_at=getattr(raw, "created_at", datetime.now(UTC)),
        symbols=tuple(getattr(raw, "symbols", []) or []),
        source=getattr(raw, "source", "alpaca") or "alpaca",
    )


async def get_recent_news(
    symbols: tuple[str, ...] = DEFAULT_WATCHLIST,
    *,
    hours_back: int = 18,
    limit: int = 50,
) -> list[NewsArticle]:
    """Fetch news articles for the given symbols in the last `hours_back` hours.

    Sorted newest-first. Returns at most `limit` articles.
    """

    def _fetch() -> list[NewsArticle]:
        req = NewsRequest(
            symbols=",".join(symbols),
            start=datetime.now(UTC) - timedelta(hours=hours_back),
            end=datetime.now(UTC),
            limit=limit,
            sort="desc",
        )
        raw = _client().get_news(req)
        if hasattr(raw, "news"):
            items = raw.news
        elif hasattr(raw, "data"):
            items = raw.data
        else:
            items = raw
        return [_to_article(a) for a in items]

    return await asyncio.to_thread(_fetch)
