"""Iron-condor execution.

Phase 2.3a: paper-mode auto-execution. Strategist hands us an approved
plan; we submit a 4-leg credit order, wait for it to fill, and persist a
single `trades` row with the leg breakdown in `extra`.

Live-mode (TRADING_MODE=live) does NOT auto-execute here — Phase 2.3c
will add the Discord /approve flow. Until then, live mode short-circuits
and posts a placeholder.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from integrations import alpaca_client
from integrations.alpaca_client import OrderResult
from strategies.spy_0dte_iron_condor import IronCondorPlan
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
    ) -> None:
        self.executed = executed
        self.order = order
        self.trade_id = trade_id
        self.reason = reason


def _net_credit_per_contract_at_fill(
    plan: IronCondorPlan, filled_avg_price: Decimal | None
) -> Decimal:
    """Convert Alpaca's per-share fill price back to per-contract credit ($)."""
    if filled_avg_price is None:
        return plan.credit_per_contract
    return (filled_avg_price * Decimal("100")).quantize(Decimal("0.01"))


def _persist_trade(
    factory: Callable[[], Session],
    *,
    plan: IronCondorPlan,
    order: OrderResult,
    credit_per_contract: Decimal,
) -> int:
    """Persist a `trades` row for the iron condor. Returns the row id."""
    with factory() as session:
        row = Trade(
            symbol=plan.short_put.underlying,
            asset_class="option",
            side="sell",
            strategy="spy_0dte_ic",
            qty=Decimal(plan.qty),
            entry_price=credit_per_contract,
            alpaca_order_id=order.order_id,
            opened_at=datetime.now(UTC),
            extra={
                "structure": "iron_condor",
                "short_put": plan.short_put.occ_symbol,
                "long_put": plan.long_put.occ_symbol,
                "short_call": plan.short_call.occ_symbol,
                "long_call": plan.long_call.occ_symbol,
                "wing_width": str(plan.wing_width),
                "max_loss_per_contract": str(plan.max_loss_per_contract),
                "credit_per_contract": str(credit_per_contract),
                "expiry": plan.short_put.expiry.isoformat(),
                "filled_avg_price_per_share": (
                    str(order.filled_avg_price) if order.filled_avg_price else None
                ),
            },
        )
        session.add(row)
        session.commit()
        return int(row.id)


async def execute_iron_condor(
    plan: IronCondorPlan,
    *,
    session_factory: Callable[[], Session] | None = None,
    submitter: Callable[..., object] = alpaca_client.submit_iron_condor_entry,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 120.0,
) -> ExecutionResult:
    """Submit the iron-condor open order and wait for it to fill.

    Paper mode (TRADING_MODE=paper): always attempts execution.
    Live mode: refuses for now — approval flow lands in Phase 2.3c.
    """
    factory = session_factory or make_session_factory()
    settings = get_settings()

    if settings.trading_mode == "live":
        return ExecutionResult(
            executed=False,
            order=None,
            trade_id=None,
            reason="live mode: order execution requires /approve (Phase 2.3c)",
        )

    order = await submitter(
        qty=plan.qty,
        limit_credit_per_contract=plan.credit_per_contract,
        short_put=plan.short_put.occ_symbol,
        long_put=plan.long_put.occ_symbol,
        short_call=plan.short_call.occ_symbol,
        long_call=plan.long_call.occ_symbol,
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

    credit = _net_credit_per_contract_at_fill(plan, final.filled_avg_price)
    trade_id = _persist_trade(factory, plan=plan, order=final, credit_per_contract=credit)
    return ExecutionResult(
        executed=True,
        order=final,
        trade_id=trade_id,
        reason=f"filled at ${credit}/contract",
    )
