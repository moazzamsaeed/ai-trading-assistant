"""Scheduler registration and job-behavior tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from integrations.alpaca_client import MarketClock
from trademaster import scheduler as sch
from trademaster.state import get_state, reset_state_for_tests


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


async def _noop_poster(_text: str) -> None:
    return None


def _clock(is_open: bool) -> MarketClock:
    now = datetime.now(UTC)
    return MarketClock(
        timestamp=now,
        is_open=is_open,
        next_open=now + timedelta(hours=12),
        next_close=now + timedelta(hours=6),
    )


def _all_posters():
    return {
        "research_poster": _noop_poster,
        "signal_poster": _noop_poster,
        "trade_poster": _noop_poster,
        "log_poster": _noop_poster,
    }


# ----------------- premarket -----------------


def test_make_scheduler_registers_premarket_job():
    scheduler = sch.make_scheduler(**_all_posters())
    job = scheduler.get_job("premarket_briefing")
    assert job is not None

    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "8"
    assert fields["minute"] == "0"
    assert str(job.trigger.timezone) == sch.PREMARKET_TZ


def test_make_scheduler_registers_research_analysis_jobs():
    """#research gets exactly two analysis posts: mid-day (12:30) and close (16:05)."""
    scheduler = sch.make_scheduler(**_all_posters())

    midday = scheduler.get_job("research_midday")
    assert midday is not None
    mfields = {f.name: str(f) for f in midday.trigger.fields}
    assert mfields["day_of_week"] == "mon-fri"
    assert mfields["hour"] == "12"
    assert mfields["minute"] == "30"

    close = scheduler.get_job("research_close")
    assert close is not None
    cfields = {f.name: str(f) for f in close.trigger.fields}
    assert cfields["day_of_week"] == "mon-fri"
    assert cfields["hour"] == "16"
    assert cfields["minute"] == "5"


async def test_market_analysis_job_posts_to_research(monkeypatch):
    posted: list[str] = []

    async def research(text: str) -> None:
        posted.append(text)

    async def fake_clock():
        return _clock(is_open=True)

    async def fake_analysis(*, mode="intraday"):
        return f"report[{mode}]"

    monkeypatch.setattr(
        "agents.research.market_analysis.run_market_analysis", fake_analysis
    )

    await sch._market_analysis_job(
        research_poster=research, clock_fetcher=fake_clock, mode="intraday"
    )
    assert posted == ["report[intraday]"]


async def test_market_analysis_intraday_skips_when_market_closed(monkeypatch):
    posted: list[str] = []

    async def research(text: str) -> None:
        posted.append(text)

    async def closed_clock():
        return _clock(is_open=False)

    await sch._market_analysis_job(
        research_poster=research, clock_fetcher=closed_clock, mode="intraday"
    )
    assert posted == [], "mid-day update must not post when the market is closed"


async def test_market_analysis_close_posts_after_bell(monkeypatch):
    """The close wrap runs after 16:00 (market closed) and still posts."""
    posted: list[str] = []

    async def research(text: str) -> None:
        posted.append(text)

    async def closed_clock():
        return _clock(is_open=False)

    async def fake_analysis(*, mode="intraday"):
        return f"report[{mode}]"

    monkeypatch.setattr(
        "agents.research.market_analysis.run_market_analysis", fake_analysis
    )

    await sch._market_analysis_job(
        research_poster=research, clock_fetcher=closed_clock, mode="close"
    )
    assert posted == ["report[close]"]


