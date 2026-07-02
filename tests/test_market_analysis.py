"""Tests for the #research market-analysis synthesis (mid-day + close)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.research import market_analysis as ma
from integrations.alpaca_client import Bar
from trademaster.db import Base, make_engine, make_session_factory
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _bars(n: int = 60, start: float = 740.0) -> list[Bar]:
    t = datetime(2026, 6, 5, 14, 0, tzinfo=UTC)
    return [
        Bar(timestamp=t + timedelta(minutes=5 * i), open=Decimal(str(start)),
            high=Decimal(str(start + 0.3)), low=Decimal(str(start - 0.3)),
            close=Decimal(str(start + i * 0.05)), volume=1000, vwap=None)
        for i in range(n)
    ]


async def test_run_market_analysis_produces_report(monkeypatch, session_factory):
    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return [] if t == "VIX" else _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        return LLMResponse(
            text="**Trend & Regime**\nSPY grinding up, holding VWAP.",
            provider="deepseek", model="deepseek-v4-flash",
            input_tokens=500, output_tokens=100, cost_usd=Decimal("0.0005"),
            duration_ms=1000,
        )

    monkeypatch.setattr(ma, "route_to_model", fake_route)

    out = await ma.run_market_analysis(
        now=datetime(2026, 6, 5, 19, 0, tzinfo=UTC),
        bars_fetcher=fake_bars, news_fetcher=fake_news,
        session_factory=session_factory,
    )
    assert "Market Analysis" in out
    assert "Trend & Regime" in out


async def test_run_market_analysis_close_mode(monkeypatch, session_factory):
    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return [] if t == "VIX" else _bars()

    captured: dict[str, str] = {}

    async def fake_route(_task_type, prompt, **_k):
        captured["prompt"] = prompt
        return LLMResponse(text="wrap", provider="deepseek", model="deepseek-v4-flash",
                           input_tokens=1, output_tokens=1, cost_usd=Decimal("0"), duration_ms=1)

    monkeypatch.setattr(ma, "route_to_model", fake_route)
    out = await ma.run_market_analysis(
        now=datetime(2026, 6, 5, 20, 5, tzinfo=UTC), mode="close", bars_fetcher=fake_bars,
        news_fetcher=lambda *a, **k: _noop(), session_factory=session_factory,
    )
    assert "Tomorrow's Outlook" in out
    assert "Tomorrow's Bias" in captured["prompt"]


async def _noop():
    return []
