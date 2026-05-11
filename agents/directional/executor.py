"""Directional single-leg options executor.

On a BUY_CALL or BUY_PUT signal:
  1. Check max concurrent directional positions haven't been reached.
  2. Build OCC symbol from ticker/strike/expiry.
  3. Fetch current ask for sizing + limit-price.
  4. Submit limit buy-to-open.
  5. Wait for fill; persist Trade row with PT/SL targets in extra.
  6. Return result including formatted trade text for #trades.

No /approve gate — directional size is small and signals are time-sensitive.
Both paper and live modes execute immediately.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.directional.intraday import TickerDecision
from integrations import alpaca_client
from integrations.alpaca_client import (
    OrderResult,
    build_occ_symbol,
    get_single_option_quote,
)
from trademaster.config import get_settings
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger

log = get_logger(__name__)

STRATEGY_CALL = "directional_call"
STRATEGY_PUT = "directional_put"

# Fraction of trading_capital_usd allocated per trade by mode.
_SIZE_FRACTION = {"aggressive": 0.15, "selective": 0.03}
# PT and SL pct by mode (mirrors _MODE_CONFIG in intraday.py).
_EXIT_PCT = {
    "aggressive": {"pt": Decimal("1.0"), "sl": Decimal("0.5")},
    "selective": {"pt": Decimal("0.5"), "sl": Decimal("0.3")},
}


class DirectionalExecutionResult:
    def __init__(
        self,
        *,
        executed: bool,
        order: OrderResult | None,
        trade_id: int | None,
        reason: str,
        trade_text: str | None = None,
    ) -> None:
        self.executed = executed
        self.order = order
        self.trade_id = trade_id
        self.reason = reason
        self.trade_text = trade_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_directional_count(session: Session) -> int:
    stmt = select(Trade).where(
        Trade.strategy.in_([STRATEGY_CALL, STRATEGY_PUT]),
        Trade.closed_at.is_(None),
    )
    return len(list(session.execute(stmt).scalars()))


def _resolve_expiry(expiry_str: str, today: date) -> date:
    if expiry_str == "0DTE":
        return today
    days = (4 - today.weekday()) % 7
    if days == 0:
        days = 7
    return today + timedelta(days=days)


def _persist_entry(
    session: Session,
    *,
    ticker: str,
    occ: str,
    action: str,
    qty: int,
    entry_premium: Decimal,
    profit_target_premium: Decimal,
    stop_premium: Decimal,
    mode: str,
    order: OrderResult,
) -> int:
    strategy = STRATEGY_CALL if action == "BUY_CALL" else STRATEGY_PUT
    row = Trade(
        symbol=occ,
        asset_class="option",
        side="buy",
        strategy=strategy,
        qty=Decimal(qty),
        entry_price=entry_premium,
        alpaca_order_id=order.order_id,
        opened_at=datetime.now(UTC),
        extra={
            "ticker": ticker,
            "action": action,
            "occ_symbol": occ,
            "mode": mode,
            "profit_target_premium": str(profit_target_premium),
            "stop_premium": str(stop_premium),
            "fill_status": order.status,
            "filled_avg_price": (
                str(order.filled_avg_price) if order.filled_avg_price else None
            ),
        },
    )
    session.add(row)
    session.commit()
    return int(row.id)


def _format_trade_text(
    decision: TickerDecision,
    *,
    trade_id: int,
    qty: int,
    occ: str,
    entry_premium: Decimal,
    profit_target_premium: Decimal,
    stop_premium: Decimal,
    mode: str,
) -> str:
    action_word = "Bought CALL" if decision.action == "BUY_CALL" else "Bought PUT"
    total_cost = (entry_premium * 100 * qty).quantize(Decimal("0.01"))
    return (
        f"🤖 **Directional executed — trade #{trade_id}** [{mode.upper()}]\n"
        f"{action_word} **{qty}× {occ}** at **${entry_premium}/share** "
        f"(${total_cost} total)\n"
        f"PT: ≥${profit_target_premium}/share · Stop: ≤${stop_premium}/share"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_directional_signal(
    decision: TickerDecision,
    *,
    today: date | None = None,
    mode: str | None = None,
    session_factory: Callable[[], Session] | None = None,
    quote_fetcher: Callable[..., object] = get_single_option_quote,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_buy,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 90.0,
) -> DirectionalExecutionResult:
    """Execute a BUY_CALL or BUY_PUT signal. Returns immediately on any skip."""
    if decision.action not in ("BUY_CALL", "BUY_PUT"):
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None, reason="HOLD — nothing to execute"
        )
    if decision.strike is None or decision.expiry is None:
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None, reason="missing strike or expiry"
        )

    factory = session_factory or make_session_factory()
    settings = get_settings()
    mode = mode or settings.directional_mode
    today = today or datetime.now(UTC).date()

    with factory() as session:
        n_open = _open_directional_count(session)
    max_concurrent = settings.directional_max_concurrent
    if n_open >= max_concurrent:
        log.info(
            "directional_execute_skipped_max_concurrent",
            n_open=n_open,
            max=max_concurrent,
        )
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None,
            reason=f"max_concurrent={max_concurrent} already open",
        )

    option_type = "call" if decision.action == "BUY_CALL" else "put"
    expiry_date = _resolve_expiry(decision.expiry, today)
    occ = build_occ_symbol(decision.ticker, expiry_date, option_type, decision.strike)

    quote = await quote_fetcher(occ)
    if quote is None or quote.ask <= 0:
        log.warning("directional_execute_no_quote", occ=occ)
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None, reason=f"no live quote for {occ}"
        )

    exit_pcts = _EXIT_PCT.get(mode, _EXIT_PCT["selective"])
    size_frac = _SIZE_FRACTION.get(mode, _SIZE_FRACTION["selective"])
    position_usd = float(settings.trading_capital_usd) * size_frac
    qty = max(1, math.floor(position_usd / (float(quote.ask) * 100)))

    entry_premium = quote.ask
    profit_target_premium = (
        entry_premium * (Decimal("1") + exit_pcts["pt"])
    ).quantize(Decimal("0.0001"))
    stop_premium = (
        entry_premium * (Decimal("1") - exit_pcts["sl"])
    ).quantize(Decimal("0.0001"))

    order = await submitter(qty=qty, occ_symbol=occ, limit_price=entry_premium)
    final = await waiter(order.order_id, timeout_s=fill_timeout_s)

    log.info(
        "directional_execute_terminal",
        occ=occ,
        qty=qty,
        order_id=final.order_id,
        status=final.status,
    )

    if final.status != "filled":
        return DirectionalExecutionResult(
            executed=False, order=final, trade_id=None,
            reason=f"order ended with status={final.status}",
        )

    filled_premium = (
        final.filled_avg_price if final.filled_avg_price is not None else entry_premium
    )
    with factory() as session:
        trade_id = _persist_entry(
            session,
            ticker=decision.ticker,
            occ=occ,
            action=decision.action,
            qty=qty,
            entry_premium=filled_premium,
            profit_target_premium=profit_target_premium,
            stop_premium=stop_premium,
            mode=mode,
            order=final,
        )

    trade_text = _format_trade_text(
        decision,
        trade_id=trade_id,
        qty=qty,
        occ=occ,
        entry_premium=filled_premium,
        profit_target_premium=profit_target_premium,
        stop_premium=stop_premium,
        mode=mode,
    )
    return DirectionalExecutionResult(
        executed=True,
        order=final,
        trade_id=trade_id,
        reason=f"filled {qty}× {occ} at ${filled_premium}",
        trade_text=trade_text,
    )
