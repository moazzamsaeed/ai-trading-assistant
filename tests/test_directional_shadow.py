"""Signals-only shadow P&L tracker tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from agents.directional import shadow as sh
from agents.directional.intraday import TickerDecision as _TD
from agents.directional.shadow import (
    SHADOW_STRATEGY,
    record_shadow_signal,
    score_shadow_signals,
    shadow_summary,
)
from integrations.alpaca_client import OptionQuote
from trademaster.db import Base, Trade, make_engine, make_session_factory


def _sf():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _quote(occ: str, *, bid: str, ask: str) -> OptionQuote:
    b, a = Decimal(bid), Decimal(ask)
    return OptionQuote(
        occ_symbol=occ, underlying="SPY", strike=Decimal("500"),
        expiry=date(2026, 7, 29), option_type="call",
        bid=b, ask=a, mid=((b + a) / 2),
        delta=None, gamma=None, theta=None, vega=None, implied_volatility=None,
    )


class _Selected:
    def __init__(self, occ, strike, quote):
        self.occ, self.strike, self.quote = occ, strike, quote


async def _record(sf, *, conviction="HIGH", action="BUY_CALL", ask="1.00", mode="selective"):
    d = _TD("SPY", action, 500.0, "0DTE", conviction, "strong breakout")

    async def selector(*_a, **_k):
        return _Selected("SPY260729C00500000", Decimal("500"), _quote("SPY260729C00500000", bid="0.98", ask=ask))

    return await record_shadow_signal(
        d, mode=mode, today=date(2026, 7, 29),
        session_factory=sf, strike_selector=selector,
    )


async def test_record_persists_open_shadow(monkeypatch):
    sf = _sf()
    tid = await _record(sf, ask="1.00")
    assert tid is not None
    with sf() as s:
        row = s.get(Trade, tid)
        assert row.strategy == SHADOW_STRATEGY
        assert row.closed_at is None
        assert row.entry_price == Decimal("1.00")   # entry = ask
        assert row.qty == Decimal("1")
        assert row.extra["shadow"] is True
        assert row.extra["conviction"] == "HIGH"


async def test_record_skips_hold():
    d = _TD("SPY", "HOLD", None, "0DTE", "LOW", "no trend")
    assert await record_shadow_signal(d, mode="selective") is None


async def test_score_closes_on_profit_target(monkeypatch):
    # selective pt=0.5 → entry 1.00, PT at 1.50. mid 1.60 ≥ 1.50 → profit target.
    sf = _sf()
    await _record(sf, ask="1.00")

    async def qf(_occ):
        return _quote("SPY260729C00500000", bid="1.55", ask="1.65")  # mid 1.60

    results = await score_shadow_signals(
        session_factory=sf, quote_fetcher=qf, force_close=False,
    )
    assert results[0]["status"] == "closed"
    assert results[0]["reason"] == "shadow_profit_target"
    # pnl = (1.60 - 1.00) * 100 * 1 = 60
    assert Decimal(results[0]["pnl"]) == Decimal("60.00")


async def test_score_closes_on_stop(monkeypatch):
    # selective sl=0.3 → stop at 0.70. mid 0.60 ≤ 0.70 → stop.
    sf = _sf()
    await _record(sf, ask="1.00")

    async def qf(_occ):
        return _quote("SPY260729C00500000", bid="0.58", ask="0.62")  # mid 0.60

    results = await score_shadow_signals(session_factory=sf, quote_fetcher=qf, force_close=False)
    assert results[0]["reason"] == "shadow_stop"
    assert Decimal(results[0]["pnl"]) == Decimal("-40.00")


async def test_score_holds_between_thresholds(monkeypatch):
    sf = _sf()
    await _record(sf, ask="1.00")

    async def qf(_occ):
        return _quote("SPY260729C00500000", bid="1.08", ask="1.12")  # mid 1.10, between 0.70 and 1.50

    results = await score_shadow_signals(session_factory=sf, quote_fetcher=qf, force_close=False)
    assert results[0]["status"] == "hold"
    with sf() as s:
        # peak tracked, still open
        rows = [r for r in s.query(Trade).all() if r.strategy == SHADOW_STRATEGY]
        assert rows[0].closed_at is None
        assert rows[0].extra["peak_premium"] == "1.10"


async def test_force_close_realizes_at_mark(monkeypatch):
    sf = _sf()
    await _record(sf, ask="1.00")

    async def qf(_occ):
        return _quote("SPY260729C00500000", bid="1.08", ask="1.12")  # mid 1.10, no threshold hit

    results = await score_shadow_signals(session_factory=sf, quote_fetcher=qf, force_close=True)
    assert results[0]["reason"] == "shadow_force_close"
    assert Decimal(results[0]["pnl"]) == Decimal("10.00")


async def test_force_close_no_quote_is_worthless(monkeypatch):
    sf = _sf()
    await _record(sf, ask="1.00")

    async def qf(_occ):
        return None  # no market at expiry

    results = await score_shadow_signals(session_factory=sf, quote_fetcher=qf, force_close=True)
    # A worthless (0) mark at force-close is at/below the stop threshold, so it's
    # labelled shadow_stop — either way it's the max loss. P&L is the point.
    assert results[0]["status"] == "closed"
    assert Decimal(results[0]["pnl"]) == Decimal("-100.00")  # realized at 0


async def test_summary_by_conviction(monkeypatch):
    sf = _sf()
    # one HIGH winner, one MEDIUM loser
    await _record(sf, ask="1.00", conviction="HIGH")
    await _record(sf, ask="1.00", conviction="MEDIUM")

    calls = {"n": 0}
    async def qf(_occ):
        calls["n"] += 1
        # first scored row → winner (mid 1.60), second → loser (mid 0.60)
        return _quote("x", bid="1.55", ask="1.65") if calls["n"] == 1 else _quote("x", bid="0.58", ask="0.62")

    await score_shadow_signals(session_factory=sf, quote_fetcher=qf, force_close=True)
    out = shadow_summary(session_factory=sf)
    assert out is not None
    assert "HIGH" in out and "MEDIUM" in out
    assert "2 signals" in out


async def test_summary_none_when_empty():
    assert shadow_summary(session_factory=_sf()) is None
