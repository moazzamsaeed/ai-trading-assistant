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


def _bars(n: int = 60, start: float = 100.0) -> list[Bar]:
    """Default n=60 so EMA50 + volume_ratio_20 are both populated. Tests of
    happy-path entry decisions need bootstrapped indicators since the
    directional scan blocks entries when ema50/volume_ratio_20 are None."""
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    return [_bar(t + timedelta(minutes=i * 5), start + i * 0.1) for i in range(n)]


def _vix_bars(level: float = 18.0, n: int = 5) -> list[Bar]:
    """Return fake VIX bars in the safe range (12–35)."""
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    return [_bar(t + timedelta(minutes=i * 5), level) for i in range(n)]


async def _fake_bars_with_vix(t: str, *, timeframe_minutes: int, limit: int, warmup_days: int = 0) -> list[Bar]:
    """Bars fetcher that returns VIX=18 for 'VIX' and normal bars for everything else."""
    if t == "VIX":
        return _vix_bars()
    return _bars()


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
    # Selective mode exits
    assert "+50%" in msg
    assert "-30%" in msg
    # Plain-language, not jargon
    assert "iron condor" not in msg.lower()
    assert "credit" not in msg.lower()


def test_format_signal_put_weekly():
    d = TickerDecision("NVDA", "BUY_PUT", 500.0, "WEEKLY", "MEDIUM", "bearish")
    msg = format_directional_signal(d, today=date(2026, 5, 11))
    assert "BUY a PUT" in msg
    assert "NVDA" in msg
    assert "this Friday" in msg  # 2026-05-15


def test_format_signal_aggressive_mode():
    d = TickerDecision("SPY", "BUY_CALL", 500.0, "0DTE", "HIGH", "strong breakout")
    msg = format_directional_signal(d, today=date(2026, 5, 12), mode="aggressive")
    assert "+100%" in msg
    assert "-50%" in msg
    assert "AGGRESSIVE" in msg


# ----------------- run_directional_scan -----------------


async def test_scan_actionable_returns_messages(monkeypatch, session_factory):
    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
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

    decisions, messages, _report = await agent.run_directional_scan(
        watchlist=("SPY", "QQQ"),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
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
    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        return _llm(
            '[{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,'
            '"conviction":"LOW","reasoning":"quiet"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages, _report = await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
    )
    assert decisions[0].action == "HOLD"
    assert messages == []
    with session_factory() as s:
        assert s.query(Signal).count() == 0  # no rows for HOLD


async def test_scan_handles_bars_fetch_failure(monkeypatch, session_factory):
    """One ticker fails to fetch bars; agent continues with empty data."""
    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
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

    decisions, _, _report = await agent.run_directional_scan(
        watchlist=("BROKEN", "SPY"),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
    )
    # The broken ticker still appears in decisions; bars just empty.
    assert any(d.ticker == "BROKEN" for d in decisions)
    assert "BROKEN" in captured_prompt["text"]


async def test_scan_empty_watchlist_returns_empty():
    decisions, messages, _report = await agent.run_directional_scan(watchlist=())
    assert decisions == []
    assert messages == []


async def test_scan_aggressive_passes_medium_and_high_conviction(monkeypatch, session_factory):
    """Bug 2 regression: aggressive mode must pass MEDIUM + HIGH, block LOW."""

    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        # SPY = HIGH, QQQ = MEDIUM — both should pass in aggressive mode
        return _llm(
            '[{"ticker":"SPY","action":"BUY_CALL","strike":745,"expiry":"0DTE",'
            '"conviction":"HIGH","reasoning":"strong"},'
            '{"ticker":"QQQ","action":"BUY_CALL","strike":400,"expiry":"WEEKLY",'
            '"conviction":"MEDIUM","reasoning":"moderate"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages, _report = await agent.run_directional_scan(
        watchlist=("SPY", "QQQ"),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
        mode="aggressive",
    )
    # Both HIGH and MEDIUM pass in aggressive mode
    assert len(messages) == 2
    tickers = {m.split("**")[1].split(" ")[0] for m in messages}
    assert "SPY" in tickers
    assert "QQQ" in tickers
    assert all("+100%" in m for m in messages)  # aggressive exit targets


async def test_scan_selective_blocks_medium_conviction(monkeypatch, session_factory):
    """Bug 2 regression: selective mode must only pass HIGH conviction signals."""

    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        # MEDIUM conviction — selective mode should block this
        return _llm(
            '[{"ticker":"SPY","action":"BUY_CALL","strike":745,"expiry":"0DTE",'
            '"conviction":"MEDIUM","reasoning":"moderate setup"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages, _report = await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
        mode="selective",
    )
    assert len(messages) == 0, "MEDIUM conviction must be blocked in selective mode"


async def test_scan_selective_passes_high_conviction(monkeypatch, session_factory):
    """Selective mode executes HIGH conviction signals with tighter exit targets."""

    async def fake_bars(t, *, timeframe_minutes, limit, warmup_days=0):
        return _bars()

    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        return _llm(
            '[{"ticker":"SPY","action":"BUY_CALL","strike":745,"expiry":"0DTE",'
            '"conviction":"HIGH","reasoning":"strong breakout"}]'
        )

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, messages, _report = await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
        mode="selective",
    )
    assert len(messages) == 1
    assert "+50%" in messages[0]  # selective exit targets