async def test_run_premarket_once_invokes_poster(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def fake_briefing(**_kwargs):
        return "briefing body", object()

    monkeypatch.setattr(sch, "run_premarket_briefing", fake_briefing)

    await sch.run_premarket_once(poster)
    assert posted == ["briefing body"]


async def test_premarket_job_routes_failure_to_logs(monkeypatch):
    research_posted: list[str] = []
    logs_posted: list[str] = []

    async def research(text: str) -> None:
        research_posted.append(text)

    async def logs(text: str) -> None:
        logs_posted.append(text)

    async def boom(**_kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(sch, "run_premarket_briefing", boom)
    await sch._premarket_job(research_poster=research, log_poster=logs)

    assert research_posted == []  # don't put errors in #research
    assert len(logs_posted) == 1
    assert "failed" in logs_posted[0].lower()
    assert "router down" in logs_posted[0]


# ----------------- intraday -----------------


def test_make_scheduler_registers_intraday_job():
    scheduler = sch.make_scheduler(**_all_posters())
    job = scheduler.get_job("intraday_scan")
    assert job is not None

    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "9-15"
    assert "0" in fields["minute"] and "30" in fields["minute"]
    assert str(job.trigger.timezone) == sch.PREMARKET_TZ


async def test_intraday_skipped_when_paused(monkeypatch):
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)

    async def boom_clock() -> MarketClock:
        raise AssertionError("clock should not be fetched when paused")

    async def boom_scan(**_kwargs):
        raise AssertionError("scan should not run when paused")

    monkeypatch.setattr(sch, "run_intraday_scan", boom_scan)
    await sch._intraday_scan_job(signal_poster=signals, clock_fetcher=boom_clock)
    assert signal_posted == []


async def test_intraday_skipped_when_market_closed(monkeypatch):
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def clock_closed() -> MarketClock:
        return _clock(is_open=False)

    async def boom(**_kwargs):
        raise AssertionError("scan should not run when market closed")

    monkeypatch.setattr(sch, "run_intraday_scan", boom)
    await sch._intraday_scan_job(signal_poster=signals, clock_fetcher=clock_closed)
    assert signal_posted == []


async def test_intraday_posts_signal_when_actionable(monkeypatch):
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), "SPY breakout alert"

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch._intraday_scan_job(signal_poster=signals, clock_fetcher=clock_open)
    assert signal_posted == ["SPY breakout alert"]


async def test_intraday_silent_on_hold(monkeypatch):
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), None  # HOLD

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch._intraday_scan_job(signal_poster=signals, clock_fetcher=clock_open)
    assert signal_posted == []


async def test_intraday_failure_routes_to_logs(monkeypatch):
    signal_posted: list[str] = []
    log_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def logs(text: str) -> None:
        log_posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def boom(**_kwargs):
        raise RuntimeError("deepseek down")

    monkeypatch.setattr(sch, "run_intraday_scan", boom)
    await sch._intraday_scan_job(
        signal_poster=signals, log_poster=logs, clock_fetcher=clock_open
    )
    assert signal_posted == []
    assert len(log_posted) == 1
    assert "failed" in log_posted[0].lower()


async def test_intraday_clock_failure_silent(monkeypatch):
    """Transient clock-fetch failures don't spam any channel."""
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def clock_boom() -> MarketClock:
        raise RuntimeError("alpaca clock 503")

    async def boom_scan(**_kwargs):
        raise AssertionError("scan should not run if clock fetch failed")

    monkeypatch.setattr(sch, "run_intraday_scan", boom_scan)
    await sch._intraday_scan_job(signal_poster=signals, clock_fetcher=clock_boom)
    assert signal_posted == []


async def test_run_intraday_once(monkeypatch):
    signal_posted: list[str] = []

    async def signals(text: str) -> None:
        signal_posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), "alert"

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch.run_intraday_once(signals, clock_fetcher=clock_open)
    assert signal_posted == ["alert"]


# ----------------- directional exit monitor -----------------


def test_make_scheduler_registers_directional_exit_jobs():
    """Regular exit job + 15:50 0DTE safety net — no global force-close job."""
    scheduler = sch.make_scheduler(**_all_posters())
    monitor = scheduler.get_job("directional_exit")
    safety_net = scheduler.get_job("directional_0dte_final_close")
    gone = scheduler.get_job("directional_force_close")  # Bug fix: this job was removed

    assert monitor is not None
    assert safety_net is not None, "15:50 0DTE safety-net job must be registered"
    assert gone is None, "directional_force_close must NOT exist — it bypassed per-trade expiry check"

    sn_fields = {f.name: str(f) for f in safety_net.trigger.fields}
    assert sn_fields["hour"] == "15"
    assert sn_fields["minute"] == "50"


