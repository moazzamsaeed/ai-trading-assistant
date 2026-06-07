"""Pre-market research agent tests.

Mocks the Alpaca news fetcher and the router, so no real API calls happen.
Asserts the briefing text is returned and the signal is persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agents.research import premarket
from integrations.alpaca_client import NewsArticle
from trademaster.db import Base, Signal, make_engine, make_session_factory
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _fake_articles() -> list[NewsArticle]:
    return [
        NewsArticle(
            headline="SPY gaps up on Fed minutes",
            summary="Fed minutes signal dovish pivot",
            url="https://example.com/1",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
            symbols=("SPY",),
            source="alpaca",
        ),
    ]


async def test_briefing_runs_end_to_end(monkeypatch, session_factory):
    async def fake_fetcher(_symbols, *, hours_back=18, limit=50):
        return _fake_articles()

    async def fake_route(_task_type, _prompt, **_kwargs):
        return LLMResponse(
            text="## Overnight Summary\nMarkets up.\n\n## Synthesis\nWatch SPY.",
            provider="google",
            model="gemini-3.1-pro-preview",
            input_tokens=400,
            output_tokens=120,
            cost_usd=Decimal("0.002240"),
            duration_ms=1500,
        )

    monkeypatch.setattr(premarket, "route_to_model", fake_route)

    text, signal = await premarket.run_premarket_briefing(
        session_factory=session_factory,
        news_fetcher=fake_fetcher,
    )

    assert "Overnight Summary" in text
    assert signal.agent == "research"
    assert signal.action.value == "alert_only"
    assert signal.extra["news_count"] == 1

    with session_factory() as s:
        rows = s.query(Signal).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.agent == "research"
        assert row.action == "alert_only"
        assert row.payload["news_count"] == 1
        assert row.accepted is True


async def test_briefing_with_no_news(monkeypatch, session_factory):
    async def fake_fetcher(_symbols, *, hours_back=18, limit=50):
        return []

    captured_prompt: dict = {}

    async def fake_route(_task_type, prompt, **_kwargs):
        captured_prompt["text"] = prompt
        return LLMResponse(
            text="No notable items today.",
            provider="google",
            model="gemini-3.1-pro-preview",
            input_tokens=50,
            output_tokens=10,
            cost_usd=Decimal("0.00022"),
            duration_ms=900,
        )

    monkeypatch.setattr(premarket, "route_to_model", fake_route)

    text, signal = await premarket.run_premarket_briefing(
        session_factory=session_factory,
        news_fetcher=fake_fetcher,
    )

    assert "No notable items" in text
    assert signal.extra["news_count"] == 0
    assert "(no articles in window)" in captured_prompt["text"]


async def test_briefing_includes_week_window_and_upcoming_events(monkeypatch, session_factory):
    """Redesign: 7-day news window, broad tech tickers, upcoming macro events,
    and a prediction-focused prompt."""
    captured = {}

    async def fake_fetcher(symbols, *, hours_back=18, limit=50):
        captured["symbols"] = tuple(symbols)
        captured["hours_back"] = hours_back
        return _fake_articles()

    async def fake_route(_task_type, prompt, **_kwargs):
        captured["prompt"] = prompt
        return LLMResponse(
            text='## Synthesis\nUp.\n{"bias":"BULLISH","summary":"trend up","catalysts":["CPI"],"risks":["FOMC"]}',
            provider="google", model="gemini-2.5-pro",
            input_tokens=400, output_tokens=120,
            cost_usd=Decimal("0.002"), duration_ms=1500,
        )

    monkeypatch.setattr(premarket, "route_to_model", fake_route)

    await premarket.run_premarket_briefing(
        now=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),  # CPI 6/11 + FOMC 6/17 ahead
        session_factory=session_factory,
        news_fetcher=fake_fetcher,
    )
    # 7-day window, broad tech universe
    assert captured["hours_back"] == 168
    assert "NVDA" in captured["symbols"] and "SPY" in captured["symbols"]
    # prompt carries upcoming macro events + prediction framing
    assert "CPI Release" in captured["prompt"]
    assert "FOMC Decision" in captured["prompt"]
    assert "Prediction" in captured["prompt"]
