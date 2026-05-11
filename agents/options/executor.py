"""Iron-condor execution.

Two paths:

- **Paper mode** (`TRADING_MODE=paper`): the strategist's approved plan
  is auto-submitted via `execute_iron_condor`. Persists a `trades` row
  on fill.
- **Live mode** (`TRADING_MODE=live`): `execute_iron_condor` does NOT
  submit. Instead it creates a `pending_orders` row (D-014). The user
  runs `/approve <id>` in Discord, which calls `execute_approved_pending`
  to do the actual submission and persistence.

The fill → Trade-row code path is shared so manual approval and paper
auto-execute produce identical accounting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import OrderResult
from strategies.spy_0dte_iron_condor import IronCondorPlan
from trademaster import pending_orders
from trademaster.config import get_settings
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger

log = get_logger(__name__)


class ExecutionResult:
    """Outcome of an execution attempt."""

    def __init__(
        self,
        *,
        executed: bool,
        order: OrderResult | None,
        trade_id: int | None,
        reason: str,
        pending_id: int | None = None,
    ) -> None:
        self.executed = executed
        self.order = order
        self.trade_id = trade_id
        self.reason = reason
        self.pending_id = pending_id


def _net_credit_per_contract_at_fill(
    plan_credit_per_contract: Decimal, filled_avg_price: Decimal | None
) -> Decimal:
    """Convert Alpaca's per-share fill price back to per-contract credit ($).

    Alpaca returns a NEGATIVE per-share price for credit fills (e.g. -0.18
    for an $18-per-contract credit) since the order is "buy" the spread at
    a negative net price. We always want the positive credit value here.
    """
    if filled_avg_price is None:
        return plan_credit_per_contract
    return abs(filled_avg_price * Decimal("100")).quantize(Decimal("0.01"))


def _persist_trade(
    session: Session,
    *,
    underlying: str,
    qty: int,
    strategy: str,
    expiry_iso: str,
    wing_width: str,
    short_put_occ: str,
    long_put_occ: str,
    short_call_occ: str,
    long_call_occ: str,
    plan_credit_per_contract: Decimal,
    plan_max_loss_per_contract: Decimal,
    credit_per_contract: Decimal,
    order: OrderResult,
) -> int:
    """Persist a `trades` row. Returns the row id."""
    row = Trade(
        symbol=underlying,
        asset_class="option",
        side="sell",
        strategy=strategy,
        qty=Decimal(qty),
        entry_price=credit_per_contract,
        alpaca_order_id=order.order_id,
        opened_at=datetime.now(UTC),
        extra={
            "structure": "iron_condor",
            "short_put": short_put_occ,
            "long_put": long_put_occ,
            "short_call": short_call_occ,
            "long_call": long_call_occ,
            "wing_width": wing_width,
            "max_loss_per_contract": str(plan_max_loss_per_contract),
            "credit_per_contract": str(credit_per_contract),
            "expected_credit_per_contract": str(plan_credit_per_contract),
            "expiry": expiry_iso,
            "filled_avg_price_per_share": (
                str(order.filled_avg_price) if order.filled_avg_price else None
            ),
        },
    )
    session.add(row)
    session.commit()
    return int(row.id)


async def _submit_and_persist(
    *,
    factory: Callable[[], Session],
    submitter: Callable[..., object],
    waiter: Callable[..., object],
    fill_timeout_s: float,
    underlying: str,
    qty: int,
    strategy: str,
    expiry_iso: str,
    wing_width: str,
    short_put_occ: str,
    long_put_occ: str,
    short_call_occ: str,
    long_call_occ: str,
    plan_credit_per_contract: Decimal,
    plan_max_loss_per_contract: Decimal,
) -> ExecutionResult:
    """Submit a 4-leg credit order and persist the Trade row on fill.

    Shared between paper auto-execute and live /approve flows so they
    produce identical accounting.
    """
    order = await submitter(
        qty=qty,
        limit_credit_per_contract=plan_credit_per_contract,
        short_put=short_put_occ,
        long_put=long_put_occ,
        short_call=short_call_occ,
        long_call=long_call_occ,
    )
    final = await waiter(order.order_id, timeout_s=fill_timeout_s)
    log.info(
        "iron_condor_execute_terminal",
        order_id=final.order_id,
        status=final.status,
        filled_qty=str(final.filled_qty),
    )

    if final.status != "filled":
        return ExecutionResult(
            executed=False,
            order=final,
            trade_id=None,
            reason=f"order ended in status={final.status}",
        )

    credit = _net_credit_per_contract_at_fill(
        plan_credit_per_contract, final.filled_avg_price
    )
    with factory() as session:
        trade_id = _persist_trade(
            session,
            underlying=underlying,
            qty=qty,
            strategy=strategy,
            expiry_iso=expiry_iso,
            wing_width=wing_width,
            short_put_occ=short_put_occ,
            long_put_occ=long_put_occ,
            short_call_occ=short_call_occ,
            long_call_occ=long_call_occ,
            plan_credit_per_contract=plan_credit_per_contract,
            plan_max_loss_per_contract=plan_max_loss_per_contract,
            credit_per_contract=credit,
            order=final,
        )
    return ExecutionResult(
        executed=True,
        order=final,
        trade_id=trade_id,
        reason=f"filled at ${credit}/contract",
    )


# ----------------- public entry points -----------------


async def execute_iron_condor(
    plan: IronCondorPlan,
    *,
    session_factory: Callable[[], Session] | None = None,
    submitter: Callable[..., object] = alpaca_client.submit_iron_condor_entry,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 120.0,
    summary: str | None = None,
    signal_id: int | None = None,
) -> ExecutionResult:
    """Paper: submit + persist. Live: create a pending_orders row (D-014).

    `summary` is the human-readable manual signal text — stored on the
    pending row so the user can re-read it later via `/pending`.
    """
    factory = session_factory or make_session_factory()
    settings = get_settings()

    if settings.trading_mode == "live":
        plan_dict = pending_orders.iron_condor_plan_to_dict(plan)
        with factory() as session:
            pending_id = pending_orders.create_pending(
                session,
                signal_id=signal_id,
                strategy="spy_0dte_ic",
                plan=plan_dict,
                summary=summary or "iron condor (no summary supplied)",
            )
        log.info("iron_condor_pending_created", pending_id=pending_id)
        return ExecutionResult(
            executed=False,
            order=None,
            trade_id=None,
            reason=(
                f"awaiting /approve {pending_id} (expires in "
                f"{pending_orders.DEFAULT_EXPIRY_MINUTES} min)"
            ),
            pending_id=pending_id,
        )

    return await _submit_and_persist(
        factory=factory,
        submitter=submitter,
        waiter=waiter,
        fill_timeout_s=fill_timeout_s,
        underlying=plan.short_put.underlying,
        qty=plan.qty,
        strategy="spy_0dte_ic",
        expiry_iso=plan.short_put.expiry.isoformat(),
        wing_width=str(plan.wing_width),
        short_put_occ=plan.short_put.occ_symbol,
        long_put_occ=plan.long_put.occ_symbol,
        short_call_occ=plan.short_call.occ_symbol,
        long_call_occ=plan.long_call.occ_symbol,
        plan_credit_per_contract=plan.credit_per_contract,
        plan_max_loss_per_contract=plan.max_loss_per_contract,
    )


async def execute_approved_pending(
    pending_id: int,
    *,
    decided_by: str,
    session_factory: Callable[[], Session] | None = None,
    submitter: Callable[..., object] = alpaca_client.submit_iron_condor_entry,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 120.0,
) -> ExecutionResult:
    """Reconstruct + submit a pending iron-condor plan after /approve.

    Re-checks status (pending and not expired) before submitting. Records
    the outcome back onto the pending row (alpaca_order_id, trade_id, error).
    """
    factory = session_factory or make_session_factory()

    with factory() as session:
        row = pending_orders.get_pending(session, pending_id)
        if row is None:
            return ExecutionResult(
                executed=False, order=None, trade_id=None,
                reason=f"pending #{pending_id} not found",
                pending_id=pending_id,
            )
        if row.status != "pending":
            return ExecutionResult(
                executed=False, order=None, trade_id=None,
                reason=f"pending #{pending_id} is `{row.status}`, not pending",
                pending_id=pending_id,
            )
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            row.status = "expired"
            row.decided_at = datetime.now(UTC)
            session.commit()
            return ExecutionResult(
                executed=False, order=None, trade_id=None,
                reason=f"pending #{pending_id} expired",
                pending_id=pending_id,
            )
        plan_dict = dict(row.plan)
        strategy = row.strategy

    legs = pending_orders.plan_legs_for_submission(plan_dict)

    result = await _submit_and_persist(
        factory=factory,
        submitter=submitter,
        waiter=waiter,
        fill_timeout_s=fill_timeout_s,
        underlying=plan_dict["underlying"],
        qty=legs["qty"],
        strategy=strategy,
        expiry_iso=plan_dict["expiry"],
        wing_width=plan_dict["wing_width"],
        short_put_occ=legs["short_put"],
        long_put_occ=legs["long_put"],
        short_call_occ=legs["short_call"],
        long_call_occ=legs["long_call"],
        plan_credit_per_contract=legs["limit_credit_per_contract"],
        plan_max_loss_per_contract=Decimal(plan_dict["max_loss_per_contract"]),
    )

    with factory() as session:
        pending_orders.mark_approved(
            session,
            pending_id,
            decided_by=decided_by,
            alpaca_order_id=result.order.order_id if result.order else None,
            trade_id=result.trade_id,
            error=None if result.executed else result.reason,
        )

    # Pass through with pending_id annotated for the caller.
    return ExecutionResult(
        executed=result.executed,
        order=result.order,
        trade_id=result.trade_id,
        reason=result.reason,
        pending_id=pending_id,
    )
