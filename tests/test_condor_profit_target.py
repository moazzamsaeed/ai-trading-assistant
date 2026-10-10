"""Opt-in condor profit target, gated on the session's opening range.

Enabled 2026-10-10 as a deliberate variance-reduction experiment for October —
NOT as an edge play. Five formulations were measured on real option bars and
none produced return (see project_condor_whipsaw_verdict); what is real is
~-40% per-trade volatility at a return cost set by real close slippage.

The gate DIRECTION matters and is the counter-intuitive part: quiet mornings
produce a lower VIX1D, so the engine sets TIGHTER strikes (mean band 7.0pt vs
8.0pt) with less cushion — and that is where the losses are. Calm mornings ran
13W/9L at -$416/trade vs active mornings 20W/3L at +$680/trade in the 28ct era.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agents.options import exit_monitor as em


@pytest.fixture
def cfg(monkeypatch):
    """Set the profit-target knobs on the live settings object."""
    s = em.get_settings()

    def _set(pct="0", thr=0.0, on_quiet=True):
        monkeypatch.setattr(s, "condor_profit_target_pct", Decimal(str(pct)))
        monkeypatch.setattr(s, "condor_profit_target_open_range_pct", thr)
        monkeypatch.setattr(s, "condor_profit_target_on_quiet", on_quiet)
        return s

    return _set


# --- the debit threshold ---------------------------------------------------

def test_target_disabled_by_default_returns_none(cfg):
    cfg(pct="0")
    assert em._profit_target_debit(Decimal("40")) is None


def test_50pct_target_is_half_the_credit(cfg):
    cfg(pct="0.5")
    # bank 50% of max profit => buy back for the remaining 50% of the credit
    assert em._profit_target_debit(Decimal("40")) == Decimal("20.00")
    assert em._profit_target_debit(Decimal("35")) == Decimal("17.50")


def test_75pct_target_leaves_a_quarter_of_the_credit(cfg):
    cfg(pct="0.75")
    assert em._profit_target_debit(Decimal("40")) == Decimal("10.00")


# --- the opening-range gate ------------------------------------------------

def test_no_threshold_means_every_day(cfg):
    cfg(pct="0.5", thr=0.0)
    assert em._profit_target_applies(0.11) is True
    assert em._profit_target_applies(0.59) is True


def test_quiet_mode_applies_below_the_threshold(cfg):
    """Default direction — the one the data supports."""
    cfg(pct="0.5", thr=0.24, on_quiet=True)
    assert em._profit_target_applies(0.17) is True    # quiet morning -> protect
    assert em._profit_target_applies(0.24) is True    # boundary is inclusive
    assert em._profit_target_applies(0.40) is False   # active morning -> let it run


def test_active_mode_inverts_the_gate(cfg):
    """Opposite direction, exposed so the intuition is testable."""
    cfg(pct="0.5", thr=0.24, on_quiet=False)
    assert em._profit_target_applies(0.40) is True
    assert em._profit_target_applies(0.17) is False


def test_unknown_opening_range_does_not_apply_the_target(cfg):
    """A feed failure must never silently change exit behaviour."""
    cfg(pct="0.5", thr=0.24)
    assert em._profit_target_applies(None) is False


def test_unknown_range_is_fine_when_no_gate_is_configured(cfg):
    cfg(pct="0.5", thr=0.0)
    assert em._profit_target_applies(None) is True


# --- interaction with the loss exits ---------------------------------------

def _decide(**kw):
    base = {"credit_received": Decimal("40"), "exit_debit": Decimal("20"),
            "force": False}
    return em._decide_exit(**{**base, **kw})


def test_target_fires_when_debit_falls_to_it(cfg):
    cfg(pct="0.5")
    ok, reason = _decide(exit_debit=Decimal("20"), profit_target_debit=Decimal("20"))
    assert ok and reason == "profit_target_50pct"


def test_target_does_not_fire_above_the_threshold(cfg):
    cfg(pct="0.5")
    ok, reason = _decide(exit_debit=Decimal("21"), profit_target_debit=Decimal("20"))
    assert not ok and reason == ""


def test_force_close_still_wins_over_the_target(cfg):
    cfg(pct="0.5")
    ok, reason = _decide(force=True, profit_target_debit=Decimal("20"))
    assert ok and reason == "force_close_15:50"


def test_stop_still_fires_when_the_trade_is_losing(cfg):
    """Target and stop are mutually exclusive — one needs a LOW debit, the other
    a HIGH one — so enabling the target must not disturb the stop."""
    cfg(pct="0.5")
    ok, reason = _decide(exit_debit=Decimal("100"), profit_target_debit=Decimal("20"))
    assert ok and reason.startswith("stop_loss")


def test_daily_cap_still_fires(cfg):
    cfg(pct="0.5")
    ok, reason = _decide(
        exit_debit=Decimal("500"), profit_target_debit=Decimal("20"),
        daily_loss_cap_per_contract=Decimal("300"),
    )
    assert ok and reason == "daily_loss_cap"


def test_target_off_leaves_behaviour_identical(cfg):
    """With the flag off nothing about the existing strategy changes."""
    cfg(pct="0")
    assert _decide(exit_debit=Decimal("1"), profit_target_debit=None) == (False, "")
    ok, reason = _decide(exit_debit=Decimal("100"), profit_target_debit=None)
    assert ok and reason.startswith("stop_loss")
