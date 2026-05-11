"""One-off forced paper iron-condor for end-to-end sanity testing.

Bypasses the strategist's DeepSeek HOLD decision (low R/R today) but runs
the order through the real risk manager and submits to Alpaca paper. The
already-running daemon's exit monitor will pick up the resulting Trade row
on its 5-min cycle and manage it through to PT / stop / 15:50 force-close.

Do NOT use this in normal operation — the strategist's HOLD is correct
behavior 99% of the time. This is a sanity test of the execution path.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal

from agents.options.executor import execute_iron_condor
from agents.options.strategist import (
    CHAIN_HALF_WIDTH,
    UNDERLYING,
    _enrich_chain_with_bs_greeks,
    format_manual_signal,
)
from integrations import alpaca_client
from strategies.spy_0dte_iron_condor import build_iron_condor
from trademaster import risk_manager
from trademaster.db import Signal as SignalRow
from trademaster.db import init_db, make_session_factory
from trademaster.logging import configure_logging, get_logger
from trademaster.models import Signal, SignalAction
from trademaster.risk_manager import RiskRejectionError
from trademaster.router import TaskType

log = get_logger(__name__)


async def main() -> int:
    configure_logging()
    init_db()
    factory = make_session_factory()
    now = datetime.now(UTC)

    print("--- 1. Fetching SPY quote ---", flush=True)
    spy = await alpaca_client.get_latest_stock_quote(UNDERLYING)
    spy_mid = spy.mid if spy.mid > 0 else (spy.bid or spy.ask)
    print(f"  SPY mid: ${spy_mid}", flush=True)

    print("--- 2. Fetching options chain + enriching greeks via BS ---", flush=True)
    chain = await alpaca_client.get_options_chain(
        UNDERLYING,
        expiry=now.date(),
        strike_lo=spy_mid - CHAIN_HALF_WIDTH,
        strike_hi=spy_mid + CHAIN_HALF_WIDTH,
    )
    chain = _enrich_chain_with_bs_greeks(chain, spot=spy_mid, now=now)
    print(f"  Chain: {len(chain)} contracts", flush=True)

    print("--- 3. Building iron condor ---", flush=True)
    plan = build_iron_condor(chain, qty=1, wing_width=Decimal("5"))
    print(
        f"  Short put ${plan.short_put.strike} · Long put ${plan.long_put.strike}\n"
        f"  Short call ${plan.short_call.strike} · Long call ${plan.long_call.strike}\n"
        f"  Credit: ${plan.credit_per_contract}/contract · "
        f"Max loss: ${plan.max_loss_per_contract}/contract",
        flush=True,
    )

    print("--- 4. Persisting Signal (forced OPEN) ---", flush=True)
    open_signal = Signal(
        task_type=TaskType.OPTIONS_STRATEGY.value,
        agent="options_forced_test",
        action=SignalAction.OPEN,
        symbol=UNDERLYING,
        confidence=1.0,
        reasoning="FORCED TEST TRADE — sanity check of execution path",
        order=plan.to_trade_order(),
        extra={
            "forced_test": True,
            "credit_per_contract": str(plan.credit_per_contract),
            "max_loss_per_contract": str(plan.max_loss_per_contract),
        },
    )
    with factory() as session:
        row = SignalRow(
            task_type=open_signal.task_type,
            agent=open_signal.agent,
            action=open_signal.action.value,
            symbol=open_signal.symbol,
            confidence=open_signal.confidence,
            reasoning=open_signal.reasoning,
            payload=open_signal.extra,
            accepted=None,
        )
        session.add(row)
        session.commit()
        signal_id = int(row.id)
    print(f"  Signal #{signal_id} persisted", flush=True)

    print("--- 5. Risk manager validation ---", flush=True)
    try:
        await risk_manager.validate_signal(
            open_signal, signal_id=signal_id, session_factory=factory,
        )
        print("  ✓ APPROVED", flush=True)
    except RiskRejectionError as e:
        print(f"  ✗ REJECTED: {e}", flush=True)
        return 1

    print("--- 6. Submitting multi-leg order to Alpaca paper ---", flush=True)
    signals_text = format_manual_signal(plan, open_signal)
    execution = await execute_iron_condor(
        plan,
        session_factory=factory,
        summary=signals_text,
        signal_id=signal_id,
    )
    print(
        f"  executed={execution.executed}\n"
        f"  reason={execution.reason}\n"
        f"  trade_id={execution.trade_id}",
        flush=True,
    )
    if execution.order:
        print(
            f"  order_id={execution.order.order_id}\n"
            f"  status={execution.order.status}\n"
            f"  filled_avg_price={execution.order.filled_avg_price}",
            flush=True,
        )

    if execution.executed:
        print(
            "\n✓ Trade is live in Alpaca paper. The running daemon's exit "
            "monitor (every 5 min) will pick it up automatically and manage "
            "exits per 50% PT / 2× stop / 15:50 ET force-close rules.",
            flush=True,
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
