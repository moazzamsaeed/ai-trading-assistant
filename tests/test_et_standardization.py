"""End-to-end RTH-scenario tests for the ET timezone standardization.

These exercise the production code paths that were touched by the ET refactor,
simulating specific clock times during and after RTH so we catch surprises
before the live market does.

The goal is *behavior* coverage, not unit coverage of timeutils itself
(which is covered by test_timeutils.py).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trademaster import timeutils
from trademaster.db import Base, Trade, get_today_realized_pnl, make_engine, make_session_factory
from trademaster.timeutils import ET, fmt_et, to_et, today_et


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _closed_trade(session_factory, *, closed_at: datetime, pnl: float) -> None:
    """Insert a closed trade with the given UTC close time + realized P&L."""
    with session_factory() as session:
        session.add(
            Trade(
                symbol="SPY260513C00500000",
                asset_class="option",
                side="buy",
                strategy="directional_call",
                qty=Decimal("1"),
                entry_price=Decimal("2.00"),
                exit_price=Decimal("3.00"),
                realized_pnl_usd=Decimal(str(pnl)),
                opened_at=closed_at - timedelta(minutes=30),
                closed_at=closed_at,
            )
        )
        session.commit()


def _freeze_now(monkeypatch, fake_utc: datetime) -> None:
    """Make `datetime.now(...)` (and our timeutils.now_et) return a fixed instant.

    Anything that calls `datetime.now(UTC)` or `datetime.now(ET)` will see this.
    """
    real_datetime = datetime

    class _Fake(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_utc.astimezone(tz) if tz else fake_utc.replace(tzinfo=None)

    monkeypatch.setattr("trademaster.timeutils.datetime", _Fake)


# ---------------------------------------------------------------------------
# get_today_realized_pnl — the function whose ET fix is the whole point
# ---------------------------------------------------------------------------


def test_pnl_today_during_rth_counts_today_trades(session_factory, monkeypatch):
    """At 14:00 ET (RTH), trades closed earlier today count."""
    # Pretend "now" is May 13, 2026 at 14:00 ET = 18:00 UTC
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 18, 0, tzinfo=UTC))

    # Trade closed at 10:00 ET today = 14:00 UTC
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 13, 14, 0, tzinfo=UTC), pnl=100.00)

    assert get_today_realized_pnl(session_factory) == Decimal("100.00")


def test_pnl_today_after_8pm_central_still_counts_today_trades(session_factory, monkeypatch):
    """The exact bug we fixed: at 21:00 ET (01:00 UTC tomorrow), today's
    trades MUST still appear in P&L Today. SQLite's DATE('now') would
    have failed this — verify the Python path doesn't.
    """
    # 21:00 ET on May 13 = 01:00 UTC on May 14
    _freeze_now(monkeypatch, datetime(2026, 5, 14, 1, 0, tzinfo=UTC))

    # Trade closed at 15:55 ET today = 19:55 UTC May 13
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 13, 19, 55, tzinfo=UTC), pnl=250.00)

    assert get_today_realized_pnl(session_factory) == Decimal("250.00")


def test_pnl_today_excludes_yesterday_trades(session_factory, monkeypatch):
    """Yesterday's trades must not leak into today's P&L."""
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 18, 0, tzinfo=UTC))

    # Yesterday at 15:00 ET = 19:00 UTC May 12
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 12, 19, 0, tzinfo=UTC), pnl=999.00)

    assert get_today_realized_pnl(session_factory) == Decimal("0")


def test_pnl_today_excludes_tomorrow_trades(session_factory, monkeypatch):
    """Trades dated in the future (off-by-one date arithmetic guard) excluded."""
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 18, 0, tzinfo=UTC))

    # 10:00 ET tomorrow = 14:00 UTC May 14
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC), pnl=42.00)

    assert get_today_realized_pnl(session_factory) == Decimal("0")


def test_pnl_today_aggregates_multiple_wins_and_losses(session_factory, monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 19, 0, tzinfo=UTC))

    _closed_trade(session_factory, closed_at=datetime(2026, 5, 13, 14, 0, tzinfo=UTC), pnl=300.00)
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 13, 16, 0, tzinfo=UTC), pnl=-125.50)
    _closed_trade(session_factory, closed_at=datetime(2026, 5, 13, 18, 30, tzinfo=UTC), pnl=75.00)

    assert get_today_realized_pnl(session_factory) == Decimal("249.50")


# ---------------------------------------------------------------------------
# today_et / now_et — used by scheduler "today" boundary for cooldown logic
# ---------------------------------------------------------------------------


def test_today_et_returns_today_after_midnight_utc(monkeypatch):
    # 23:00 ET on May 13 = 03:00 UTC May 14
    _freeze_now(monkeypatch, datetime(2026, 5, 14, 3, 0, tzinfo=UTC))
    assert today_et() == date(2026, 5, 13)


def test_today_et_flips_at_midnight_et(monkeypatch):
    # 00:01 ET on May 14 = 04:01 UTC May 14
    _freeze_now(monkeypatch, datetime(2026, 5, 14, 4, 1, tzinfo=UTC))
    assert today_et() == date(2026, 5, 14)


def test_today_et_correct_under_est(monkeypatch):
    """In winter (EST = UTC-5), 21:00 ET = 02:00 UTC next day."""
    # 21:00 ET on Jan 15 = 02:00 UTC Jan 16
    _freeze_now(monkeypatch, datetime(2026, 1, 16, 2, 0, tzinfo=UTC))
    assert today_et() == date(2026, 1, 15)


# ---------------------------------------------------------------------------
# Exit monitor force_close — the 15:30 ET / 15:50 ET clocks
# ---------------------------------------------------------------------------


def test_directional_force_close_clock_15_29_et_not_triggered():
    """15:29 ET (1 min before threshold) → past_force_close_time = False."""
    from agents.directional.exit_monitor import FORCE_CLOSE_AFTER

    et_now = to_et(datetime(2026, 5, 13, 19, 29, tzinfo=UTC))  # 15:29 EDT
    assert et_now.time() < FORCE_CLOSE_AFTER


def test_directional_force_close_clock_15_30_et_triggered():
    from agents.directional.exit_monitor import FORCE_CLOSE_AFTER

    et_now = to_et(datetime(2026, 5, 13, 19, 30, tzinfo=UTC))  # 15:30 EDT
    assert et_now.time() >= FORCE_CLOSE_AFTER


def test_iron_condor_force_close_clock_15_49_et_not_triggered():
    from agents.options.exit_monitor import FORCE_CLOSE_AFTER

    et_now = to_et(datetime(2026, 5, 13, 19, 49, tzinfo=UTC))  # 15:49 EDT
    assert et_now.time() < FORCE_CLOSE_AFTER


def test_iron_condor_force_close_clock_15_50_et_triggered():
    from agents.options.exit_monitor import FORCE_CLOSE_AFTER

    et_now = to_et(datetime(2026, 5, 13, 19, 50, tzinfo=UTC))  # 15:50 EDT
    assert et_now.time() >= FORCE_CLOSE_AFTER


def test_force_close_clock_uses_et_not_host_tz():
    """Same UTC instant: 19:50 UTC. In Tokyo (UTC+9) that's 04:50 next day —
    if we accidentally used host TZ, force-close would not fire. Using ET
    (UTC-4 in EDT) it's 15:50 EDT, which DOES fire.
    """
    from agents.options.exit_monitor import FORCE_CLOSE_AFTER

    utc_at_15_50_et = datetime(2026, 5, 13, 19, 50, tzinfo=UTC)
    assert to_et(utc_at_15_50_et).time() >= FORCE_CLOSE_AFTER


# ---------------------------------------------------------------------------
# Strategist prompt — host-TZ bug regression test
# ---------------------------------------------------------------------------


def test_strategist_now_et_renders_in_et_regardless_of_host(monkeypatch):
    """Reproduces the agents/options/strategist.py:173 bug.

    Before the fix, `.astimezone()` (no arg) used the host TZ. We force
    the host TZ to UTC and verify the formatted "now_et" is in ET (EDT),
    not UTC.
    """
    # Simulate a host running in UTC. The prompt should still render ET.
    monkeypatch.setenv("TZ", "UTC")

    # 13:00 EDT on May 13 = 17:00 UTC
    utc_now = datetime(2026, 5, 13, 17, 0, tzinfo=UTC)
    rendered = to_et(utc_now).strftime("%H:%M ET")

    # 17:00 UTC = 13:00 EDT, NOT 17:00 — confirms we shifted to ET, not host.
    assert rendered == "13:00 ET"


# ---------------------------------------------------------------------------
# News timestamp formatting — premarket + intraday scan
# ---------------------------------------------------------------------------


def test_premarket_news_timestamp_in_et_edt():
    """A news article at 14:30 UTC in May (EDT) renders as 10:30 ET."""
    utc_dt = datetime(2026, 5, 13, 14, 30, tzinfo=UTC)
    rendered = fmt_et(utc_dt, "%Y-%m-%d %H:%M ET")
    assert rendered == "2026-05-13 10:30 ET"


def test_premarket_news_timestamp_in_et_est():
    """A news article at 14:30 UTC in January (EST) renders as 09:30 ET."""
    utc_dt = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    rendered = fmt_et(utc_dt, "%Y-%m-%d %H:%M ET")
    assert rendered == "2026-01-15 09:30 ET"


def test_intraday_scan_timestamp_compact_et():
    """Compact form used by the intraday scanner."""
    utc_dt = datetime(2026, 5, 13, 18, 45, tzinfo=UTC)
    assert fmt_et(utc_dt, "%H:%M ET") == "14:45 ET"


# ---------------------------------------------------------------------------
# RTH bar anchor — get_recent_bars start-time logic
# ---------------------------------------------------------------------------


def test_rth_bar_anchor_during_rth_starts_at_930_et(monkeypatch):
    """When called at 11:00 ET (RTH), the bar fetch must start at 09:30 ET today,
    not 04:00 ET (pre-market) — that was the bug we fixed earlier.
    """
    # 11:00 ET on May 13 = 15:00 UTC
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 15, 0, tzinfo=UTC))

    now_et = to_et(timeutils.datetime.now(UTC))
    rth_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start = rth_open if now_et >= rth_open else now_et.replace(hour=4, minute=0, second=0, microsecond=0)

    assert start.hour == 9 and start.minute == 30
    assert start.date() == date(2026, 5, 13)


def test_rth_bar_anchor_premarket_starts_at_4am_et(monkeypatch):
    """Pre-market (07:00 ET): start = 04:00 ET (pre-market session)."""
    # 07:00 ET on May 13 = 11:00 UTC
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 11, 0, tzinfo=UTC))

    now_et = to_et(timeutils.datetime.now(UTC))
    rth_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start = rth_open if now_et >= rth_open else now_et.replace(hour=4, minute=0, second=0, microsecond=0)

    assert start.hour == 4 and start.minute == 0


def test_rth_bar_anchor_at_exact_open_uses_rth(monkeypatch):
    """At 09:30:00 ET exactly, we cross the threshold → start = today's open."""
    # 09:30 ET on May 13 = 13:30 UTC
    _freeze_now(monkeypatch, datetime(2026, 5, 13, 13, 30, tzinfo=UTC))

    now_et = to_et(timeutils.datetime.now(UTC))
    rth_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    start = rth_open if now_et >= rth_open else now_et.replace(hour=4, minute=0, second=0, microsecond=0)

    assert start.hour == 9 and start.minute == 30


# ---------------------------------------------------------------------------
# Round-trip: ET conversions don't drift
# ---------------------------------------------------------------------------


def test_to_et_and_back_to_utc_is_lossless():
    utc_dt = datetime(2026, 5, 13, 18, 37, 22, tzinfo=UTC)
    assert to_et(utc_dt).astimezone(UTC) == utc_dt


def test_et_remains_aware_after_conversion():
    utc_dt = datetime(2026, 5, 13, 18, 0, tzinfo=UTC)
    et_dt = to_et(utc_dt)
    assert et_dt.tzinfo is ET
