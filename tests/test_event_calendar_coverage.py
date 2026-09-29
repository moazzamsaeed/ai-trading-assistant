"""The blackout calendar is hand-maintained and must never expire silently.

Past a given event type's last entry, is_blackout_day() returns None for every
one of its days — the blackout reads as ON while doing nothing. That is the
same silent shape that let the condor trade FOMC on 2026-09-16 for -$6,020.
"""

from __future__ import annotations

from datetime import date

from trademaster.event_calendar import (
    all_blackout_dates,
    coverage_end,
    coverage_end_by_event,
    coverage_expired,
    coverage_gaps,
    is_blackout_day,
)


def test_calendar_dates_are_weekdays_and_nfp_is_always_friday():
    for d, name in all_blackout_dates().items():
        assert d.weekday() < 5, f"{d} ({name}) falls on a weekend"
        if name.startswith("NFP"):
            assert d.weekday() == 4, f"NFP {d} is not a Friday"


def test_coverage_is_tracked_per_event_type():
    """A plain max() over the whole calendar would report healthy coverage off
    whichever type reaches furthest while the others have run out."""
    ends = coverage_end_by_event()
    assert set(ends) == {"FOMC Decision", "CPI Release", "NFP Release"}
    # coverage_end is the EARLIEST per-type end, i.e. when the calendar stops
    # being complete — not the latest date present.
    assert coverage_end() == min(ends.values())
    assert coverage_end() < max(ends.values()), (
        "test assumes the types have different horizons; if they were topped up "
        "to the same date this assertion is simply no longer meaningful"
    )


def test_gaps_are_empty_while_every_type_still_has_dates():
    earliest_end = coverage_end()
    assert coverage_gaps(earliest_end) == []
    assert not coverage_expired(earliest_end)


def test_gap_is_reported_the_day_after_a_type_runs_out():
    ends = coverage_end_by_event()
    soonest = min(ends, key=lambda k: ends[k])
    day_after = date.fromordinal(ends[soonest].toordinal() + 1)
    gaps = coverage_gaps(day_after)
    assert gaps, "a lapsed event type must be reported"
    assert soonest in [n for n, _ in gaps]
    assert coverage_expired(day_after)


def test_far_future_reports_every_type_as_lapsed():
    gaps = coverage_gaps(date(2099, 1, 1))
    assert {n for n, _ in gaps} == {"FOMC Decision", "CPI Release", "NFP Release"}


def test_known_2026_blackouts_resolve():
    assert is_blackout_day(date(2026, 10, 2)) == "NFP Release"
    assert is_blackout_day(date(2026, 10, 14)) == "CPI Release"
    assert is_blackout_day(date(2026, 12, 16)) == "FOMC Decision"
    assert is_blackout_day(date(2026, 10, 1)) is None


def test_2027_fomc_is_covered():
    """Go-live is January 2027 — the FOMC days that month must be known."""
    assert is_blackout_day(date(2027, 1, 27)) == "FOMC Decision"
    assert is_blackout_day(date(2027, 12, 8)) == "FOMC Decision"
