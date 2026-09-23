"""Watchdog alert logic — silent when healthy, loud when not.

Guards the 2026-09-23 failure mode: the daemon crash-looped from 06:45 and
nothing announced it, so the missed 10:00 ET condor entry looked like a
quiet day.
"""

from __future__ import annotations

from scripts.daemon_watchdog import build_alert


def _props(state: str, sub: str) -> dict[str, str]:
    return {"ActiveState": state, "SubState": sub}


def test_healthy_daemon_is_silent():
    assert build_alert(_props("active", "running"), restarts=0) is None


def test_a_couple_of_restarts_is_still_silent():
    """systemd restarts on transient network blips; don't cry wolf."""
    assert build_alert(_props("active", "running"), restarts=2) is None


def test_flapping_daemon_alerts_even_while_active():
    """Active *right now* but repeatedly dying — it may be down again by 10:00."""
    msg = build_alert(_props("active", "running"), restarts=7)
    assert msg is not None
    assert "UNSTABLE" in msg
    assert "7×" in msg


def test_crash_loop_is_reported_as_such():
    msg = build_alert(_props("activating", "auto-restart"), restarts=447)
    assert msg is not None
    assert "CRASH-LOOPING" in msg
    assert "447" in msg


def test_failed_unit_alerts():
    msg = build_alert(_props("failed", "failed"))
    assert msg is not None
    assert "FAILED" in msg


def test_inactive_unit_alerts():
    msg = build_alert(_props("inactive", "dead"))
    assert msg is not None
    assert "NOT RUNNING" in msg
    assert "inactive/dead" in msg


def test_unqueryable_systemd_alerts_rather_than_assuming_health():
    """No data must never be read as 'fine'."""
    msg = build_alert({})
    assert msg is not None
    assert "UNKNOWN" in msg


def test_alert_names_the_stake_and_pings():
    msg = build_alert(_props("inactive", "dead"))
    assert msg.startswith("@here ")
    assert "10:00 ET" in msg
    assert "systemctl --user restart trademaster.service" in msg


def test_alert_includes_the_error_when_available():
    msg = build_alert(
        _props("activating", "auto-restart"),
        restarts=447,
        error="RuntimeError: Missing required environment variables: ANTHROPIC_API_KEY",
    )
    assert "ANTHROPIC_API_KEY" in msg
    assert "```" in msg
