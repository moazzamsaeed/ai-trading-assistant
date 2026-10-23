"""Time-scaled stop arming + long-leg recovery on a loss-cut.

Both added 2026-10-22 after measuring the 11 stop-outs: $1,786 per stop was
TIME VALUE (not intrinsic) and $186 per stop of long-leg premium was discarded.
The stop fires with SPY on the strike, which is peak extrinsic — so require a
measurable breach, deeper early in the session than late.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents.options import exit_monitor as em
from trademaster.timeutils import ET

SP = "SPY261022P00762000"   # strike 762
LP = "SPY261022P00757000"   # strike 757
SC = "SPY261022C00772000"   # strike 772
LC = "SPY261022C00777000"   # strike 777


@pytest.fixture
def depth(monkeypatch):
    s = em.get_settings()

    def _set(open_pct=0.0, close_pct=0.0):
        monkeypatch.setattr(s, "condor_stop_arm_through_pct_open", open_pct)
        monkeypatch.setattr(s, "condor_stop_arm_through_pct_close", close_pct)
        return s

    return _set


def _at(hh, mm):
    return datetime(2026, 10, 22, hh, mm, tzinfo=ET).astimezone(UTC)


# --- the time scale -------------------------------------------------------

def test_depth_is_zero_when_unconfigured(depth):
    depth()
    assert em._stop_arm_depth(_at(10, 0)) == 0.0
    assert em._stop_arm_depth(_at(15, 0)) == 0.0


def test_depth_is_deepest_at_entry_and_shallowest_at_the_deadline(depth):
    depth(open_pct=0.0026, close_pct=0.0003)
    at_open = em._stop_arm_depth(_at(10, 0))
    at_close = em._stop_arm_depth(_at(15, 45))
    assert at_open == pytest.approx(0.0026, abs=1e-5)
    assert at_close == pytest.approx(0.0003, abs=1e-5)
    assert at_open > at_close


def test_depth_decreases_monotonically_through_the_session(depth):
    depth(open_pct=0.0026, close_pct=0.0003)
    seq = [em._stop_arm_depth(_at(h, 0)) for h in (10, 11, 12, 13, 14, 15)]
    assert seq == sorted(seq, reverse=True), seq


def test_depth_clamps_past_the_deadline(depth):
    """After 15:45 the requirement must not go negative."""
    depth(open_pct=0.0026, close_pct=0.0003)
    assert em._stop_arm_depth(_at(15, 55)) == pytest.approx(0.0003, abs=1e-5)


# --- the gate -------------------------------------------------------------

def test_zero_depth_arms_at_the_touch():
    """Identical to the old band-0 behaviour."""
    assert em._through_short_strike(Decimal("762.00"), SP, SC, 0.0) is True
    assert em._through_short_strike(Decimal("762.01"), SP, SC, 0.0) is False


def test_depth_requires_a_real_breach():
    # 0.0026 of 762 = $1.98 -> needs SPY <= 760.02
    assert em._through_short_strike(Decimal("761.50"), SP, SC, 0.0026) is False
    assert em._through_short_strike(Decimal("760.00"), SP, SC, 0.0026) is True


def test_todays_whipsaw_would_not_have_armed_in_the_morning():
    """#196 (2026-10-22) armed at SPY 761.64 — $0.36 through, 73% time value —
    and settled back at 764.03. A morning depth requirement holds it."""
    morning = 0.0026                      # ~$1.98 on a 762 strike
    assert em._through_short_strike(Decimal("761.64"), SP, SC, morning) is False
    # late in the session the same breach DOES arm
    late = 0.0003                         # ~$0.23
    assert em._through_short_strike(Decimal("761.64"), SP, SC, late) is True


def test_call_side_requires_breach_upward():
    # 0.0026 of 772 = $2.01 -> needs SPY >= 774.01
    assert em._through_short_strike(Decimal("773.00"), SP, SC, 0.0026) is False
    assert em._through_short_strike(Decimal("774.50"), SP, SC, 0.0026) is True


def test_unknown_spot_fails_safe_to_armed():
    """A blind quote must still cut, never silently hold a loser."""
    assert em._through_short_strike(None, SP, SC, 0.0026) is True


def test_unparseable_strikes_fail_safe_to_armed():
    assert em._through_short_strike(Decimal("770"), "garbage", SC, 0.0026) is True


# --- long-leg recovery ----------------------------------------------------

class _Sess:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, *_a): return None
    def commit(self): pass


def _run_legout(*, long_seller, qty=28):
    trade = SimpleNamespace(id=196, qty=qty, entry_price=Decimal("43"), extra={})
    calls = []

    async def closer(**kw):
        calls.append(("buy", kw["occ_symbol"]))
        return SimpleNamespace(order_id="b1")

    async def seller(**kw):
        calls.append(("sell", kw["occ_symbol"]))
        return SimpleNamespace(order_id="s1")

    async def waiter(oid, **_k):
        return SimpleNamespace(
            status="filled",
            filled_avg_price=Decimal("1.34") if oid == "b1" else Decimal("0.13"),
        )

    res = asyncio.run(em._assignment_leg_out(
        trade, spot=Decimal("761.50"), legs=(SP, LP, SC, LC), chain=[],
        closer=closer, waiter=waiter, buf=0.003, fill_timeout_s=1.0,
        factory=lambda: _Sess(), close_all=True, reason="stop_loss_1.5x",
        long_seller=seller if long_seller else None,
    ))
    return res, calls


def test_loss_cut_sells_the_paired_longs():
    res, calls = _run_legout(long_seller=True)
    assert ("sell", LP) in calls and ("sell", LC) in calls
    assert res["longs_sold"] == ["put", "call"]


def test_without_the_seller_longs_are_left_to_expire():
    """The 15:45 assignment leg-out passes no seller — unchanged behaviour."""
    res, calls = _run_legout(long_seller=False)
    assert not [c for c in calls if c[0] == "sell"]
    assert res["longs_sold"] == []


def test_shorts_are_still_bought_back_first():
    _, calls = _run_legout(long_seller=True)
    assert calls[0] == ("buy", SP)


# --- settlement must not double-count a SOLD long ------------------------

def test_settlement_credits_actual_sale_not_intrinsic_when_the_long_was_sold():
    """The reconciler normally credits the long's EXPIRY INTRINSIC on the
    assumption it is still held. If the loss-cut sold it, crediting intrinsic
    too would double-count. #196: short put bought at 1.34, long sold at 0.13,
    SPY settled 764.03 so the 757 long expired worthless (intrinsic 0)."""
    from trademaster.reconciler import _condor_settlement_debit_legout as f

    common = dict(short_put=Decimal("762"), long_put=Decimal("757"),
                  short_call=Decimal("772"), long_call=Decimal("777"),
                  closed_put_fill=Decimal("1.34"), closed_call_fill=None,
                  max_loss_per_contract=Decimal("457"))
    # long still held, expired worthless -> cost is the full buyback
    held = f(764.03, **common)
    # long SOLD for 0.13 -> that credit reduces the cost
    sold = f(764.03, **common, sold_put_fill=Decimal("0.13"))
    assert held == Decimal("134.00")
    assert sold == Decimal("121.00")
    assert held - sold == Decimal("13.00")      # exactly the sale proceeds


def test_settlement_sold_long_in_the_money_still_credits_the_sale():
    """If SPY had kept falling the long would finish ITM. Crediting its
    intrinsic AND the sale proceeds would be the double-count we are avoiding."""
    from trademaster.reconciler import _condor_settlement_debit_legout as f

    common = dict(short_put=Decimal("762"), long_put=Decimal("757"),
                  short_call=Decimal("772"), long_call=Decimal("777"),
                  closed_put_fill=Decimal("3.00"), closed_call_fill=None,
                  max_loss_per_contract=Decimal("457"))
    # SPY 755 -> the 757 long would have been worth 2.00 at expiry
    held = f(755.0, **common)                                  # credits 2.00
    sold = f(755.0, **common, sold_put_fill=Decimal("1.80"))    # credits the 1.80 sale
    assert held == Decimal("100.00")
    assert sold == Decimal("120.00")
    assert sold > held          # selling early for less than intrinsic costs us
