"""Directional intraday agent tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from agents.directional import intraday as agent
from agents.directional.intraday import (
    TickerDecision,
    _next_friday,
    _parse_decisions,
    format_directional_signal,
)
from integrations.alpaca_client import Bar, NewsArticle
from trademaster.db import Base, Signal, make_engine, make_session_factory
from trademaster.llm.types import LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _bar(t: datetime, close: float, vol: int = 1000) -> Bar:
    return Bar(
        timestamp=t,
        open=Decimal(str(close - 0.1)),
        high=Decimal(str(close + 0.2)),
        low=Decimal(str(close - 0.2)),
        close=Decimal(str(close)),
        volume=vol,
        vwap=Decimal(str(close)),
    )


def _bars(n: int = 30, start: float = 100.0) -> list[Bar]:
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    return [_bar(t + timedelta(minutes=i * 5), start + i * 0.1) for i in range(n)]


def _llm(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, provider="deepseek", model="deepseek-v4-flash",
        input_tokens=500, output_tokens=120,
        cost_usd=Decimal("0.000200"), duration_ms=1500,
    )


# ----------------- _parse_decisions -----------------


def test_parse_decisions_clean():
    txt = '[{"ticker":"SPY","action":"BUY_CALL","strike":745.0,"expiry":"0DTE",' \
          '"conviction":"HIGH","reasoning":"breakout"}]'
    out = _parse_decisions(txt, ["SPY"])
    assert len(out) == 1
    d = out[0]
    assert d.ticker == "SPY"
    assert d.action == "BUY_CALL"
    assert d.strike == 745.0
    assert d.expiry == "0DTE"
    assert d.conviction == "HIGH"


def test_parse_decisions_hold():
    txt = '[{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,' \
          '"conviction":"LOW","reasoning":"no edge"}]'
    out = _parse_decisions(txt, ["SPY"])
    assert out[0].action == "HOLD"
    assert out[0].strike is None


def test_parse_decisions_strips_code_fence():
    txt = '```json\n[{"ticker":"NVDA","action":"BUY_PUT","strike":500,"expiry":"WEEKLY",' \
          '"conviction":"MEDIUM","reasoning":"x"}]\n```'
    out = _parse_decisions(txt, ["NVDA"])
    assert out[0].action == "BUY_PUT"


def test_parse_decisions_garbage_defaults_to_hold():
    out = _parse_decisions("not json", ["SPY", "QQQ"])
    assert all(d.action == "HOLD" for d in out)
    assert all("parse failed" in d.reasoning for d in out)


def test_parse_decisions_invalid_action_becomes_hold():
    txt = '[{"ticker":"SPY","action":"MAYBE","strike":500,"expiry":"0DTE","conviction":"HIGH"}]'
    out = _parse_decisions(txt, ["SPY"])
    assert out[0].action == "HOLD"


def test_parse_decisions_preserves_input_order():
    # LLM returns tickers in reverse — agent re-orders to match input.
    txt = (
        '[{"ticker":"QQQ","action":"BUY_CALL","strike":400,"expiry":"0DTE",'
        '"conviction":"HIGH","reasoning":"qqq"},'
        '{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,'
        '"conviction":"LOW","reasoning":"spy"}]'
    )
    out = _parse_decisions(txt, ["SPY", "QQQ"])
    assert out[0].ticker == "SPY"
    assert out[1].ticker == "QQQ"


def test_parse_decisions_missing_ticker_filled_as_hold():
    txt = '[{"ticker":"SPY","action":"BUY_CALL","strike":745,"expiry":"0DTE",' \
          '"conviction":"HIGH","reasoning":"ok"}]'
    out = _parse_decisions(txt, ["SPY", "MISSING"])
    assert out[0].action == "BUY_CALL"
    assert out[1].action == "HOLD"
    assert "missing" in out[1].reasoning.lower()


# ----------------- _next_friday -----------------


def test_next_friday_from_monday():
    # Mon May 11 → next Fri = May 15
    assert _next_friday(date(2026, 5, 11)) == date(2026, 5, 15)


def test_next_friday_from_friday_returns_following():
    # Fri May 15 → following Fri = May 22 (avoid returning same-day)
    assert _next_friday(date(2026, 5, 15)) == date(2026, 5, 22)


# ----------------- format_directional_signal -----------------


def test_format_signal_call_0dte():
    d = TickerDecision("SPY", "BUY_CALL", 745.0, "0DTE", "HIGH", "breakout above VWAP")
    msg = format_directional_signal(d, today=date(2026, 5, 12))
    assert "BUY a CALL" in msg
    assert "SPY" in msg
    assert "$745" in msg
    assert "today (2026-05-12)" in msg
    assert "HIGH" in msg
    assert "breakout above VWAP" in msg
    # Plain-language, not jargon
    assert "iron condor" not in msg.lower()
    assert "credit" not in msg.lower()


def test_format_signal_put_weekly():
    d = TickerDecision("NVDA", "BUY_PUT", 500.0, "WEEKLY", "MEDIUM", "bearish")
    msg = format_directional_signal(d, today=date(2026, 5, 11))
    assert "BUY a PUT" in msg
    assert "NVDA" in msg
    assert "this Friday" in msg  # 2026-05-15


# ----------------- run_directional_scan -----------------


async def test_scan_actionable_returns_messages(monkeypatch, session_factory):
    async def fake_bars(t, *, timeframe_minutes, limit):
        return _bars()

    async def fake_news(symbols, *, hours_back, limit):
        return [
            NewsArticle(
                headline=f"{symbols[0]} breakout on volume",
                summary="surge",
                url="x",
                created_at=datetime.now(UTC),
                symbols=tuple(symbols),
                source="alpaca",
            )
        ]

    async def fake_route(_task_type, _prompt, **_k):
        return _llm(
            '[{"ticker":"SPY","action":"BUY_CALL","strike":745,"expiry":"0DTE",'
            '"conviction":"HIGH","reasoning":"strong breakout"},'
            '{"ticker":"QQQ","action":"HOLD","strike":null,"expiry":null,'
            '"conviction":"LOW","reasoning":"flat"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages = await agent.run_directional_scan(
        watchlist=("SPY", "QQQ"),
        session_factory=session_factory,
        bars_fetcher=fake_bars,
        news_fetcher=fake_news,
    )
    assert len(decisions) == 2
    assert decisions[0].action == "BUY_CALL"
    assert decisions[1].action == "HOLD"
    # Only the BUY_CALL produces a signal message
    assert len(messages) == 1
    assert "BUY a CALL" in messages[0]
    assert "SPY" in messages[0]
    assert "$745" in messages[0]

    # Signal row persisted for the actionable decision only
    with session_factory() as s:
        rows = s.query(Signal).all()
        assert len(rows) == 1
        assert rows[0].symbol == "SPY"
        assert rows[0].payload["action"] == "BUY_CALL"


async def test_scan_all_hold_returns_no_messages(monkeypatch, session_factory):
    async def fake_bars(t, *, timeframe_minutes, limit):
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        return _llm(
            '[{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,'
            '"conviction":"LOW","reasoning":"quiet"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages = await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=fake_bars,
        news_fetcher=fake_news,
    )
    assert decisions[0].action == "HOLD"
    assert messages == []
    with session_factory() as s:
        assert s.query(Signal).count() == 0  # no rows for HOLD


async def test_scan_handles_bars_fetch_failure(monkeypatch, session_factory):
    """One ticker fails to fetch bars; agent continues with empty data."""
    async def fake_bars(t, *, timeframe_minutes, limit):
        if t == "BROKEN":
            raise RuntimeError("alpaca 500")
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    captured_prompt: dict = {}

    async def fake_route(_task_type, prompt, **_k):
        captured_prompt["text"] = prompt
        return _llm(
            '[{"ticker":"BROKEN","action":"HOLD","strike":null,"expiry":null,'
            '"conviction":"LOW","reasoning":"no data"},'
            '{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,'
            '"conviction":"LOW","reasoning":"flat"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, _ = await agent.run_directional_scan(
        watchlist=("BROKEN", "SPY"),
        session_factory=session_factory,
        bars_fetcher=fake_bars,
        news_fetcher=fake_news,
    )
    # The broken ticker still appears in decisions; bars just empty.
    assert any(d.ticker == "BROKEN" for d in decisions)
    assert "BROKEN" in captured_prompt["text"]


async def test_scan_empty_watchlist_returns_empty():
    decisions, messages = await agent.run_directional_scan(watchlist=())
    assert decisions == []
    assert messages == []