# ----------------- intraday price-path summary -----------------


def _path_bars(seq: list[tuple[float, float, float, float]]) -> list[Bar]:
    """Build 5-min bars from (open, high, low, close) tuples."""
    t = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
    out = []
    for i, (o, h, low, c) in enumerate(seq):
        out.append(Bar(
            timestamp=t + timedelta(minutes=5 * i),
            open=Decimal(str(o)), high=Decimal(str(h)),
            low=Decimal(str(low)), close=Decimal(str(c)),
            volume=1000, vwap=None,
        ))
    return out


def test_price_path_too_few_bars_returns_empty():
    assert agent._summarize_price_path(_path_bars([(740, 740.5, 739.8, 740.2)]), 740.2) == ""


def test_price_path_grinding_up_detects_structure_and_levels():
    bars = _path_bars([
        (740.0, 740.5, 739.8, 740.3), (740.3, 741.2, 740.1, 741.0),
        (741.0, 742.0, 740.8, 741.8), (741.8, 743.8, 741.7, 743.0),
        (743.0, 743.8, 742.6, 742.9), (742.9, 743.5, 742.7, 743.1),
    ])
    out = agent._summarize_price_path(bars, 743.1)
    assert "ranged $739.80–$743.80" in out
    assert "near session high" in out
    assert "grinding up" in out
    assert "743.80" in out and "resistance" in out  # tested twice


def test_price_path_flat_when_no_range():
    bars = _path_bars([(740.0, 740.0, 740.0, 740.0)] * 6)
    out = agent._summarize_price_path(bars, 740.0)
    assert "flat" in out
    assert "choppy/flat" in out


# ----------------- key levels map -----------------


def test_key_levels_splits_resistance_and_support():
    md = {"prev_high": 745.3, "prev_low": 739.8, "prev_close": 741.0,
          "ma5": 742.2, "ma10": 740.5}
    out = agent._build_key_levels_block(743.1, md, 740.5, 739.8, 743.8, 739.8, 742.4)
    assert "KEY LEVELS (SPY $743.10)" in out
    assert "Resistance:" in out and "Support:" in out
    # prev high (745.30) is above price → resistance; VWAP (742.40) below → support
    res = [ln for ln in out.splitlines() if "Resistance" in ln][0]
    sup = [ln for ln in out.splitlines() if "Support" in ln][0]
    assert "745.30" in res and "prev high" in res
    assert "742.40" in sup and "VWAP" in sup


def test_key_levels_empty_price_returns_empty():
    assert agent._build_key_levels_block(0, {}, 0, 0, 0, 0, 0) == ""


# ----------------- position context -----------------


def test_position_context_empty_returns_empty():
    empty = {"open": [], "today_closed": []}
    assert agent._format_position_context(empty, now=datetime.now(UTC)) == ""


