"""Alpaca client wrapper tests with mocked SDK.

Covers the news endpoint and the trading-side wrappers (account,
positions, cancel/close) used by the risk manager and the kill switch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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


async def test_get_recent_news_parses_real_newsset_shape(monkeypatch):
    """Regression: alpaca-py returns NewsSet with .data == {"news": [...]} (NO
    .news attr). The old code did `items = raw.data` and iterated the dict's
    KEYS → one empty article every fetch → the bot ran with zero news. This is
    the shape production actually returns; it must parse to real articles."""
    raw_items = [
        SimpleNamespace(
            headline="Michigan Consumer Sentiment beats",
            summary="June 48.9 vs 46.1 est.", url="https://example.com/1",
            created_at=datetime(2026, 6, 12, 14, 0, tzinfo=UTC),
            symbols=["SPY"], source="alpaca",
        ),
    ]

    class FakeClient:
        def __init__(self, **_):
            pass

        def get_news(self, _req):
            return SimpleNamespace(data={"news": raw_items})  # the real shape

    monkeypatch.setattr(alpaca_client, "_client", lambda: FakeClient())
    articles = await alpaca_client.get_recent_news(("SPY",))
    assert len(articles) == 1
    assert articles[0].headline == "Michigan Consumer Sentiment beats"


def test_unwrap_news_shapes():
    # NewsSet.data dict (production), bare list, .news attr, and junk → [].
    assert alpaca_client._unwrap_news(SimpleNamespace(data={"news": [1, 2, 3]})) == [1, 2, 3]
    assert alpaca_client._unwrap_news(SimpleNamespace(data=[4, 5])) == [4, 5]
    assert alpaca_client._unwrap_news(SimpleNamespace(news=[6])) == [6]
    assert alpaca_client._unwrap_news(SimpleNamespace(data={"foo": "bar"})) == []
    assert alpaca_client._unwrap_news(object()) == []


# ----------------- trading client -----------------


async def test_get_account_parses_snapshot(monkeypatch):
    raw_account = SimpleNamespace(
        account_number="A123",
        status="ACTIVE",
        multiplier="1",
        cash="12345.67",
        buying_power="12345.67",
        equity="20000.00",
        portfolio_value="20000.00",
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
    )

    class FakeTrading:
        def __init__(self, **_):
            pass

        def get_account(self):
            return raw_account

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    acc = await alpaca_client.get_account()
    assert acc.multiplier == "1"
    assert acc.cash == Decimal("12345.67")
    assert acc.status == "ACTIVE"
    assert acc.account_blocked is False


async def test_get_positions_parses_list(monkeypatch):
    raw = [
        SimpleNamespace(
            symbol="SPY", qty="10", avg_entry_price="450",
            market_value="4600", unrealized_pl="100",
            current_price="460", side="long", asset_class="us_equity",
        ),
        SimpleNamespace(
            symbol="QQQ", qty="-5", avg_entry_price="400",
            market_value="-2050", unrealized_pl="-50",
            current_price="410", side="short", asset_class="us_equity",
        ),
    ]

    class FakeTrading:
        def __init__(self, **_):
            pass

        def get_all_positions(self):
            return raw

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())

    positions = await alpaca_client.get_positions()
    assert len(positions) == 2
    assert positions[0].symbol == "SPY"
    assert positions[0].qty == Decimal("10")
    assert positions[1].qty == Decimal("-5")


async def test_cancel_all_orders_returns_count(monkeypatch):
    class FakeTrading:
        def __init__(self, **_):
            pass

        def cancel_orders(self):
            return ["resp1", "resp2", "resp3"]

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())
    assert await alpaca_client.cancel_all_orders() == 3


async def test_close_all_positions_returns_count(monkeypatch):
    received_kwargs: dict = {}

    class FakeTrading:
        def __init__(self, **_):
            pass

        def close_all_positions(self, cancel_orders=None):
            received_kwargs["cancel_orders"] = cancel_orders
            return ["closed1", "closed2"]

    monkeypatch.setattr(alpaca_client, "_trading_client", lambda: FakeTrading())
    assert await alpaca_client.close_all_positions(True) == 2
    assert received_kwargs["cancel_orders"] is True
