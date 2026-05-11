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
    assert fields["hour"] == "9"
    assert fields["minute"] == "45"


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