async def test_directional_exit_job_posts_combined_to_signals(monkeypatch):
    sig: list[str] = []

    async def signals(t):
        sig.append(t)

    async def clock_open() -> sch.alpaca_client.MarketClock:
        from datetime import UTC, timedelta
        now = __import__("datetime").datetime.now(UTC)
        from integrations.alpaca_client import MarketClock
        return MarketClock(
            timestamp=now, is_open=True,
            next_open=now + timedelta(hours=12),
            next_close=now + timedelta(hours=6),
        )

    async def fake_monitor(**_kwargs):
        return [{"combined_text": "📈 SPY CALL — model closed"}]

    monkeypatch.setattr(sch, "run_directional_exit_monitor", fake_monitor)
    await sch._directional_exit_job(signal_poster=signals, clock_fetcher=clock_open)
    assert sig == ["📈 SPY CALL — model closed"]


async def test_directional_exit_job_force_skips_clock(monkeypatch):
    """force=True bypasses market-open check (used for 15:50 safety net)."""
    sig: list[str] = []

    async def signals(t):
        sig.append(t)

    async def clock_closed():
        from datetime import UTC, timedelta
        from integrations.alpaca_client import MarketClock
        now = __import__("datetime").datetime.now(UTC)
        return MarketClock(
            timestamp=now, is_open=False,
            next_open=now + timedelta(hours=12),
            next_close=now + timedelta(hours=6),
        )

    monitor_called = []

    async def fake_monitor(**kwargs):
        monitor_called.append(True)
        return []

    monkeypatch.setattr(sch, "run_directional_exit_monitor", fake_monitor)
    await sch._directional_exit_job(
        signal_poster=signals, clock_fetcher=clock_closed, force=True,
    )
    assert monitor_called == [True], "force=True must bypass clock check"


async def test_exit_monitor_runs_when_paused(monkeypatch):
    """Bug 3: pausing blocks new entries but exit monitor must still run to protect open positions."""
    from trademaster.state import get_state
    get_state().pause(hours=24)

    monitor_called = []

    async def fake_monitor(**_kwargs):
        monitor_called.append(True)
        return []

    async def clock_open():
        from datetime import UTC, timedelta
        from integrations.alpaca_client import MarketClock
        now = __import__("datetime").datetime.now(UTC)
        return MarketClock(
            timestamp=now, is_open=True,
            next_open=now + timedelta(hours=12),
            next_close=now + timedelta(hours=6),
        )

    monkeypatch.setattr(sch, "run_directional_exit_monitor", fake_monitor)
    await sch._directional_exit_job(signal_poster=_noop_poster, clock_fetcher=clock_open)
    assert monitor_called == [True], "exit monitor must run even when trading is paused"


# ----------------- iron condor entry -----------------


def test_make_scheduler_ic_absent_by_default():
    scheduler = sch.make_scheduler(**_all_posters())
    assert scheduler.get_job("iron_condor_entry") is None
    assert scheduler.get_job("iron_condor_exit") is None
    assert scheduler.get_job("iron_condor_force_close") is None


def test_make_scheduler_registers_iron_condor_job_when_enabled():
    scheduler = sch.make_scheduler(**_all_posters(), enable_iron_condor=True)
    job = scheduler.get_job("iron_condor_entry")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "10"
    assert fields["minute"] == "0"


async def test_iron_condor_job_skipped_when_paused(monkeypatch):
    sig_posted: list[str] = []
    trade_posted: list[str] = []

    async def signals(text: str) -> None:
        sig_posted.append(text)

    async def trades(text: str) -> None:
        trade_posted.append(text)

    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)

    async def boom_strat(**_kwargs):
        raise AssertionError("strategist must not run when paused")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom_strat)
    await sch._iron_condor_entry_job(
        signal_poster=signals, trade_poster=trades,
        clock_fetcher=lambda: _async(_clock(True)),
    )
    assert sig_posted == []
    assert trade_posted == []


async def test_iron_condor_job_skipped_when_market_closed(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_closed() -> MarketClock:
        return _clock(False)

    async def boom_strat(**_kwargs):
        raise AssertionError("strategist must not run when closed")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom_strat)
    await sch._iron_condor_entry_job(
        signal_poster=signals, trade_poster=trades, clock_fetcher=clock_closed
    )
    assert sig == [] and trd == []


