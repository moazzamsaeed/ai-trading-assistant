"""Reconciler tests — focus on 0DTE iron-condor expiry settlement.

The condor exit monitor closes by submitting an MLEG buy_to_close, which can
fail near expiry (legs swept early → APIError 42210000, or no quotes). The
daemon's 16:15 stop-timer then fires before the 16:00 settlement is booked, so
a won condor would sit open forever (the directional reconciler ignored condors
entirely). These tests cover the settlement-by-payoff path that fixes that.
"""

from __future__ import annotations

import types
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from integrations import alpaca_client
from trademaster import reconciler
from trademaster.db import Base, Trade, make_engine, make_session_factory
from trademaster.reconciler import (
    _condor_settlement_debit,
    _strike_from_occ,
    reconcile_positions,
)
from trademaster.timeutils import ET, to_et


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# ----------------- payoff math -----------------


def test_strike_from_occ():
    assert _strike_from_occ("SPY260629P00732000") == Decimal("732")
    assert _strike_from_occ("SPY260629C00742500") == Decimal("742.5")


# Condor: long_put 490 / short_put 495 / short_call 505 / long_call 510, $5 wings.
_STRIKES = dict(
    short_put=Decimal("495"), long_put=Decimal("490"),
    short_call=Decimal("505"), long_call=Decimal("510"),
)


def test_settlement_worthless_full_credit():
    # spot between the shorts → both spreads expire worthless → debit 0.
    debit = _condor_settlement_debit(500.0, **_STRIKES, max_loss_per_contract=Decimal("420"))
    assert debit == Decimal("0.00")


def test_settlement_partial_put_breach():
    # spot 493: short put 495 ITM by 2, long put 490 still OTM → 2/share → $200/ct.
    debit = _condor_settlement_debit(493.0, **_STRIKES, max_loss_per_contract=Decimal("420"))
    assert debit == Decimal("200.00")


def test_settlement_capped_at_max_loss():
    # spot 480 (below long put) → full $5 wing = $500/ct, capped at recorded $420 max loss.
    debit = _condor_settlement_debit(480.0, **_STRIKES, max_loss_per_contract=Decimal("420"))
    assert debit == Decimal("420")


def test_settlement_call_side_breach():
    # spot 507: short call 505 ITM by 2 → $200/ct (put side worthless).
    debit = _condor_settlement_debit(507.0, **_STRIKES, max_loss_per_contract=Decimal("420"))
    assert debit == Decimal("200.00")


# ----------------- end-to-end settlement -----------------


def _open_condor(session_factory, *, expiry: str, credit: str = "80.00", qty: int = 1) -> int:
    legs = {
        "short_put": "SPY260511P00495000",
        "long_put": "SPY260511P00490000",
        "short_call": "SPY260511C00505000",
        "long_call": "SPY260511C00510000",
    }
    with session_factory() as s:
        row = Trade(
            symbol="SPY", asset_class="option", side="sell", strategy="spy_0dte_ic",
            qty=Decimal(qty), entry_price=Decimal(credit), alpaca_order_id="open_1",
            opened_at=datetime.fromisoformat(expiry).replace(hour=13, minute=45, tzinfo=UTC),
            extra={**legs, "structure": "iron_condor", "wing_width": "5",
                   "credit_per_contract": credit, "max_loss_per_contract": "420.00",
                   "expiry": expiry},
        )
        s.add(row)
        s.commit()
        return int(row.id)


def _patch_alpaca(monkeypatch, *, positions, spot_on_expiry, expiry: str):
    async def fake_positions():
        return positions

    async def fake_daily_bars(symbol, *, limit=10):
        ts = datetime.fromisoformat(expiry).replace(hour=20, tzinfo=UTC)  # ~16:00 ET
        return [types.SimpleNamespace(timestamp=ts, close=Decimal(str(spot_on_expiry)))]

    async def fake_recent_bars(symbol, *, timeframe_minutes=5, limit=30, warmup_days=0):
        return []  # no same-day intraday bars → settlement falls back to the daily bar

    monkeypatch.setattr(alpaca_client, "get_positions", fake_positions)
    monkeypatch.setattr(alpaca_client, "get_daily_bars", fake_daily_bars)
    monkeypatch.setattr(alpaca_client, "get_recent_bars", fake_recent_bars)


@pytest.mark.asyncio
async def test_expired_condor_settled_full_credit(session_factory, monkeypatch):
    tid = _open_condor(session_factory, expiry="2026-05-11", credit="80.00")
    # Legs gone from broker (expired/swept), SPY closed 500 → between shorts → full win.
    _patch_alpaca(monkeypatch, positions=[], spot_on_expiry=500.0, expiry="2026-05-11")

    warnings = await reconcile_positions(session_factory=session_factory)

    with session_factory() as s:
        row = s.get(Trade, tid)
    assert row.closed_at is not None, "expired condor must be closed"
    assert row.realized_pnl_usd == Decimal("80.00"), "worthless expiry keeps full credit"
    assert row.exit_price == Decimal("0.00")
    assert row.extra["exit_reason"] == "expired_settled"
    assert row.extra["settlement_spot"] == 500.0
    # Dated to the 16:00 ET expiry, not next-morning startup. SQLite drops tzinfo
    # on read, so the stored value comes back naive UTC — interpret it as UTC
    # (what the app's _as_aware_utc helper does) before converting to ET.
    closed_utc = row.closed_at.replace(tzinfo=UTC)
    assert closed_utc.astimezone(ET).date() == date(2026, 5, 11)
    assert closed_utc.astimezone(ET).hour == 16
    assert any("settled" in w for w in warnings)


