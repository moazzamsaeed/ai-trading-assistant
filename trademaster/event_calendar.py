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

from datetime import date

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
