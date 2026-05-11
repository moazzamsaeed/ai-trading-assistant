"""Pending-orders persistence + Discord command tests.

Covers the full live-mode lifecycle: pending creation, listing,
expiry, /approve auto-execute + Trade row, /reject discard.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from agents.options import executor as ex
from agents.options.executor import execute_approved_pending, execute_iron_condor
from integrations import discord_commands as cmds
from integrations.alpaca_client import OptionQuote, OrderResult
from strategies.spy_0dte_iron_condor import IronCondorPlan
from trademaster import pending_orders as po
from trademaster.db import (
    Base,
    PendingOrder,
    Trade,
    make_engine,
    make_session_factory,
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _q(occ: str, strike: int, kind: str) -> OptionQuote:
    return OptionQuote(
        occ_symbol=occ,
        underlying="SPY",
        strike=Decimal(strike),
        expiry=date(2026, 5, 11),
        option_type=kind,
        bid=Decimal("0.60"),
        ask=Decimal("0.65"),
        mid=Decimal("0.625"),
        delta=Decimal("0.16"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.1"),
        implied_volatility=Decimal("0.2"),
    )


def _plan(qty: int = 1) -> IronCondorPlan:
    return IronCondorPlan(
        short_put=_q("SPY260511P00495000", 495, "put"),
        long_put=_q("SPY260511P00490000", 490, "put"),
        short_call=_q("SPY260511C00505000", 505, "call"),
        long_call=_q("SPY260511C00510000", 510, "call"),
        qty=qty,
        credit_per_contract=Decimal("80.00"),
        max_loss_per_contract=Decimal("420.00"),
        wing_width=Decimal("5"),
    )


def _live_settings(monkeypatch):
    class FakeSettings:
        trading_mode = "live"

    monkeypatch.setattr(ex, "get_settings", lambda: FakeSettings())


# ----------------- plan ↔ dict round-trip -----------------


def test_plan_to_dict_includes_all_legs():
    d = po.iron_condor_plan_to_dict(_plan(qty=3))
    assert d["schema"] == "iron_condor_v1"
    assert d["underlying"] == "SPY"
    assert d["qty"] == 3
    assert d["credit_per_contract"] == "80.00"
    assert d["short_put"]["occ_symbol"] == "SPY260511P00495000"
    assert d["long_call"]["occ_symbol"] == "SPY260511C00510000"


def test_legs_for_submission_matches_alpaca_kwargs():
    d = po.iron_condor_plan_to_dict(_plan())
    legs = po.plan_legs_for_submission(d)
    assert legs == {
        "qty": 1,
        "limit_credit_per_contract": Decimal("80.00"),
        "short_put": "SPY260511P00495000",
        "long_put": "SPY260511P00490000",
        "short_call": "SPY260511C00505000",
        "long_call": "SPY260511C00510000",
    }


# ----------------- live executor creates pending -----------------


async def test_live_executor_creates_pending_row(monkeypatch, session_factory):
    _live_settings(monkeypatch)

    result = await execute_iron_condor(
        _plan(),
        session_factory=session_factory,
        summary="📋 manual signal",
        signal_id=None,
    )
    assert result.executed is False
    assert result.pending_id is not None
    with session_factory() as s:
        row = s.get(PendingOrder, result.pending_id)
        assert row.status == "pending"
        # Expiry is 15 min from creation (default)
        assert row.expires_at > row.created_at
        assert (row.expires_at - row.created_at) <= timedelta(minutes=15, seconds=1)


# ----------------- /pending listing -----------------


async def test_pending_list_marks_expired(session_factory):
    with session_factory() as s:
        # Already-expired row
        old = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="old",
            now=datetime.now(UTC) - timedelta(minutes=30),
            expires_in_minutes=15,
        )
        # Fresh row
        fresh = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="fresh",
        )

    text = await cmds.pending(session_factory=session_factory)
    assert "fresh" in text
    assert f"#{fresh}" in text
    # Stale one shouldn't show up.
    assert "old" not in text

    with session_factory() as s:
        old_row = s.get(PendingOrder, old)
        assert old_row.status == "expired"


async def test_pending_list_empty(session_factory):
    text = await cmds.pending(session_factory=session_factory)
    assert text == "No pending trades."


# ----------------- /reject -----------------


async def test_reject_marks_rejected(session_factory):
    with session_factory() as s:
        pid = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="x",
        )

    msg = await cmds.reject(pid, user_label="moazzam", session_factory=session_factory)
    assert "Rejected" in msg
    with session_factory() as s:
        row = s.get(PendingOrder, pid)
        assert row.status == "rejected"
        assert row.decided_by == "moazzam"


async def test_reject_unknown_id(session_factory):
    msg = await cmds.reject(9999, user_label="x", session_factory=session_factory)
    assert "not found" in msg.lower()


# ----------------- /approve -----------------


def _fake_order(status: str = "filled", filled_avg: str = "0.80") -> OrderResult:
    return OrderResult(
        order_id="o1",
        status=status,
        filled_avg_price=Decimal(filled_avg) if filled_avg else None,
        filled_qty=Decimal("1"),
        submitted_at=datetime.now(UTC),
        raw_status=status,
    )


async def test_approve_submits_and_persists_trade(session_factory):
    with session_factory() as s:
        pid = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan(qty=2)),
            summary="x",
        )

    captured: dict = {}

    async def submitter(**kwargs):
        captured.update(kwargs)
        return _fake_order("new", filled_avg=None)

    async def waiter(_id, *, timeout_s):
        return _fake_order("filled", filled_avg="0.85")

    result = await execute_approved_pending(
        pid,
        decided_by="moazzam",
        session_factory=session_factory,
        submitter=submitter,
        waiter=waiter,
    )
    assert result.executed is True
    assert result.trade_id is not None
    assert captured["short_put"] == "SPY260511P00495000"
    assert captured["qty"] == 2
    assert captured["limit_credit_per_contract"] == Decimal("80.00")

    with session_factory() as s:
        row = s.get(PendingOrder, pid)
        assert row.status == "approved"
        assert row.decided_by == "moazzam"
        assert row.trade_id == result.trade_id
        trade = s.get(Trade, result.trade_id)
        assert trade.entry_price == Decimal("85.00")


async def test_approve_unknown_id(session_factory):
    result = await execute_approved_pending(
        99,
        decided_by="x",
        session_factory=session_factory,
        submitter=None,
        waiter=None,
    )
    assert result.executed is False
    assert "not found" in result.reason.lower()


async def test_approve_expired_marks_expired(session_factory):
    with session_factory() as s:
        pid = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="x",
            now=datetime.now(UTC) - timedelta(minutes=30),
            expires_in_minutes=15,
        )

    async def boom_submit(**_):
        raise AssertionError("must not submit when expired")

    result = await execute_approved_pending(
        pid,
        decided_by="x",
        session_factory=session_factory,
        submitter=boom_submit,
        waiter=boom_submit,
    )
    assert result.executed is False
    assert "expired" in result.reason.lower()
    with session_factory() as s:
        row = s.get(PendingOrder, pid)
        assert row.status == "expired"


async def test_approve_already_rejected_short_circuits(session_factory):
    with session_factory() as s:
        pid = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="x",
        )
        po.mark_rejected(s, pid, decided_by="moazzam")

    async def boom_submit(**_):
        raise AssertionError("must not submit when already rejected")

    result = await execute_approved_pending(
        pid,
        decided_by="moazzam",
        session_factory=session_factory,
        submitter=boom_submit,
        waiter=boom_submit,
    )
    assert result.executed is False
    assert "rejected" in result.reason.lower() or "not pending" in result.reason.lower()


async def test_approve_failed_submission_records_error(session_factory):
    with session_factory() as s:
        pid = po.create_pending(
            s,
            signal_id=None,
            strategy="spy_0dte_ic",
            plan=po.iron_condor_plan_to_dict(_plan()),
            summary="x",
        )

    async def submitter(**_):
        return _fake_order("new", filled_avg=None)

    async def waiter(_id, *, timeout_s):
        return _fake_order("rejected", filled_avg=None)

    result = await execute_approved_pending(
        pid,
        decided_by="moazzam",
        session_factory=session_factory,
        submitter=submitter,
        waiter=waiter,
    )
    assert result.executed is False

    with session_factory() as s:
        row = s.get(PendingOrder, pid)
        assert row.status == "approved"  # decision was approve, even though fill failed
        assert row.trade_id is None
        assert "rejected" in (row.error or "").lower()


# ----------------- discord_commands.approve / reject -----------------


async def test_cmd_approve_calls_executor(monkeypatch, session_factory):
    """The /approve command surface formats whatever the executor returns."""
    called: dict = {}

    async def fake_exec(pid, *, decided_by, session_factory):
        called["pid"] = pid
        called["decided_by"] = decided_by
        from agents.options.executor import ExecutionResult
        return ExecutionResult(
            executed=True, order=None, trade_id=42,
            reason="filled at $80/contract", pending_id=pid,
        )

    msg = await cmds.approve(
        5, user_label="moazzam#1234",
        session_factory=session_factory, executor=fake_exec,
    )
    assert "Approved" in msg
    assert "#5" in msg
    assert "#42" in msg
    assert called == {"pid": 5, "decided_by": "moazzam#1234"}


async def test_cmd_approve_handles_executor_exception(session_factory):
    async def boom(*_a, **_k):
        raise RuntimeError("alpaca 500")

    msg = await cmds.approve(
        1, user_label="moazzam",
        session_factory=session_factory, executor=boom,
    )
    assert "failed" in msg.lower()
    assert "alpaca 500" in msg
