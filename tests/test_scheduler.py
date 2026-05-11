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


# ----------------- premarket -----------------


def test_make_scheduler_registers_premarket_job():
    scheduler = sch.make_scheduler(_noop_poster)
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


async def test_premarket_job_swallows_exception(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def boom(**_kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(sch, "run_premarket_briefing", boom)
    await sch._premarket_job(poster)
    assert len(posted) == 1
    assert "failed" in posted[0].lower()
    assert "router down" in posted[0]


# ----------------- intraday -----------------


def test_make_scheduler_registers_intraday_job():
    scheduler = sch.make_scheduler(_noop_poster, _noop_poster)
    job = scheduler.get_job("intraday_scan")
    assert job is not None

    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "9-15"
    assert "0" in fields["minute"] and "30" in fields["minute"]
    assert str(job.trigger.timezone) == sch.PREMARKET_TZ


async def test_intraday_skipped_when_paused(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)

    # Should NOT call scan or fetch clock — both would raise if called.
    async def boom_clock() -> MarketClock:
        raise AssertionError("clock should not be fetched when paused")

    async def boom_scan(**_kwargs):
        raise AssertionError("scan should not run when paused")

    monkeypatch.setattr(sch, "run_intraday_scan", boom_scan)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=boom_clock)
    assert posted == []


async def test_intraday_skipped_when_market_closed(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_closed() -> MarketClock:
        return _clock(is_open=False)

    async def boom(**_kwargs):
        raise AssertionError("scan should not run when market closed")

    monkeypatch.setattr(sch, "run_intraday_scan", boom)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=clock_closed)
    assert posted == []


async def test_intraday_posts_alert_when_actionable(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), "SPY breakout alert"

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=clock_open)
    assert posted == ["SPY breakout alert"]


async def test_intraday_silent_on_hold(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), None  # HOLD

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=clock_open)
    assert posted == []


async def test_intraday_job_swallows_scan_exception(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def boom(**_kwargs):
        raise RuntimeError("deepseek down")

    monkeypatch.setattr(sch, "run_intraday_scan", boom)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=clock_open)
    assert len(posted) == 1
    assert "failed" in posted[0].lower()


async def test_intraday_clock_failure_silent(monkeypatch):
    """Transient clock-fetch failures don't spam #alerts."""
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_boom() -> MarketClock:
        raise RuntimeError("alpaca clock 503")

    async def boom_scan(**_kwargs):
        raise AssertionError("scan should not run if clock fetch failed")

    monkeypatch.setattr(sch, "run_intraday_scan", boom_scan)
    await sch._intraday_scan_job(alert_poster=poster, clock_fetcher=clock_boom)
    assert posted == []


async def test_run_intraday_once(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(is_open=True)

    async def fake_scan(**_kwargs):
        return object(), "alert"

    monkeypatch.setattr(sch, "run_intraday_scan", fake_scan)
    await sch.run_intraday_once(poster, clock_fetcher=clock_open)
    assert posted == ["alert"]


# ----------------- iron condor entry -----------------


def test_make_scheduler_registers_iron_condor_job():
    scheduler = sch.make_scheduler(_noop_poster, _noop_poster)
    job = scheduler.get_job("iron_condor_entry")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["day_of_week"] == "mon-fri"
    assert fields["hour"] == "9"
    assert fields["minute"] == "45"


async def test_iron_condor_job_skipped_when_paused(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    get_state().paused_until = datetime.now(UTC) + timedelta(minutes=30)

    async def boom_strat(**_kwargs):
        raise AssertionError("strategist must not run when paused")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom_strat)
    await sch._iron_condor_entry_job(
        alert_poster=poster, clock_fetcher=lambda: _async(_clock(True))
    )
    assert posted == []


async def test_iron_condor_job_skipped_when_market_closed(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_closed() -> MarketClock:
        return _clock(False)

    async def boom_strat(**_kwargs):
        raise AssertionError("strategist must not run when closed")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom_strat)
    await sch._iron_condor_entry_job(alert_poster=poster, clock_fetcher=clock_closed)
    assert posted == []


async def test_iron_condor_job_posts_alert_when_strategist_approves(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), "📋 candidate"

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch._iron_condor_entry_job(alert_poster=poster, clock_fetcher=clock_open)
    assert posted == ["📋 candidate"]


async def test_iron_condor_job_silent_on_hold(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), None  # HOLD

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch._iron_condor_entry_job(alert_poster=poster, clock_fetcher=clock_open)
    assert posted == []


async def test_iron_condor_job_swallows_exception(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def boom(**_kwargs):
        raise RuntimeError("chain fetch failed")

    monkeypatch.setattr(sch, "run_iron_condor_strategist", boom)
    await sch._iron_condor_entry_job(alert_poster=poster, clock_fetcher=clock_open)
    assert len(posted) == 1
    assert "failed" in posted[0].lower()


async def _async(value):
    return value


async def test_run_iron_condor_once(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_strat(**_kwargs):
        return object(), "alert"

    monkeypatch.setattr(sch, "run_iron_condor_strategist", fake_strat)
    await sch.run_iron_condor_once(poster, clock_fetcher=clock_open)
    assert posted == ["alert"]


# ----------------- exit monitor -----------------


def test_scheduler_registers_exit_jobs():
    scheduler = sch.make_scheduler(_noop_poster, _noop_poster)
    monitor = scheduler.get_job("iron_condor_exit")
    force = scheduler.get_job("iron_condor_force_close")
    assert monitor is not None
    assert force is not None
    f_fields = {f.name: str(f) for f in force.trigger.fields}
    assert f_fields["hour"] == "15"
    assert f_fields["minute"] == "50"


async def test_exit_job_posts_when_trades_closed(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

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
            }
        ]

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(alert_poster=poster, clock_fetcher=clock_open)
    assert len(posted) == 1
    assert "trade #1" in posted[0]
    assert "profit_target_50pct" in posted[0]


async def test_exit_job_silent_when_nothing_closed(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def fake_monitor(**_kwargs):
        return [{"trade_id": 1, "status": "hold"}]

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(alert_poster=poster, clock_fetcher=clock_open)
    assert posted == []


async def test_force_close_skips_clock_check(monkeypatch):
    """force=True must work even when market is closed (e.g., after-hours sweep)."""
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_closed() -> MarketClock:
        return _clock(False)

    async def fake_monitor(**kwargs):
        # force_close should be passed True.
        assert kwargs.get("force_close") is True
        return []

    monkeypatch.setattr(sch, "run_exit_monitor", fake_monitor)
    await sch._iron_condor_exit_job(
        alert_poster=poster, clock_fetcher=clock_closed, force=True
    )
    # no closures → no message
    assert posted == []


async def test_exit_job_swallows_exception(monkeypatch):
    posted: list[str] = []

    async def poster(text: str) -> None:
        posted.append(text)

    async def clock_open() -> MarketClock:
        return _clock(True)

    async def boom(**_kwargs):
        raise RuntimeError("alpaca quotes down")

    monkeypatch.setattr(sch, "run_exit_monitor", boom)
    await sch._iron_condor_exit_job(alert_poster=poster, clock_fetcher=clock_open)
    assert len(posted) == 1
    assert "failed" in posted[0].lower()