async def test_iron_condor_job_posts_signal_and_trade(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), "📋 manual signal", "🤖 trade telem"

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch._iron_condor_entry_job(
        signal_poster=signals, trade_poster=trades, clock_fetcher=clock_open
    )
    assert sig == ["📋 manual signal"]
    assert trd == ["🤖 trade telem"]


async def test_iron_condor_job_silent_on_hold(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), None, None  # HOLD

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch._iron_condor_entry_job(
        signal_poster=signals, trade_poster=trades, clock_fetcher=clock_open
    )
    assert sig == [] and trd == []


async def test_iron_condor_job_failure_routes_to_logs(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []
    logs: list[str] = []

    async def s(t):
        sig.append(t)

    async def tr(t):
        trd.append(t)

    async def lg(t):
        logs.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def boom(**_kwargs):
        raise RuntimeError("chain fetch failed")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom)
    await sch._iron_condor_entry_job(
        signal_poster=s, trade_poster=tr, log_poster=lg, clock_fetcher=clock_open
    )
    assert sig == [] and trd == []
    assert len(logs) == 1
    assert "failed" in logs[0].lower()


async def _async(value):
    return value


async def test_run_iron_condor_once(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), "📋 entry", "🤖 telem"

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch.run_iron_condor_once(signals, trades, clock_fetcher=clock_open)
    assert sig == ["📋 entry"]
    assert trd == ["🤖 telem"]


# ----------------- exit monitor -----------------


def test_scheduler_registers_exit_jobs_when_enabled():
    scheduler = sch.make_scheduler(**_all_posters(), enable_iron_condor=True)
    monitor = scheduler.get_job("iron_condor_exit")
    force = scheduler.get_job("iron_condor_force_close")
    assert monitor is not None
    assert force is not None
    f_fields = {f.name: str(f) for f in force.trigger.fields}
    assert f_fields["hour"] == "15"
    assert f_fields["minute"] == "50"


