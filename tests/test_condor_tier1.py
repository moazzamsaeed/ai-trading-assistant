"""Tier-1 spot polling: cheap proximity probe that escalates to the full sweep.

Built 2026-10-22. The value is PRECISION for the stop de-bounce, not raw speed —
speed alone was shown harmful on #196 (the */2 sweep caught a 2-minute arming
window */5 would have missed, costing $3,808). At a 2-minute sweep a 180s
de-bounce is 2-3 coarse samples; at 15s it is 12.

Tier-1 must never be load-bearing: tier-2's own cron stays as the safety net, so
every failure path here returns "escalate" or "do nothing", never "suppress".
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents.options import exit_monitor as em

SP = "SPY261022P00762000"   # strike 762
SC = "SPY261022C00772000"   # strike 772


def _factory(trades):
    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    sess = _S()

    def f():
        return sess

    return f, sess


@pytest.fixture
def probe(monkeypatch):
    """Run spot_near_any_open_condor with controlled trades + spot."""
    def _run(*, spot, trades=((1, SP, SC),), band=0.001):
        rows = [SimpleNamespace(id=i, extra={"short_put": sp, "short_call": sc})
                for i, sp, sc in trades]
        monkeypatch.setattr(em, "_open_iron_condor_trades", lambda _s: rows)
        f, _ = _factory(rows)

        async def fetcher(_sym):
            if spot is None:
                return None
            return SimpleNamespace(mid=Decimal(str(spot)), bid=None, ask=None)

        return asyncio.run(em.spot_near_any_open_condor(
            stock_fetcher=fetcher, session_factory=f, band_pct=band))

    return _run


def test_no_open_condor_means_no_quote_at_all(monkeypatch):
    """Cheapest path: nothing open -> not even a spot fetch."""
    monkeypatch.setattr(em, "_open_iron_condor_trades", lambda _s: [])
    called = []

    async def fetcher(_sym):
        called.append(1)
        return SimpleNamespace(mid=Decimal("770"), bid=None, ask=None)

    f, _ = _factory([])
    out = asyncio.run(em.spot_near_any_open_condor(
        stock_fetcher=fetcher, session_factory=f))
    assert out is None
    assert not called, "must not fetch a quote with no open position"


def test_mid_band_spot_does_not_escalate(probe):
    """SPY comfortably inside both shorts -> no chain fetch."""
    assert probe(spot=767.0) is None


def test_near_the_put_escalates(probe):
    # band 0.001 of 762 = $0.76 -> escalate at/below 762.76
    assert probe(spot=762.70) is not None
    assert probe(spot=763.50) is None


def test_near_the_call_escalates(probe):
    # band 0.001 of 772 = $0.77 -> escalate at/above 771.23
    assert probe(spot=771.40) is not None
    assert probe(spot=770.00) is None


def test_through_the_strike_escalates(probe):
    out = probe(spot=761.00)
    assert out is not None
    assert out["trade_ids"] == [1]


def test_unavailable_spot_escalates_rather_than_suppresses(probe):
    """A blind quote must not silently stop tier-2 from evaluating."""
    out = probe(spot=None)
    assert out is not None
    assert out["reason"] == "spot_unavailable"


def test_only_the_affected_trades_are_reported(probe):
    """Two open condors, one near its strike -> only that one escalates."""
    out = probe(
        spot=761.00,
        trades=((1, SP, SC), (2, "SPY261022P00740000", "SPY261022C00790000")),
    )
    assert out["trade_ids"] == [1]


def test_wider_band_escalates_earlier(probe):
    assert probe(spot=764.00, band=0.001) is None
    assert probe(spot=764.00, band=0.005) is not None   # 0.5% of 762 = $3.81


# --- the scheduler job -----------------------------------------------------

def test_tier1_job_registers_only_when_configured(monkeypatch):
    from trademaster import scheduler as sch

    async def n(_t): pass

    def build(secs):
        monkeypatch.setattr(sch.get_settings(), "condor_tier1_seconds", secs)
        monkeypatch.setattr(sch.get_settings(), "enable_directional", False)
        monkeypatch.setattr(sch.get_settings(), "enable_equities_scanner", False)
        monkeypatch.setattr(sch.get_settings(), "enable_research", False)
        s = sch.make_scheduler(
            research_poster=n, signal_poster=n, trade_poster=n, log_poster=n,
            stock_signal_poster=n, enable_iron_condor=True)
        return {j.id for j in s.get_jobs()}

    assert "condor_tier1_spot" not in build(0)
    assert "condor_tier1_spot" in build(15)


def test_tier1_job_does_not_escalate_when_probe_is_quiet(monkeypatch):
    """No proximity -> tier-2 must not be invoked (that is the whole saving)."""
    from trademaster import scheduler as sch

    ran = []

    async def quiet():
        return None

    async def fake_exit(**_kw):
        ran.append(1)

    async def clock():
        return SimpleNamespace(is_open=True, next_open=None)

    monkeypatch.setattr(sch, "spot_near_any_open_condor", quiet)
    monkeypatch.setattr(sch, "_iron_condor_exit_job", fake_exit)

    async def noop(_t): pass

    asyncio.run(sch._condor_tier1_job(
        signal_poster=noop, trade_poster=noop, clock_fetcher=clock))
    assert not ran


def test_tier1_job_escalates_on_proximity(monkeypatch):
    from trademaster import scheduler as sch

    ran = []

    async def hot():
        return {"spot": "761.00", "trade_ids": [1]}

    async def fake_exit(**_kw):
        ran.append(1)

    async def clock():
        return SimpleNamespace(is_open=True, next_open=None)

    monkeypatch.setattr(sch, "spot_near_any_open_condor", hot)
    monkeypatch.setattr(sch, "_iron_condor_exit_job", fake_exit)

    async def noop(_t): pass

    asyncio.run(sch._condor_tier1_job(
        signal_poster=noop, trade_poster=noop, clock_fetcher=clock))
    assert ran == [1]


def test_tier1_probe_failure_is_swallowed(monkeypatch):
    """Tier-1 is an accelerator. A failure must not raise into the scheduler,
    and tier-2's own cron still covers the position."""
    from trademaster import scheduler as sch

    async def boom():
        raise RuntimeError("feed down")

    async def clock():
        return SimpleNamespace(is_open=True, next_open=None)

    monkeypatch.setattr(sch, "spot_near_any_open_condor", boom)

    async def noop(_t): pass

    asyncio.run(sch._condor_tier1_job(
        signal_poster=noop, trade_poster=noop, clock_fetcher=clock))


def test_tier1_skips_when_market_closed(monkeypatch):
    from trademaster import scheduler as sch

    probed = []

    async def probe():
        probed.append(1)
        return None

    async def closed():
        return SimpleNamespace(is_open=False, next_open=None)

    monkeypatch.setattr(sch, "spot_near_any_open_condor", probe)

    async def noop(_t): pass

    asyncio.run(sch._condor_tier1_job(
        signal_poster=noop, trade_poster=noop, clock_fetcher=closed))
    assert not probed
