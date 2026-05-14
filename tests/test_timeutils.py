"""Tests for trademaster.timeutils — ET timezone helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from trademaster.timeutils import ET, fmt_et, now_et, to_et, today_et


def test_et_zone_is_new_york():
    assert str(ET) == "America/New_York"


def test_now_et_is_aware_and_in_et():
    n = now_et()
    assert n.tzinfo is ET


def test_today_et_returns_date():
    assert isinstance(today_et(), date)


def test_to_et_converts_utc_to_et():
    # 2026-05-13 20:00 UTC = 16:00 EDT
    utc_dt = datetime(2026, 5, 13, 20, 0, tzinfo=UTC)
    et_dt = to_et(utc_dt)
    assert et_dt.hour == 16
    assert et_dt.minute == 0
    assert et_dt.tzinfo is ET


def test_to_et_handles_standard_time():
    # 2026-01-15 20:00 UTC = 15:00 EST (EST is UTC-5)
    utc_dt = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    et_dt = to_et(utc_dt)
    assert et_dt.hour == 15


def test_today_et_at_late_utc_evening_returns_correct_et_date(monkeypatch):
    """Right after midnight UTC on 2026-05-14 (8 PM EDT on May 13),
    today_et must still return May 13.

    Concretely: simulate `datetime.now(ET)` returning 2026-05-13 20:00 EDT.
    """
    fake_now = datetime(2026, 5, 13, 20, 0, tzinfo=ET)

    class _Fake:
        @staticmethod
        def now(tz=None):
            return fake_now.astimezone(tz) if tz else fake_now.replace(tzinfo=None)

    monkeypatch.setattr("trademaster.timeutils.datetime", _Fake)
    assert today_et() == date(2026, 5, 13)


def test_today_et_across_dst_spring_forward():
    """DST starts 2026-03-08 02:00 EST → 03:00 EDT.
    At 06:00 UTC on 2026-03-08, ET is 01:00 EST (pre-DST) → ET date = 2026-03-08.
    """
    utc_dt = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    assert to_et(utc_dt).date() == date(2026, 3, 8)


def test_today_et_across_dst_fall_back():
    """DST ends 2026-11-01 02:00 EDT → 01:00 EST.
    At 06:00 UTC on 2026-11-01, ET is 02:00 EST (post-DST) → ET date = 2026-11-01.
    """
    utc_dt = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
    assert to_et(utc_dt).date() == date(2026, 11, 1)


def test_fmt_et_default_format():
    # 2026-05-13 19:45 UTC = 15:45 EDT = "May 13 3:45 PM ET"
    utc_dt = datetime(2026, 5, 13, 19, 45, tzinfo=UTC)
    s = fmt_et(utc_dt)
    assert "May 13" in s
    assert "3:45 PM" in s
    assert "ET" in s


def test_fmt_et_custom_format():
    utc_dt = datetime(2026, 5, 13, 19, 45, tzinfo=UTC)
    s = fmt_et(utc_dt, "%H:%M")
    assert s == "15:45"


def test_to_et_accepts_other_tz_aware_inputs():
    # Pacific time input → ET output
    pst = ZoneInfo("America/Los_Angeles")
    pst_dt = datetime(2026, 5, 13, 12, 0, tzinfo=pst)  # noon PT = 3 PM ET
    et_dt = to_et(pst_dt)
    assert et_dt.hour == 15
