"""Multi-ticker directional support (QQQ alongside SPY).

The deterministic engine's S/R headroom gate reads levels from market_ctx. When
the scan runs a non-SPY ticker it must use that ticker's OWN levels, not SPY's —
`_ticker_sr_ctx` builds them (same logic as the equities scanner's per-ticker ctx).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.directional.intraday import _ticker_sr_ctx
from integrations.alpaca_client import Bar

NOW = datetime(2026, 6, 26, 14, tzinfo=UTC)


def _bar(ts, o, h, l, c, v=1000):
    return Bar(timestamp=ts, open=Decimal(str(o)), high=Decimal(str(h)),
               low=Decimal(str(l)), close=Decimal(str(c)), volume=v, vwap=Decimal(str(c)))


def test_ticker_sr_ctx_builds_own_levels():
    # 10 completed daily sessions (closes 100..109), all before today (2026-06-26)
    daily = [
        _bar(datetime(2026, 6, 12, 20, tzinfo=UTC) + timedelta(days=i),
             100 + i, 100 + i + 2, 100 + i - 2, 100 + i)
        for i in range(10)
    ]
    intraday = [
        _bar(datetime(2026, 6, 26, 13, 35, tzinfo=UTC), 110, 112, 109, 111),  # ORB bar
        _bar(datetime(2026, 6, 26, 13, 40, tzinfo=UTC), 111, 115, 110, 114),
        _bar(datetime(2026, 6, 26, 13, 45, tzinfo=UTC), 114, 114, 108, 109),
    ]
    ctx = _ticker_sr_ctx(intraday, daily, NOW)
    md = ctx["multi_day"]
    assert md["prev_close"] == 109.0            # last completed session
    assert md["ma5"] == pytest.approx(107.0)    # mean of 105..109
    assert md["ma10"] == pytest.approx(104.5)
    assert ctx["orb_high"] == 112.0             # first today bar's high
    assert ctx["session_high"] == 115.0         # max across today's bars
    assert ctx["session_low"] == 108.0          # min across today's bars


def test_ticker_sr_ctx_failopen_when_empty():
    # No bars → empty levels (fail-open), never raises.
    assert _ticker_sr_ctx([], [], NOW) == {"multi_day": {}}


def test_ticker_sr_ctx_excludes_today_from_daily():
    # A same-day "daily" bar must not be treated as a completed prior session.
    daily = [_bar(datetime(2026, 6, 26, 20, tzinfo=UTC), 200, 205, 195, 202)]
    ctx = _ticker_sr_ctx([], daily, NOW)
    assert ctx["multi_day"] == {}  # today excluded → no prev-day levels
