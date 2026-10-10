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

    def _set(pct="0", thr=0.0, on_quiet=True, usd="0"):
        monkeypatch.setattr(s, "condor_profit_target_pct", Decimal(str(pct)))
        monkeypatch.setattr(s, "condor_profit_target_usd", Decimal(str(usd)))
        monkeypatch.setattr(s, "condor_profit_target_open_range_pct", thr)
        monkeypatch.setattr(s, "condor_profit_target_on_quiet", on_quiet)
        return s

    return _set


# --- the debit threshold ---------------------------------------------------

def test_target_disabled_by_default_returns_none(cfg):
    cfg(pct="0")
    assert em._profit_target_debit(Decimal("40"), 28) is None


def test_50pct_target_is_half_the_credit(cfg):
    cfg(pct="0.5")
    # bank 50% of max profit => buy back for the remaining 50% of the credit
    assert em._profit_target_debit(Decimal("40"), 28) == Decimal("20.00")
    assert em._profit_target_debit(Decimal("35"), 28) == Decimal("17.50")


def test_75pct_target_leaves_a_quarter_of_the_credit(cfg):
    cfg(pct="0.75")
    assert em._profit_target_debit(Decimal("40"), 28) == Decimal("10.00")


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


# --- absolute-dollar target ($500 rule) ------------------------------------

def test_usd_target_threshold(cfg):
    """$500 across 28 contracts = $17.86/ct of profit."""
    cfg(usd="500")
    # credit 44 -> close at 44 - 500/28 = 26.14
    assert em._profit_target_debit(Decimal("44"), 28) == Decimal("26.14")


def test_usd_target_fires_far_earlier_on_rich_credit(cfg):
    """The whole reason the $500 rule differs: on #173's $59 credit it needs the
    mark at $41.10 while 50% needs $29.50, so it banks out much sooner."""
    cfg(usd="500")
    assert em._profit_target_debit(Decimal("59"), 28) == Decimal("41.14")
    cfg(pct="0.5")
    assert em._profit_target_debit(Decimal("59"), 28) == Decimal("29.50")


def test_usd_target_unreachable_on_thin_credit_is_no_target(cfg):
    """$500 on 1 contract would need a negative debit — treat as no target."""
    cfg(usd="500")
    assert em._profit_target_debit(Decimal("40"), 1) is None


def test_both_targets_set_takes_whichever_comes_first(cfg):
    """Debit FALLS as profit accrues, so 'first' is the HIGHER threshold."""
    cfg(pct="0.5", usd="500")
    # credit 59: pct -> 29.50, usd -> 41.14 ; usd is reached first
    assert em._profit_target_debit(Decimal("59"), 28) == Decimal("41.14")
    # credit 28: pct -> 14.00, usd -> 10.14 ; pct is reached first
    assert em._profit_target_debit(Decimal("28"), 28) == Decimal("14.00")


def test_usd_target_reason_names_the_dollar_amount(cfg):
    cfg(usd="500")
    ok, reason = em._decide_exit(
        credit_received=Decimal("44"), exit_debit=Decimal("26"),
        force=False, profit_target_debit=Decimal("26.14"),
    )
    assert ok and reason == "profit_target_$500"


# --- trailing profit lock --------------------------------------------------

@pytest.fixture
def trail(monkeypatch):
    s = em.get_settings()

    def _set(start="500", step="100", cap="1000"):
        monkeypatch.setattr(s, "condor_profit_trail_usd", Decimal(str(start)))
        monkeypatch.setattr(s, "condor_profit_trail_step_usd", Decimal(str(step)))
        monkeypatch.setattr(s, "condor_profit_trail_cap_usd", Decimal(str(cap)))
        return s

    return _set


def test_no_rung_below_the_ladder_start(trail):
    trail()
    assert em._trail_rung(Decimal("0")) is None
    assert em._trail_rung(Decimal("499")) is None


def test_rung_is_the_highest_step_reached(trail):
    trail()
    assert em._trail_rung(Decimal("500")) == Decimal("500")
    assert em._trail_rung(Decimal("599")) == Decimal("500")
    assert em._trail_rung(Decimal("600")) == Decimal("600")
    assert em._trail_rung(Decimal("720")) == Decimal("700")
    assert em._trail_rung(Decimal("650")) == Decimal("600")
    assert em._trail_rung(Decimal("999")) == Decimal("900")


def test_trail_disabled_yields_no_rung(trail):
    trail(start="0")
    assert em._trail_rung(Decimal("5000")) is None


def test_floor_ratchets_up_and_never_down():
    """The floor must survive between sweeps and only move up."""
    from types import SimpleNamespace

    class _Session:
        def __init__(self, store): self.store = store
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, _model, _id): return self.store
        def commit(self): pass

    store = SimpleNamespace(id=1, extra={})
    trade = SimpleNamespace(id=1, extra={})

    def factory():
        return _Session(store)

    s = em.get_settings()
    orig = (s.condor_profit_trail_usd, s.condor_profit_trail_step_usd)
    s.condor_profit_trail_usd = Decimal("500")
    s.condor_profit_trail_step_usd = Decimal("100")
    try:
        assert em._trail_floor(trade, Decimal("400"), factory) is None
        assert em._trail_floor(trade, Decimal("650"), factory) == Decimal("600")
        assert em._trail_floor(trade, Decimal("820"), factory) == Decimal("800")
        # profit falls back — floor must HOLD at its high-water mark
        assert em._trail_floor(trade, Decimal("610"), factory) == Decimal("800")
        assert trade.extra["profit_trail_floor"] == "800"
    finally:
        s.condor_profit_trail_usd, s.condor_profit_trail_step_usd = orig