def test_position_context_renders_open_and_today():
    pc = {
        "open": [{"ticker": "SPY", "action": "BUY_CALL", "conviction": "HIGH",
                  "qty": 2, "entry_price": 1.40, "peak_pnl_pct": 18.0,
                  "opened_at": datetime(2026, 5, 29, 15, 5, tzinfo=UTC)}],
        "today_closed": [
            {"ticker": "SPY", "action": "BUY_CALL", "realized_pnl": -952.0,
             "exit_reason": "stop_loss"},
            {"ticker": "SPY", "action": "BUY_CALL", "realized_pnl": 232.0,
             "exit_reason": "profit_target"},
        ],
    }
    out = agent._format_position_context(pc, now=datetime.now(UTC))
    assert "YOUR POSITIONS & TODAY" in out
    assert "ALREADY in this direction" in out
    assert "peak +18%" in out
    assert "1W/1L" in out
    assert "stop_loss" in out


# ----------------- get_directional_trade_context (DB) -----------------


def test_trade_context_query_open_and_closed(session_factory):
    from trademaster.db import Trade, get_directional_trade_context

    now = datetime.now(UTC)
    with session_factory() as s:
        s.add(Trade(
            opened_at=now, closed_at=None, symbol="SPY250529C00740000",
            asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("2"), entry_price=Decimal("1.40"),
            extra={"ticker": "SPY", "action": "BUY_CALL", "conviction": "HIGH",
                   "peak_pnl_pct": 12.0},
        ))
        s.add(Trade(
            opened_at=now, closed_at=now, symbol="SPY250529P00740000",
            asset_class="option", side="buy", strategy="directional_put",
            qty=Decimal("1"), entry_price=Decimal("0.90"),
            exit_price=Decimal("0.45"), realized_pnl_usd=Decimal("-45"),
            extra={"ticker": "SPY", "action": "BUY_PUT", "exit_reason": "stop_loss"},
        ))
        s.commit()

    ctx = get_directional_trade_context(session_factory)
    assert len(ctx["open"]) == 1
    assert ctx["open"][0]["action"] == "BUY_CALL"
    assert ctx["open"][0]["peak_pnl_pct"] == 12.0
    assert len(ctx["today_closed"]) == 1
    assert ctx["today_closed"][0]["realized_pnl"] == -45.0


async def test_scan_injects_open_position_into_prompt(monkeypatch, session_factory):
    """An open directional position must surface in the LLM prompt."""
    from trademaster.db import Trade

    now = datetime.now(UTC)
    with session_factory() as s:
        s.add(Trade(
            opened_at=now, closed_at=None, symbol="SPY250529C00740000",
            asset_class="option", side="buy", strategy="directional_call",
            qty=Decimal("2"), entry_price=Decimal("1.40"),
            extra={"ticker": "SPY", "action": "BUY_CALL", "conviction": "HIGH"},
        ))
        s.commit()

    async def fake_news(*_a, **_k):
        return []

    captured: dict = {}

    async def fake_route(_task_type, prompt, **_k):
        captured["text"] = prompt
        return _llm('[{"ticker":"SPY","action":"HOLD","strike":null,"expiry":null,'
                    '"conviction":"LOW","reasoning":"already long"}]')

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
    )
    assert "YOUR POSITIONS & TODAY" in captured["text"]
    assert "ALREADY in this direction" in captured["text"]


# ----------------- green lights + plan/entry signal format -----------------


_CALL_ANALYSIS = {
    "spy_price": 743.10, "vwap": 742.40, "rsi9": 61.0, "ema20": 742.9,
    "ema50": 742.1, "macd": 0.12, "macd_signal": 0.08, "volume_ratio": 1.6,
    "next_target": "$745.30 (prev high)",
}


def test_green_lights_all_confirm_for_aligned_call():
    lines = agent._green_lights("BUY_CALL", _CALL_ANALYSIS)
    assert len(lines) == 5
    assert all(line.startswith("✅") for line in lines)
    assert "above VWAP" in lines[0]


def test_green_lights_flags_weak_volume_and_bad_ema():
    a = dict(_CALL_ANALYSIS, volume_ratio=0.7, ema20=742.0, ema50=742.5)
    lines = agent._green_lights("BUY_CALL", a)
    assert any(line.startswith("⚪") and "Volume" in line for line in lines)
    assert any(line.startswith("⚪") and "EMA" in line for line in lines)


