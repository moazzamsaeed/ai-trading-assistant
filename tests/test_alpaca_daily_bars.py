"""Regression: get_daily_bars must return the MOST RECENT `limit` sessions.

The bug: it passed the low `limit` straight to Alpaca alongside a ~30-day-back
`start`. Alpaca returns bars OLDEST-first and truncates at `limit`, so it kept the
OLDEST `limit` sessions (observed live: newest bar 3 weeks stale). That silently
broke the settlement reconciler — `_underlying_close_on` never found the expiry
date, so expired 0DTE condors never settled.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from integrations import alpaca_client


def _raw(day_index: int):
    """A raw daily bar `_to_bar` can consume; close encodes the session index."""
    ts = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=day_index)
    px = 700 + day_index
    return SimpleNamespace(
        timestamp=ts, open=px, high=px, low=px, close=px, volume=1000, vwap=px,
    )


async def test_get_daily_bars_returns_most_recent_not_oldest(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def get_stock_bars(self, req):
            captured["req_limit"] = req.limit
            # Alpaca returns OLDEST-first; hand back 20 sessions (0..19).
            return {"SPY": [_raw(i) for i in range(20)]}

    monkeypatch.setattr(alpaca_client, "_stock_client", lambda: FakeClient())

    bars = await alpaca_client.get_daily_bars("SPY", limit=10)

    # Must return the LAST 10 sessions (10..19), i.e. the most recent — the whole
    # point. The pre-fix code returned the first 10 (0..9).
    assert len(bars) == 10
    assert [int(b.close) for b in bars] == list(range(710, 720))
    # And it must over-fetch (request > limit) so the tail is reachable at all.
    assert captured["req_limit"] > 10
