"""Intraday scan agent tests.

Mocks the news fetcher and router; in-memory SQLite for the Signal row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.intraday import scan
from integrations.alpaca_client import NewsArticle
from trademaster.db import Base, Signal, make_engine, make_session_factory
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _article(headline: str = "h", minutes_ago: int = 5, symbol: str = "SPY") -> NewsArticle:
    return NewsArticle(
        headline=headline,
        summary="summary",
        url="https://x",
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        symbols=(symbol,),
        source="alpaca",
    )


def _resp(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="deepseek",
        model="deepseek-v4-flash",
        input_tokens=200,
        output_tokens=80,
        cost_usd=Decimal("0.000050"),
        duration_ms=600,
    )


async def test_hold_response_yields_no_alert(monkeypatch, session_factory):
    async def fetcher(*_a, **_k):
        return [_article()]

    async def route(*_a, **_k):
        return _resp("HOLD")

    monkeypatch.setattr(scan, "route_to_model", route)

    signal, alert = await scan.run_intraday_scan(
        session_factory=session_factory, news_fetcher=fetcher
    )
    assert signal.action.value == "hold"
    assert alert is None

    with session_factory() as s:
        row = s.query(Signal).one()
        assert row.action == "hold"


async def test_actionable_response_yields_alert(monkeypatch, session_factory):
    async def fetcher(*_a, **_k):
        return [_article("Big news SPY up 2%")]

    async def route(*_a, **_k):
        return _resp(
            "SPY · headline says big rally on Fed dovish surprise · "
            "bullish · medium confidence · watch for IV crush."
        )

    monkeypatch.setattr(scan, "route_to_model", route)

    signal, alert = await scan.run_intraday_scan(
        session_factory=session_factory, news_fetcher=fetcher
    )
    assert signal.action.value == "alert_only"
    assert alert is not None
    assert "SPY" in alert

    with session_factory() as s:
        row = s.query(Signal).one()
        assert row.action == "alert_only"
        assert row.payload["news_count"] == 1


async def test_filters_articles_outside_minutes_back(monkeypatch, session_factory):
    async def fetcher(*_a, **_k):
        # Return articles spanning a wide window — agent should narrow.
        return [
            _article("recent", minutes_ago=5),
            _article("old", minutes_ago=120),
        ]

    captured_prompt = {"text": ""}

    async def route_capture(_task_type, prompt, **_k):
        captured_prompt["text"] = prompt
        return _resp("HOLD")

    monkeypatch.setattr(scan, "route_to_model", route_capture)

    await scan.run_intraday_scan(
        session_factory=session_factory,
        news_fetcher=fetcher,
        minutes_back=30,
    )
    assert "recent" in captured_prompt["text"]
    assert "old" not in captured_prompt["text"]


async def test_hold_with_extra_whitespace_still_holds(monkeypatch, session_factory):
    async def fetcher(*_a, **_k):
        return []

    async def route(*_a, **_k):
        return _resp("  HOLD  \n")

    monkeypatch.setattr(scan, "route_to_model", route)

    signal, alert = await scan.run_intraday_scan(
        session_factory=session_factory, news_fetcher=fetcher
    )
    assert signal.action.value == "hold"
    assert alert is None