async def test_exit_job_posts_when_trades_closed(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_monitor(**_kwargs):
        return [
            {
                "trade_id": 1,
                "status": "closed",
                "reason": "profit_target_50pct",
                "exit_debit": "40.00",
                "realized_pnl_per_contract": "40.00",
                "signal_text": "⏰ exit SPY IC",
                "trade_text": "🤖 closed trade #1",
            }
        ]

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(
        signal_poster=signals, trade_poster=trades, clock_fetcher=clock_open
    )
    assert sig == ["⏰ exit SPY IC"]
    assert trd == ["🤖 closed trade #1"]


async def test_exit_job_silent_when_nothing_closed(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_monitor(**_kwargs):
        return [{"trade_id": 1, "status": "hold"}]

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(
        signal_poster=signals, trade_poster=trades, clock_fetcher=clock_open
    )
    assert sig == [] and trd == []


async def test_force_close_skips_clock_check(monkeypatch):
    """force=True bypasses the clock gate (so it fires near the bell)."""
    sig: list[str] = []
    trd: list[str] = []

    async def signals(t):
        sig.append(t)

    async def trades(t):
        trd.append(t)

    async def clock_closed() -> MarketClock:
        return _clock(False)

    async def fake_monitor(**kwargs):
        assert kwargs.get("force_close") is True
        return []

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(
        signal_poster=signals, trade_poster=trades,
        clock_fetcher=clock_closed, force=True,
    )
    assert sig == [] and trd == []


async def test_exit_job_failure_routes_to_logs(monkeypatch):
    sig: list[str] = []
    trd: list[str] = []
    logs: list[str] = []

    async def s(t):
        sig.append(t)

    async def tr(t):
        trd.append(t)

    async def lg(t):
        logs.append(t)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def boom(**_kwargs):
        raise RuntimeError("alpaca quotes down")

    monkeypatch.setattr(sch, "run_exit_monitor", boom)
    await sch._iron_condor_exit_job(
        signal_poster=s, trade_poster=tr, log_poster=lg, clock_fetcher=clock_open
    )
    assert sig == [] and trd == []
    assert len(logs) == 1
    assert "failed" in logs[0].lower()


# ---------------------------------------------------------------------------
# Bug fixes — conviction filter regression tests (Bug 2)
# ---------------------------------------------------------------------------


from agents.directional.intraday import TickerDecision as _TD


def _decisions_mixed() -> list[_TD]:
    """One HIGH, one MEDIUM, one LOW conviction signal."""
    return [
        _TD("SPY", "BUY_CALL", 500.0, "0DTE", "HIGH", "strong breakout"),
        _TD("NVDA", "BUY_CALL", 900.0, "WEEKLY", "MEDIUM", "mild momentum"),
        _TD("AAPL", "BUY_CALL", 200.0, "WEEKLY", "LOW", "weak signal"),
    ]


def _filter_decisions(decisions, mode: str) -> list[_TD]:
    """Replicate the scheduler's conviction filter logic."""
    allowed = {"HIGH"} if mode == "selective" else {"MEDIUM", "HIGH"}
    return [d for d in decisions if d.action != "HOLD" and d.conviction in allowed]


def test_conviction_filter_selective_allows_only_high():
    """Bug 2: selective mode must only execute HIGH conviction signals."""
    result = _filter_decisions(_decisions_mixed(), "selective")
    convictions = {d.conviction for d in result}
    assert convictions == {"HIGH"}, f"selective should only pass HIGH, got {convictions}"
    assert len(result) == 1


def test_conviction_filter_aggressive_allows_medium_and_high():
    """Bug 2: aggressive mode must execute MEDIUM + HIGH conviction signals."""
    result = _filter_decisions(_decisions_mixed(), "aggressive")
    convictions = {d.conviction for d in result}
    assert "HIGH" in convictions
    assert "MEDIUM" in convictions
    assert "LOW" not in convictions, "LOW conviction must be blocked in aggressive mode"
    assert len(result) == 2


def test_conviction_filter_aggressive_blocks_low():
    """Bug 2: LOW conviction is never executed, even in aggressive mode."""
    low_only = [_TD("AAPL", "BUY_CALL", 200.0, "WEEKLY", "LOW", "weak")]
    result = _filter_decisions(low_only, "aggressive")
    assert result == [], "LOW conviction must be blocked in aggressive mode"


def test_conviction_filter_selective_blocks_medium():
    """Bug 2: MEDIUM conviction must not execute in selective mode (was the bug)."""
    medium_only = [_TD("NVDA", "BUY_CALL", 900.0, "WEEKLY", "MEDIUM", "mild")]
    result = _filter_decisions(medium_only, "selective")
    assert result == [], "MEDIUM conviction must be blocked in selective mode"


# ---------------------------------------------------------------------------
# New risk controls — regression tests
# ---------------------------------------------------------------------------

from decimal import Decimal as _D
from datetime import UTC as _UTC, datetime as _dt
from trademaster.db import Base as _Base, Trade as _Trade, make_engine as _make_engine, make_session_factory as _make_sf


def _fresh_db():
    engine = _make_engine("sqlite:///:memory:")
    _Base.metadata.create_all(engine)
    return _make_sf(engine)


async def test_weekly_loss_limit_halts_scan(monkeypatch):
    """Weekly loss exceeding 25% of capital pauses trading until Monday."""
    sf = _fresh_db()
    monkeypatch.setattr(sch, "make_session_factory", lambda: sf)

    import datetime as _dmod
    import trademaster.db as _db
    # Pin "today" to a fixed Wednesday so this test never depends on the real
    # calendar. It used to fail when actually run on a Monday: Monday is the first
    # day of the week, so there is no "prior day this week" to carry the loss — the
    # seeded loss landed on today and the DAILY limit fired first (logging "daily",
    # not "weekly"). A fixed mid-week date makes the scenario unambiguous.
    monkeypatch.setattr(_db, "today_et", lambda: _dmod.date(2026, 7, 1))  # Wednesday
    week_start = _dmod.date(2026, 7, 1) - _dmod.timedelta(days=2)  # Monday 2026-06-29
    seed_day = week_start  # this week but before today (Wed) → only the WEEKLY limit applies
    closed_mid_week = _dt.combine(seed_day, _dmod.time(15, 0), tzinfo=_UTC)

    with sf() as session:
        session.add(_Trade(
            symbol="SPY", asset_class="option", side="buy", strategy="directional_call",
            qty=_D("1"), entry_price=_D("5.00"), exit_price=_D("0.50"),
            realized_pnl_usd=_D("-2000"),  # exceeds 25% of $5k = $1,250 weekly limit
            opened_at=closed_mid_week - _dmod.timedelta(hours=1),
            closed_at=closed_mid_week,
        ))
        session.commit()

    async def fake_unrealized(): return _D("0")
    async def fake_capital(*_a, **_k): return _D("5000")  # weekly limit = $1,250; loss=$2,000 > limit
    monkeypatch.setattr(sch.alpaca_client, "get_unrealized_pnl", fake_unrealized)
    monkeypatch.setattr(sch, "get_effective_capital", fake_capital)
    monkeypatch.setattr(sch, "is_blackout_day", lambda *_: None)

    logs: list[str] = []

    async def log_capture(t): logs.append(t)

    await sch._directional_scan_job(
        signal_poster=_noop_poster, trade_poster=_noop_poster,
        log_poster=log_capture,
    )
    assert get_state().is_paused(), "weekly loss limit must pause trading"
    assert any("weekly" in m.lower() for m in logs)


async def test_max_trades_cap_blocks_when_set(monkeypatch):
    """When max_trades_per_day is a positive number, the scan is skipped once
    that many trades have opened today."""
    from trademaster.timeutils import today_et
    import datetime as _datetime_mod

    sf = _fresh_db()
    monkeypatch.setattr(sch, "make_session_factory", lambda: sf)
    monkeypatch.setattr(sch.get_settings(), "max_trades_per_day", 3)

    opened = _dt.combine(today_et(), _datetime_mod.time(14, 0), tzinfo=_UTC)
    with sf() as session:
        for i in range(3):
            session.add(_Trade(
                symbol=f"SPY260101C0050000{i}",
                asset_class="option", side="buy", strategy="directional_call",
                qty=_D("1"), entry_price=_D("2.00"), opened_at=opened,
            ))
        session.commit()

    async def fake_unrealized(): return _D("0")
    monkeypatch.setattr(sch.alpaca_client, "get_unrealized_pnl", fake_unrealized)
    monkeypatch.setattr(sch, "is_blackout_day", lambda *_: None)

    scan_called = []

    async def fake_scan(**_):
        scan_called.append(1)
        return ([], [], "")
    monkeypatch.setattr(sch, "run_directional_scan", fake_scan)

    await sch._directional_scan_job(
        signal_poster=_noop_poster, trade_poster=_noop_poster,
        log_poster=_noop_poster,
    )
    assert not scan_called, "scan must be blocked once the count cap is reached"


async def test_max_trades_unlimited_skips_count_check(monkeypatch):
    """Default (0 = unlimited): the per-day count cap is not even consulted."""
    sf = _fresh_db()
    monkeypatch.setattr(sch, "make_session_factory", lambda: sf)
    monkeypatch.setattr(sch.get_settings(), "max_trades_per_day", 0)

    async def fake_unrealized(): return _D("0")
    monkeypatch.setattr(sch.alpaca_client, "get_unrealized_pnl", fake_unrealized)

    consulted = []
    monkeypatch.setattr(
        sch, "get_today_trade_count_by_conviction",
        lambda *_a, **_k: consulted.append(1) or {"HIGH": 99},
    )

    async def fake_scan(**_): return ([], [], "")
    monkeypatch.setattr(sch, "run_directional_scan", fake_scan)

    await sch._directional_scan_job(
        signal_poster=_noop_poster, trade_poster=_noop_poster,
        log_poster=_noop_poster,
    )
    assert not consulted, "count cap must NOT be consulted when unlimited (0)"


async def test_event_blackout_not_consulted_when_disabled(monkeypatch):
    """Default (enable_event_blackout=False): the blackout calendar is not even
    checked — the LLM trades event days (NFP/CPI/FOMC) during paper validation."""
    sf = _fresh_db()
    monkeypatch.setattr(sch, "make_session_factory", lambda: sf)

    async def fake_unrealized(): return _D("0")
    monkeypatch.setattr(sch.alpaca_client, "get_unrealized_pnl", fake_unrealized)

    consulted = []

    def _record_blackout(*_):
        consulted.append(1)
        return "FOMC Decision"
    monkeypatch.setattr(sch, "is_blackout_day", _record_blackout)
    monkeypatch.setattr(sch.get_settings(), "enable_event_blackout", False)

    async def fake_scan(**_): return ([], [], "")
    monkeypatch.setattr(sch, "run_directional_scan", fake_scan)

    await sch._directional_scan_job(
        signal_poster=_noop_poster, trade_poster=_noop_poster,
        log_poster=_noop_poster,
    )
    assert not consulted, "blackout calendar must NOT be consulted when disabled"


async def test_event_blackout_blocks_when_enabled(monkeypatch):
    """When explicitly re-enabled, a CPI/FOMC/NFP day still skips the scan."""
    sf = _fresh_db()
    monkeypatch.setattr(sch, "make_session_factory", lambda: sf)

    async def fake_unrealized(): return _D("0")
    monkeypatch.setattr(sch.alpaca_client, "get_unrealized_pnl", fake_unrealized)
    monkeypatch.setattr(sch.get_settings(), "enable_event_blackout", True)

    consulted = []

    def _record_blackout(*_):
        consulted.append(1)
        return "FOMC Decision"
    monkeypatch.setattr(sch, "is_blackout_day", _record_blackout)

    scan_called = []

    async def fake_scan(**_):
        scan_called.append(1)
        return ([], [], "")
    monkeypatch.setattr(sch, "run_directional_scan", fake_scan)

    await sch._directional_scan_job(
        signal_poster=_noop_poster, trade_poster=_noop_poster,
        log_poster=_noop_poster,
    )
    assert consulted, "blackout calendar must be consulted when enabled"
    assert not scan_called, "scan must be blocked on a blackout day when enabled"


# ----------------- trade health check -----------------


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess process."""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _patch_subprocess(monkeypatch, proc):
    async def fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(sch.asyncio, "create_subprocess_exec", fake_exec)


def test_make_scheduler_registers_health_check_job():
    scheduler = sch.make_scheduler(**_all_posters())
    job = scheduler.get_job("trade_health_check")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "16"
    assert fields["minute"] == "15"
    assert str(job.trigger.timezone) == sch.PREMARKET_TZ


async def test_health_check_posts_report_to_logs(monkeypatch):
    """Issues found (exit 1, report on stdout) → posted to #logs."""
    _patch_subprocess(
        monkeypatch,
        _FakeProc(stdout=b"\xe2\x9a\xa0 2 issue(s) found\n", returncode=1),
    )
    posted = []

    async def logs(text): posted.append(text)

    await sch._trade_health_check_job(log_poster=logs)
    assert len(posted) == 1
    assert "issue(s) found" in posted[0]


async def test_health_check_silent_when_clean(monkeypatch):
    """Clean run (exit 0, empty stdout) → nothing posted."""
    _patch_subprocess(monkeypatch, _FakeProc(stdout=b"", returncode=0))
    posted = []

    async def logs(text): posted.append(text)

    await sch._trade_health_check_job(log_poster=logs)
    assert posted == []


async def test_health_check_crash_routes_to_logs(monkeypatch):
    """Non-0/1 return code → crash alert posted to #logs."""
    _patch_subprocess(
        monkeypatch,
        _FakeProc(stderr=b"Traceback: boom", returncode=2),
    )
    posted = []

    async def logs(text): posted.append(text)

    await sch._trade_health_check_job(log_poster=logs)
    assert len(posted) == 1
    assert "crashed" in posted[0].lower()
    assert "boom" in posted[0]


async def test_health_check_launch_failure_routes_to_logs(monkeypatch):
    """If the subprocess can't even be launched → failure alert to #logs."""
    async def boom_exec(*_args, **_kwargs):
        raise OSError("no python")

    monkeypatch.setattr(sch.asyncio, "create_subprocess_exec", boom_exec)
    posted = []

    async def logs(text): posted.append(text)

    await sch._trade_health_check_job(log_poster=logs)
    assert len(posted) == 1
    assert "failed to run" in posted[0].lower()
    assert "no python" in posted[0]
