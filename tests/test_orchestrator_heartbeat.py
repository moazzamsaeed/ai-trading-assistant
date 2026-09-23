"""Startup heartbeat — the #logs message that proves which stack booted.

Added after 2026-09-23, when a cold-start crash-loop ran 447 times through
the morning and was indistinguishable from a quiet no-trade day.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from trademaster.orchestrator import build_startup_heartbeat
from trademaster.timeutils import ET


def _settings(**over):
    base = dict(
        trading_mode="paper",
        account_type="cash",
        trading_capital_usd=25000,
        condor_contracts=28,
        condor_distance_aware_stop=True,
        condor_assignment_close=True,
        condor_stop_single_leg=True,
        enable_event_blackout=True,
        alpaca_options_feed="opra",
        directional_signals_only=True,
        enable_directional=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Scheduler:
    def __init__(self, next_run_time):
        self._job = SimpleNamespace(next_run_time=next_run_time)

    def get_job(self, job_id):
        return self._job if job_id == "iron_condor_entry" else None


NOW = datetime(2026, 9, 23, 11, 52, tzinfo=ET)


def test_heartbeat_reports_the_deployed_config():
    msg = build_startup_heartbeat(_settings(), now=NOW)
    assert "PAPER" in msg
    assert "2026-09-23 11:52 ET" in msg
    assert "$25,000" in msg
    assert "28ct" in msg
    assert "options=opra" in msg
    assert "signals-only" in msg
    # every condor flag on -> no failure marks anywhere
    assert "❌" not in msg


def test_heartbeat_marks_flags_that_are_off():
    msg = build_startup_heartbeat(
        _settings(condor_assignment_close=False, enable_event_blackout=False),
        now=NOW,
    )
    assert "assignment close ❌" in msg
    assert "event blackout ❌" in msg
    assert "dist-aware stop ✅" in msg


def test_heartbeat_flags_live_mode_distinctly():
    paper = build_startup_heartbeat(_settings(), now=NOW)
    live = build_startup_heartbeat(_settings(trading_mode="live"), now=NOW)
    assert "🟢" in paper and "LIVE" not in paper
    assert "🔴" in live and "LIVE" in live


def test_heartbeat_reports_next_condor_entry():
    nxt = datetime(2026, 9, 24, 10, 0, tzinfo=ET)
    msg = build_startup_heartbeat(_settings(), scheduler=_Scheduler(nxt), now=NOW)
    assert "Thu 2026-09-24 10:00 ET" in msg


def test_heartbeat_warns_when_condor_entry_is_not_scheduled():
    """Condor disabled/unscheduled must be loud — that is a silent no-trade day."""
    msg = build_startup_heartbeat(_settings(), scheduler=_Scheduler(None), now=NOW)
    assert "⚠️ not scheduled" in msg


def test_heartbeat_reports_directional_states():
    off = build_startup_heartbeat(
        _settings(directional_signals_only=False, enable_directional=False), now=NOW
    )
    trading = build_startup_heartbeat(
        _settings(directional_signals_only=False, enable_directional=True), now=NOW
    )
    assert "**Directional** off" in off
    assert "**Directional** trading" in trading
