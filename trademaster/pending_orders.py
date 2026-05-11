"""Pending-order helpers — serialize plans, look up by id, mark decided.

Phase 2.3c uses this for the live-mode `/approve` flow. The strategist
risk-approves a plan but does NOT submit it; it creates a PendingOrder
row with the plan serialized as JSON, plus a 15-minute expiry.

Discord `/approve N` looks up PendingOrder N, reconstructs the leg specs
from the JSON, and asks the executor to submit. `/reject N` short-circuits.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from strategies.spy_0dte_iron_condor import IronCondorPlan

DEFAULT_EXPIRY_MINUTES = 15


def _as_aware_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on read even with DateTime(timezone=True). Re-add UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ----------------- plan ↔ JSON -----------------


def iron_condor_plan_to_dict(plan: IronCondorPlan) -> dict:
    """Serialize an IronCondorPlan into a JSON-safe dict.

    We keep the OCC symbols (what the executor needs), strikes, expiry, and
    qty/credit/max-loss math. Greeks and live bid/ask quotes are not stored
    — by the time /approve fires the live quotes will have moved anyway,
    and the order is a limit on net credit, not on individual legs.
    """
    return {
        "schema": "iron_condor_v1",
        "underlying": plan.short_put.underlying,
        "expiry": plan.short_put.expiry.isoformat(),
        "qty": plan.qty,
        "wing_width": str(plan.wing_width),
        "credit_per_contract": str(plan.credit_per_contract),
        "max_loss_per_contract": str(plan.max_loss_per_contract),
        "short_put": {
            "occ_symbol": plan.short_put.occ_symbol,
            "strike": str(plan.short_put.strike),
        },
        "long_put": {
            "occ_symbol": plan.long_put.occ_symbol,
            "strike": str(plan.long_put.strike),
        },
        "short_call": {
            "occ_symbol": plan.short_call.occ_symbol,
            "strike": str(plan.short_call.strike),
        },
        "long_call": {
            "occ_symbol": plan.long_call.occ_symbol,
            "strike": str(plan.long_call.strike),
        },
    }


# ----------------- CRUD-ish helpers -----------------


def create_pending(
    session: Session,
    *,
    signal_id: int | None,
    strategy: str,
    plan: dict,
    summary: str,
    expires_in_minutes: int = DEFAULT_EXPIRY_MINUTES,
    now: datetime | None = None,
) -> int:
    """Insert a pending row and return its id."""
    from trademaster.db import PendingOrder

    now = now or datetime.now(UTC)
    row = PendingOrder(
        signal_id=signal_id,
        created_at=now,
        expires_at=now + timedelta(minutes=expires_in_minutes),
        strategy=strategy,
        plan=plan,
        summary=summary,
        status="pending",
    )
    session.add(row)
    session.commit()
    return int(row.id)


def get_pending(session: Session, pending_id: int):
    """Fetch by id. Returns the PendingOrder row or None."""
    from trademaster.db import PendingOrder

    return session.get(PendingOrder, pending_id)


def list_pending(session: Session, *, now: datetime | None = None):
    """Return all unfinished pending orders, oldest first.

    Marks orders past expiry as 'expired' as a side effect — keeps the
    visible list honest without a separate sweep job.
    """
    from trademaster.db import PendingOrder

    now = now or datetime.now(UTC)
    stmt = (
        select(PendingOrder)
        .where(PendingOrder.status == "pending")
        .order_by(PendingOrder.created_at.asc())
    )
    rows = list(session.execute(stmt).scalars())
    out = []
    for r in rows:
        if _as_aware_utc(r.expires_at) <= now:
            r.status = "expired"
            r.decided_at = now
            session.commit()
            continue
        out.append(r)
    return out


def mark_rejected(
    session: Session,
    pending_id: int,
    *,
    decided_by: str,
    now: datetime | None = None,
) -> bool:
    """Mark a pending order as rejected. Returns True on success."""
    from trademaster.db import PendingOrder

    row = session.get(PendingOrder, pending_id)
    if row is None or row.status != "pending":
        return False
    row.status = "rejected"
    row.decided_at = now or datetime.now(UTC)
    row.decided_by = decided_by
    session.commit()
    return True


def mark_approved(
    session: Session,
    pending_id: int,
    *,
    decided_by: str,
    alpaca_order_id: str | None,
    trade_id: int | None,
    error: str | None,
    now: datetime | None = None,
) -> None:
    """Mark a pending order as approved, recording the outcome."""
    from trademaster.db import PendingOrder

    row = session.get(PendingOrder, pending_id)
    if row is None:
        return
    row.status = "approved"
    row.decided_at = now or datetime.now(UTC)
    row.decided_by = decided_by
    row.alpaca_order_id = alpaca_order_id
    row.trade_id = trade_id
    row.error = error
    session.commit()


def plan_legs_for_submission(plan_dict: dict) -> dict:
    """Extract the four OCC symbols + qty + credit from a stored plan dict.

    Returned shape matches `alpaca_client.submit_iron_condor_entry` kwargs.
    """
    return {
        "qty": int(plan_dict["qty"]),
        "limit_credit_per_contract": Decimal(plan_dict["credit_per_contract"]),
        "short_put": plan_dict["short_put"]["occ_symbol"],
        "long_put": plan_dict["long_put"]["occ_symbol"],
        "short_call": plan_dict["short_call"]["occ_symbol"],
        "long_call": plan_dict["long_call"]["occ_symbol"],
    }