def test_next_target_picks_nearest_resistance_for_call():
    ctx = {"multi_day": {"prev_high": 745.3, "prev_low": 739.8},
           "session_high": 743.8, "session_low": 740.1, "orb_high": 740.5, "orb_low": 739.8}
    out = agent._next_target("BUY_CALL", 743.1, ctx)
    assert out == "$743.80 (session high)"  # nearest level above 743.1


def test_next_target_picks_nearest_support_for_put():
    ctx = {"multi_day": {"prev_high": 745.3, "prev_low": 739.8},
           "session_high": 743.8, "session_low": 740.1, "orb_high": 740.5, "orb_low": 739.8}
    out = agent._next_target("BUY_PUT", 741.0, ctx)
    assert out == "$740.50 (ORB high)"  # nearest level below 741.0


def test_format_plan_signal_has_greens_trigger_and_target():
    d = TickerDecision("SPY", "BUY_CALL", 743.0, "0DTE", "HIGH", "broke ORH",
                       analysis=_CALL_ANALYSIS)
    out = agent.format_directional_plan(d, today=date(2026, 5, 29), mode="aggressive")
    assert "setup forming" in out
    assert "Green lights (5/5)" in out
    assert "enter as **SPY trades $743.10**" in out
    assert "$745.30 (prev high)" in out
    assert "scale out 25% at +100% gain" in out  # derived from the live ladder


def test_format_plan_signal_survives_missing_analysis():
    d = TickerDecision("SPY", "BUY_CALL", 743.0, "0DTE", "HIGH", "x", analysis=None)
    out = agent.format_directional_plan(d, today=date(2026, 5, 29), mode="aggressive")
    assert "setup forming" in out
    assert "the current level" in out  # no price → graceful fallback


def test_format_entry_combined_mentions_scale_out_reporting():
    d = TickerDecision("SPY", "BUY_CALL", 743.0, "0DTE", "HIGH", "x", analysis=_CALL_ANALYSIS)
    out = agent.format_entry_combined(
        d, today=date(2026, 5, 29), mode="aggressive", trade_id=1, qty=2,
        occ="SPY260529C00743000", entry_premium=Decimal("1.42"), total_cost=Decimal("284.00"),
    )
    assert "model entered" in out
    assert "scale-out" in out and "final close" in out


async def test_scan_attaches_analysis_to_actionable(monkeypatch, session_factory):
    """Actionable decisions must carry the indicator analysis for the signals."""
    async def fake_news(*_a, **_k):
        return []

    async def fake_route(_task_type, _prompt, **_k):
        return _llm('[{"ticker":"SPY","action":"BUY_CALL","strike":743.0,"expiry":"0DTE",'
                    '"conviction":"HIGH","reasoning":"breakout"}]')

    monkeypatch.setattr(agent, "route_to_model", fake_route)

    decisions, _msgs, _report = await agent.run_directional_scan(
        watchlist=("SPY",),
        session_factory=session_factory,
        bars_fetcher=_fake_bars_with_vix,
        news_fetcher=fake_news,
        mode="aggressive",
    )
    spy = [d for d in decisions if d.ticker == "SPY"][0]
    assert spy.action == "BUY_CALL"
    assert spy.analysis is not None
    assert spy.analysis["spy_price"] is not None
    assert "volume_ratio" in spy.analysis


# ----------------- pre-emptive "setup forming" alert (Option A) -----------------


def test_log_near_misses_returns_forming(session_factory):
    """A 3/4 bullish near-miss (missing volume) is returned for a forming alert."""
    snaps = {"SPY": {"last_close": "743.1", "vwap": "742.4", "rsi9": "58",
                     "ema20": "742.9", "ema50": "742.1", "volume_ratio_20": "0.8",
                     "macd": "0.1", "macd_signal": "0.05"}}
    holds = [TickerDecision("SPY", "HOLD", None, None, "LOW", "held — low volume")]
    forming = agent._log_near_misses(
        holds, ticker_snaps=snaps, spy_regime="BULL", session_factory=session_factory,
    )
    assert "SPY" in forming
    assert forming["SPY"]["would_be_action"] == "BUY_CALL"
    assert forming["SPY"]["criteria_met"] == 3
    assert "volume ≥ 1.0×" in forming["SPY"]["missing"]