@pytest.mark.asyncio
async def test_expired_condor_settled_breach(session_factory, monkeypatch):
    tid = _open_condor(session_factory, expiry="2026-05-11", credit="80.00", qty=2)
    # SPY closed 493 → short put 495 ITM by 2 → $200/ct debit; realized = (80-200)*2.
    _patch_alpaca(monkeypatch, positions=[], spot_on_expiry=493.0, expiry="2026-05-11")

    await reconcile_positions(session_factory=session_factory)

    with session_factory() as s:
        row = s.get(Trade, tid)
    assert row.realized_pnl_usd == Decimal("-240.00")  # (80 - 200) * 2
    assert row.exit_price == Decimal("200.00")


@pytest.mark.asyncio
async def test_open_condor_not_yet_expired_left_alone(session_factory, monkeypatch):
    # Expiry in the far future → must NOT be settled.
    future = (datetime.now(UTC).date().replace(year=datetime.now(UTC).year + 1)).isoformat()
    tid = _open_condor(session_factory, expiry=future)
    _patch_alpaca(monkeypatch, positions=[], spot_on_expiry=500.0, expiry=future)

    await reconcile_positions(session_factory=session_factory)

    with session_factory() as s:
        row = s.get(Trade, tid)
    assert row.closed_at is None, "a not-yet-expired condor must stay open"


# ----------------- same-day settlement (settle_expired_condors) -----------------


def _intraday_bar(d: date, close: str, *, hh: int = 15, mm: int = 55):
    ts = datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)
    return types.SimpleNamespace(timestamp=ts, close=Decimal(close))


@pytest.mark.asyncio
async def test_same_day_condor_settles_after_bell(session_factory, monkeypatch):
    """A condor expiring TODAY settles once the ET clock is past 16:00, using the
    last intraday close (the daily bar isn't published yet right after the bell)."""
    today = date(2026, 6, 25)
    tid = _open_condor(session_factory, expiry=today.isoformat(), credit="80.00")
    now = datetime.combine(today, time(16, 3), tzinfo=ET).astimezone(UTC)

    async def recent(symbol, *, timeframe_minutes=5, limit=30, warmup_days=0):
        return [_intraday_bar(today, "500")]  # between shorts (495/505) → worthless

    async def daily_boom(*a, **k):
        raise AssertionError("daily bar should not be needed — intraday close present")

    monkeypatch.setattr(alpaca_client, "get_recent_bars", recent)
    monkeypatch.setattr(alpaca_client, "get_daily_bars", daily_boom)

    warnings = await reconciler.settle_expired_condors(now=now, session_factory=session_factory)

    with session_factory() as s:
        row = s.get(Trade, tid)
    assert row.closed_at is not None
    assert row.realized_pnl_usd == Decimal("80.00")   # full credit kept
    assert row.exit_price == Decimal("0.00")
    assert row.extra["settlement_spot"] == 500.0
    assert row.closed_at.replace(tzinfo=UTC).astimezone(ET).hour == 16  # dated to expiry bell
    assert any("settled" in w for w in warnings)


@pytest.mark.asyncio
async def test_same_day_condor_not_settled_before_bell(session_factory, monkeypatch):
    """Before 16:00 ET the 0DTE is still live — the gate must not settle it, and
    must not even reach for a settlement price."""
    today = date(2026, 6, 25)
    tid = _open_condor(session_factory, expiry=today.isoformat(), credit="80.00")
    now = datetime.combine(today, time(11, 0), tzinfo=ET).astimezone(UTC)

    async def boom(*a, **k):
        raise AssertionError("must not fetch a settlement price before the bell")

    monkeypatch.setattr(alpaca_client, "get_recent_bars", boom)
    monkeypatch.setattr(alpaca_client, "get_daily_bars", boom)

    warnings = await reconciler.settle_expired_condors(now=now, session_factory=session_factory)

    assert warnings == []
    with session_factory() as s:
        assert s.get(Trade, tid).closed_at is None


@pytest.mark.asyncio
async def test_settlement_close_prefers_intraday_then_daily(monkeypatch):
    exp = date(2026, 6, 25)
    now = datetime.combine(exp, time(16, 3), tzinfo=ET).astimezone(UTC)

    # (a) intraday bar ON the expiry date → used directly as the settlement close.
    async def recent_on_expiry(symbol, **k):
        return [_intraday_bar(exp, "742.5")]
    monkeypatch.setattr(alpaca_client, "get_recent_bars", recent_on_expiry)
    assert await reconciler._settlement_close_on(exp, now=now) == 742.5

    # (b) intraday bars belong to a LATER date → fall back to the daily bar.
    async def recent_other_day(symbol, **k):
        return [_intraday_bar(date(2026, 6, 26), "999")]
    async def daily(symbol, *, limit=10):
        ts = datetime.combine(exp, time(16, 0), tzinfo=ET).astimezone(UTC)
        return [types.SimpleNamespace(timestamp=ts, close=Decimal("740.0"))]
    monkeypatch.setattr(alpaca_client, "get_recent_bars", recent_other_day)
    monkeypatch.setattr(alpaca_client, "get_daily_bars", daily)
    assert await reconciler._settlement_close_on(exp, now=now) == 740.0
