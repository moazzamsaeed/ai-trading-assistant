"""Tests for the day-end condor projection in the #trades report.

A 0DTE condor held to expiry isn't booked until the next-morning reconcile, so the
daily report projects its expiry outcome (read-only) from the underlying close. The
projection reuses the reconciler's payoff math; these tests cover closed / worthless
/ breached / mark-unavailable and the formatter.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trademaster.reporting import build_condor_outcome, format_condor_summary

# #87-shaped condor: short 739P/748C, wings 734/753, $44 credit, $453 max loss.
_LEGS = {
    "short_put": "SPY260630P00739000", "long_put": "SPY260630P00734000",
    "short_call": "SPY260630C00748000", "long_call": "SPY260630C00753000",
    "max_loss_per_contract": "453.000",
}


def _condor(**over):
    base = {"id": 87, "credit": 44.0, "qty": 1, "opened_at": datetime(2026, 6, 30, 14, tzinfo=UTC),
            "closed_at": None, "realized_pnl_usd": None, "exit_reason": None, "expiry": "2026-06-30",
            **_LEGS}
    base.update(over)
    return base


def test_closed_condor_uses_booked_pnl():
    o = build_condor_outcome(
        _condor(closed_at=datetime(2026, 6, 30, 20, tzinfo=UTC),
                realized_pnl_usd=44.0, exit_reason="expired_full_credit"), spot=744.0)
    assert o["projected"] is False
    assert o["realized"] == 44.0
    assert "closed" in o["status"]


def test_open_worthless_projects_full_credit():
    # SPY 744 sits inside 739–748 → both spreads expire worthless → keep $44.
    o = build_condor_outcome(_condor(), spot=744.0)
    assert o["projected"] is True
    assert o["realized"] == 44.0
    assert "worthless" in o["status"] and "books next AM" in o["status"]


def test_open_breached_projects_capped_loss():
    # SPY 730 below the long put 734 → full $5 wing = $500/ct, capped at $453 max loss.
    o = build_condor_outcome(_condor(), spot=730.0)
    assert o["projected"] is True
    assert o["realized"] == 44.0 - 453.0  # credit − capped debit
    assert "breached" in o["status"]


def test_open_no_spot_is_mark_unavailable():
    o = build_condor_outcome(_condor(), spot=None)
    assert o["realized"] is None
    assert "unavailable" in o["status"]


def test_format_condor_summary():
    out = [
        build_condor_outcome(_condor(), spot=744.0),  # projected +44
    ]
    msg = format_condor_summary(out, period="2026-06-30")
    assert "Iron Condor" in msg
    assert "+44" in msg              # net line
    assert "books next AM" in msg
    assert "Projected" in msg        # the projected footnote


def test_format_condor_summary_empty():
    assert format_condor_summary([], period="2026-06-30") == ""
