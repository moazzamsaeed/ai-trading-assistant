"""Economic event blackout calendar.

On high-impact macro days (FOMC, CPI, NFP, major Fed speakers) intraday
options positions are exceptionally dangerous:
- 0DTE options can spike 5-10x on the announcement then collapse
- Spreads widen to 30-50% of mid immediately before the print
- IV crush after the event destroys premium even on correct direction

The blackout blocks new ENTRIES only — open positions are still monitored
and exited normally by the exit monitor.

Dates are hardcoded for 2026 and updated annually. Times are ET.
`is_blackout_day()` returns the event name if today is blacked out, else None.
"""

from __future__ import annotations

from datetime import date, timedelta

# 2026 economic event blackout dates (ET calendar day).
# Sources: Fed calendar, BLS release schedule, CME FedWatch.
_BLACKOUT_DATES: dict[date, str] = {
    # FOMC meeting days (decision day — most volatile)
    date(2026, 1, 28): "FOMC Decision",
    date(2026, 3, 18): "FOMC Decision",
    date(2026, 5, 6):  "FOMC Decision",
    date(2026, 6, 17): "FOMC Decision",
    date(2026, 7, 29): "FOMC Decision",
    date(2026, 9, 16): "FOMC Decision",
    date(2026, 11, 4): "FOMC Decision",
    date(2026, 12, 16): "FOMC Decision",

    # CPI release days (BLS, usually 8:30 AM ET — market opens with massive gap)
    date(2026, 1, 14): "CPI Release",
    date(2026, 2, 11): "CPI Release",
    date(2026, 3, 11): "CPI Release",
    date(2026, 4, 10): "CPI Release",
    date(2026, 5, 13): "CPI Release",
    date(2026, 6, 11): "CPI Release",
    date(2026, 7, 15): "CPI Release",
    date(2026, 8, 12): "CPI Release",
    date(2026, 9, 10): "CPI Release",
    date(2026, 10, 14): "CPI Release",
    date(2026, 11, 12): "CPI Release",
    date(2026, 12, 10): "CPI Release",

    # NFP (Non-Farm Payrolls) — first Friday of each month, 8:30 AM ET
    date(2026, 1, 9):  "NFP Release",
    date(2026, 2, 6):  "NFP Release",
    date(2026, 3, 6):  "NFP Release",
    date(2026, 4, 3):  "NFP Release",
    date(2026, 5, 1):  "NFP Release",
    date(2026, 6, 5):  "NFP Release",
    date(2026, 7, 10): "NFP Release",
    date(2026, 8, 7):  "NFP Release",
    date(2026, 9, 4):  "NFP Release",
    date(2026, 10, 2): "NFP Release",
    date(2026, 11, 6): "NFP Release",
    date(2026, 12, 4): "NFP Release",

    # --- 2027 ---
    # FOMC decision days (the SECOND day of each two-day meeting, 2 PM ET
    # statement). Source: Fed press release 2025-09-05, "FOMC announces its
    # tentative meeting schedule for 2027". Tentative — re-verify during 2027.
    date(2027, 1, 27): "FOMC Decision",
    date(2027, 3, 17): "FOMC Decision",
    date(2027, 4, 28): "FOMC Decision",
    date(2027, 6, 9): "FOMC Decision",
    date(2027, 7, 28): "FOMC Decision",
    date(2027, 9, 15): "FOMC Decision",
    date(2027, 10, 27): "FOMC Decision",
    date(2027, 12, 8): "FOMC Decision",
    # ⚠️ 2027 CPI and NFP dates are deliberately NOT here: as of 2026-09-29 the
    # BLS has only published through Dec 2026. They are NOT derivable — the
    # "first Friday" rule fails for NFP in practice (2026 has Jan 9 and Jul 10,
    # both SECOND Fridays). Fill them from bls.gov/schedule/news_release once
    # BLS publishes 2027. coverage_gaps() flags CPI/NFP as lapsed after
    # 2026-12-10 / 2026-12-04 so this cannot go unnoticed.
}


def is_blackout_day(today: date | None = None) -> str | None:
    """Return the event name if today is a blackout day, else None."""
    if today is None:
        from trademaster.timeutils import today_et
        today = today_et()
    return _BLACKOUT_DATES.get(today)


def all_blackout_dates() -> dict[date, str]:
    """Return a copy of the full blackout calendar."""
    return dict(_BLACKOUT_DATES)


def coverage_end_by_event() -> dict[str, date]:
    """Last known date for EACH event type (FOMC / CPI / NFP separately).

    Per-type on purpose. The types are published by different bodies on
    different horizons — the Fed announces FOMC more than a year ahead while
    BLS publishes CPI/NFP roughly a year out — so a plain max() over the whole
    calendar reports healthy coverage off whichever type reaches furthest while
    the others have quietly run out.
    """
    out: dict[str, date] = {}
    for d, name in _BLACKOUT_DATES.items():
        if name not in out or d > out[name]:
            out[name] = d
    return out


def coverage_end() -> date:
    """The date the calendar stops being COMPLETE — the earliest per-type end."""
    return min(coverage_end_by_event().values())


def coverage_gaps(today: date | None = None) -> list[tuple[str, date]]:
    """Event types whose calendar has run out, as (name, last known date).

    The calendar is hand-maintained. Past a type's last entry `is_blackout_day`
    quietly returns None for every one of its days, so the blackout reads as ON
    while doing nothing — the same silent-failure shape that let the condor
    trade FOMC on 2026-09-16 for -$6,020. Callers must surface this rather than
    trust the enabled flag.
    """
    if today is None:
        from trademaster.timeutils import today_et
        today = today_et()
    return sorted((n, d) for n, d in coverage_end_by_event().items() if today > d)


def coverage_expired(today: date | None = None) -> bool:
    """True once ANY event type's calendar has run out."""
    return bool(coverage_gaps(today))


def upcoming_events(today: date | None = None, days: int = 10) -> list[tuple[date, str]]:
    """Macro events in the next `days` calendar days (inclusive of today),
    sorted by date. Feeds the premarket briefing so it can flag catalysts ahead
    (e.g. "CPI Wednesday, FOMC next week")."""
    if today is None:
        from trademaster.timeutils import today_et
        today = today_et()
    horizon = today + timedelta(days=days)
    return sorted(
        (d, name) for d, name in _BLACKOUT_DATES.items() if today <= d <= horizon
    )
