"""Alpaca news-client wrapper tests with mocked SDK."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from integrations import alpaca_client


async def test_get_recent_news_parses_articles(monkeypatch):
    raw_items = [
        SimpleNamespace(
            headline="SPY hits new ATH",
            summary="The S&P 500 ETF reached a new high.",
            url="https://example.com/1",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
            symbols=["SPY"],
            source="alpaca",
        ),
        SimpleNamespace(
            headline="Macro print weighs on QQQ",
            summary="CPI came in hot.",
            url="https://example.com/2",
            created_at=datetime(2026, 5, 10, 11, 0, tzinfo=UTC),
            symbols=["QQQ"],
            source="alpaca",
        ),
    ]

    class FakeClient:
        def __init__(self, **_):
            pass

        def get_news(self, _req):
            return SimpleNamespace(news=raw_items)

    monkeypatch.setattr(alpaca_client, "_client", lambda: FakeClient())

    articles = await alpaca_client.get_recent_news(("SPY", "QQQ"))
    assert len(articles) == 2
    assert articles[0].headline == "SPY hits new ATH"
    assert articles[0].symbols == ("SPY",)
    assert articles[1].source == "alpaca"


async def test_get_recent_news_handles_empty(monkeypatch):
    class FakeClient:
        def __init__(self, **_):
            pass

        def get_news(self, _req):
            return SimpleNamespace(news=[])

    monkeypatch.setattr(alpaca_client, "_client", lambda: FakeClient())

    articles = await alpaca_client.get_recent_news()
    assert articles == []


async def test_get_recent_news_tolerates_missing_fields(monkeypatch):
    raw_items = [
        SimpleNamespace(headline="No symbols article"),  # no summary/url/symbols
    ]

    class FakeClient:
        def __init__(self, **_):
            pass

        def get_news(self, _req):
            return SimpleNamespace(news=raw_items)

    monkeypatch.setattr(alpaca_client, "_client", lambda: FakeClient())

    articles = await alpaca_client.get_recent_news()
    assert len(articles) == 1
    assert articles[0].headline == "No symbols article"
    assert articles[0].summary == ""
    assert articles[0].symbols == ()