def test_log_near_misses_empty_when_genuine_hold(session_factory):
    """A weak setup (≤2 criteria) returns no forming entry."""
    # price>VWAP + ema_bull = 2 bull, but RSI 30 out-of-band and vol low → max 2.
    snaps = {"SPY": {"last_close": "742.5", "vwap": "742.4", "rsi9": "30",
                     "ema20": "742.9", "ema50": "742.1", "volume_ratio_20": "0.5"}}
    holds = [TickerDecision("SPY", "HOLD", None, None, "LOW", "no edge")]
    forming = agent._log_near_misses(
        holds, ticker_snaps=snaps, spy_regime="NEUTRAL", session_factory=session_factory,
    )
    assert forming == {}


def test_format_setup_forming_renders_watch_alert():
    d = TickerDecision(
        "SPY", "HOLD", None, None, "LOW", "forming",
        analysis={"spy_price": 743.1, "vwap": 742.4, "rsi9": 58.0, "ema20": 742.9,
                  "ema50": 742.1, "macd": "0.10", "macd_signal": "0.06", "volume_ratio": 0.8,
                  "forming": {"would_be_action": "BUY_CALL", "criteria_met": 3,
                              "missing": ["volume ≥ 1.0×"]}},
    )
    out = agent.format_setup_forming(d, mode="aggressive")
    assert "setup forming" in out and "watching" in out
    assert "Building (3/4 criteria)" in out
    assert "Still needs: volume" in out
    assert "model will enter a CALL" in out
    assert "not an entry yet" in out.lower()


# ---------------------------------------------------------------------------
# is_fresh_leg — re-entry freshness gate (fix B, 2026-06-10)
# ---------------------------------------------------------------------------

def _fresh_analysis(price, *, sl=730.0, sh=740.0, vol=1.0):
    return {"spy_price": price, "session_low": sl, "session_high": sh, "volume_ratio": vol}


def test_fresh_leg_blocks_put_chase():
    # Mid-move, tepid volume, no pullback, not making new lows → a chase → blocked.
    a = _fresh_analysis(731.0, vol=1.0)  # 1pt off the 730 low (10% of range), vol < 1.5
    assert agent.is_fresh_leg("BUY_PUT", a, pullback_range_frac=0.30, fresh_volume_min=1.5) is False


def test_fresh_leg_allows_put_pullback():
    # Price retraced 40% of the range back up from the low → room for a new leg.
    a = _fresh_analysis(734.0, vol=1.0)  # (734-730)/10 = 40% ≥ 30%
    assert agent.is_fresh_leg("BUY_PUT", a, pullback_range_frac=0.30, fresh_volume_min=1.5) is True


def test_fresh_leg_allows_put_new_low_break_with_volume():
    # Making new session lows with strong volume → fresh breakdown leg.
    a = _fresh_analysis(730.0, vol=1.6)
    assert agent.is_fresh_leg("BUY_PUT", a, pullback_range_frac=0.30, fresh_volume_min=1.5) is True


def test_fresh_leg_new_low_break_needs_volume():
    # At the lows but volume tepid → exhausted, not fresh → blocked.
    a = _fresh_analysis(730.0, vol=1.1)
    assert agent.is_fresh_leg("BUY_PUT", a, pullback_range_frac=0.30, fresh_volume_min=1.5) is False


def test_fresh_leg_allows_call_new_high_break_with_volume():
    a = _fresh_analysis(739.8, vol=1.6)  # at the 740 high w/ volume
    assert agent.is_fresh_leg("BUY_CALL", a, pullback_range_frac=0.30, fresh_volume_min=1.5) is True


def test_fresh_leg_missing_levels_is_conservative():
    # No level data → treat as a chase (False), don't risk the gated entry.
    out = agent.is_fresh_leg(
        "BUY_PUT", {"spy_price": 730.0}, pullback_range_frac=0.30, fresh_volume_min=1.5
    )
    assert out is False
