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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from agents.directional.intraday import TickerDecision
from integrations import alpaca_client
from integrations.alpaca_client import (
    OrderResult,
)
from trademaster.config import get_settings
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger

log = get_logger(__name__)

STRATEGY_CALL = "directional_call"
STRATEGY_PUT = "directional_put"

# PT and SL pct by mode (mirrors _MODE_CONFIG in intraday.py).
# Position sizing: the scheduler passes the remaining exposure budget as
# capital_usd — the executor deploys it in full. The only limit is the
# max_total_exposure_pct cap enforced in the scheduler.
_EXIT_PCT = {
    "aggressive": {"pt": Decimal("1.0"), "sl": Decimal("0.5")},
    "selective": {"pt": Decimal("0.5"), "sl": Decimal("0.3")},
}

# Per-trade catastrophic-loss cap. When an option expires worthless the loss
# equals (qty × premium × 100), so capping the deployed amount = capping the
# realised loss. Introduced 2026-05-30 as the direct defense against the
# trade #37 pattern (56 contracts × $0.53 = $952 single-trade loss); the old
# defense was the $0.50 MIN_ASK floor, which bounded loss by proxy through
# premium and blocked too many valid setups. $500 chosen because 3 losing
# trades at full cap = $1500 = daily loss limit (15% of $10k), so the daily
# governor still catches catastrophic days even with looser entry filters.
MAX_LOSS_PER_TRADE_USD = Decimal("500")


class DirectionalExecutionResult:
    def __init__(
        self,
        *,
        executed: bool,
        order: OrderResult | None,
        trade_id: int | None,
        reason: str,
        trade_text: str | None = None,
        qty: int | None = None,
        occ: str | None = None,
        entry_premium: Decimal | None = None,
    ) -> None:
        self.executed = executed
        self.order = order
        self.trade_id = trade_id
        self.reason = reason
        self.trade_text = trade_text
        self.qty = qty
        self.occ = occ
        self.entry_premium = entry_premium


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# SPY has true daily 0DTE (Mon–Fri). QQQ and IWM only have 0DTE on Mon/Wed/Fri.
# Using today's date on an off-day produces an OCC symbol that doesn't exist.
_DAILY_OPTION_TICKERS = {"SPY"}
_MWF_OPTION_TICKERS = {"QQQ", "IWM"}  # 0DTE only on Mon(0)/Wed(2)/Fri(4)


def _next_friday(today: date) -> date:
    days = (4 - today.weekday()) % 7
    if days == 0:
        days = 7
    return today + timedelta(days=days)


def _resolve_expiry(expiry_str: str, today: date, ticker: str = "") -> date:
    t = ticker.upper()
    if expiry_str == "0DTE":
        if t in _DAILY_OPTION_TICKERS:
            return today
        if t in _MWF_OPTION_TICKERS and today.weekday() in (0, 2, 4):
            return today
    return _next_friday(today)


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
    conviction: str = "HIGH",
    order: OrderResult,
    entry_reasoning: str = "",
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
            "conviction": conviction,
            "original_qty": qty,
            "profit_target_premium": str(profit_target_premium),
            "stop_premium": str(stop_premium),
            # Initialized at 0 so losing trades (which never positively peak)
            # are distinguishable from trades where the trailing tick never ran.
            "peak_pnl_pct": 0.0,
            "entry_reasoning": entry_reasoning[:300],
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
# Unified chain-based strike selection — replaces all per-ticker fallbacks
# ---------------------------------------------------------------------------

@dataclass
class _SelectedStrike:
    strike: Decimal
    occ: str
    quote: object


async def select_best_strike(
    ticker: str,
    expiry_date: date,
    option_type: str,
    target_strike: float,
    budget: float,
) -> _SelectedStrike | None:
    """Fetch the real option chain and return the best available quoted strike.

    Always goes to the chain — never trusts the LLM's raw strike number.
    This handles all tickers uniformly: different strike increments ($1/$2.5/$5),
    missing strikes, and budget constraints are resolved in one pass.

    Selection logic:
    - Fetch strikes from $10 ITM to $30 OTM relative to target
    - Filter to those with a live ask quote within budget
    - Pick the one closest to target (ATM preference over deep OTM)
    """
    otm_dir = 1 if option_type == "call" else -1
    lo = Decimal(str(target_strike - 10))
    hi = Decimal(str(target_strike + otm_dir * 30))
    strike_lo, strike_hi = min(lo, hi), max(lo, hi)

    try:
        quotes = await alpaca_client.get_options_chain(
            ticker, expiry=expiry_date, strike_lo=strike_lo, strike_hi=strike_hi,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("directional_chain_fetch_failed", ticker=ticker, error=str(e))
        return None

    # Minimum $0.30/share ask ($30/contract). Lowered from $0.50 on 2026-05-30
    # because $0.50 blocked every BUY_PUT execute attempt on a falling SPY
    # session — near-the-money 0DTE puts are routinely quoted $0.20-$0.40.
    # The original $0.50 was a safety margin above what we believed Alpaca's
    # paper account would track reliably (the I3 ghost-position pattern); $0.30
    # tested as low enough to capture valid 0DTE OTM premiums while staying
    # above the suspected paper-tracking floor. Re-evaluate if ghost positions
    # reappear after this change. The catastrophic-loss defense from trade #37
    # moved to MAX_LOSS_PER_TRADE_USD in execute_directional_signal.
    MIN_ASK = Decimal("0.30")
    max_spread_pct = get_settings().max_bid_ask_spread_pct

    candidates = [
        q for q in quotes
        if q.option_type == option_type
        and q.ask is not None
        and q.ask >= MIN_ASK
        and float(q.ask) * 100 <= budget
        and q.mid > 0
        and float(q.spread / q.mid) <= max_spread_pct
    ]
    if not candidates:
        # Log whether the spread filter was the cause (vs. budget/min_ask)
        pre_spread = [
            q for q in quotes
            if q.option_type == option_type and q.ask is not None and q.ask >= MIN_ASK
            and float(q.ask) * 100 <= budget
        ]
        log.info(
            "directional_no_qualifying_strike",
            ticker=ticker,
            target_strike=target_strike,
            budget=budget,
            min_ask=float(MIN_ASK),
            max_spread_pct=max_spread_pct,
            spread_filtered_count=len(pre_spread),
        )
        return None

    best = min(candidates, key=lambda q: abs(float(q.strike) - target_strike))
    return _SelectedStrike(strike=best.strike, occ=best.occ_symbol, quote=best)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute_directional_signal(
    decision: TickerDecision,
    *,
    today: date | None = None,
    mode: str | None = None,
    capital_usd: Decimal | None = None,
    session_factory: Callable[[], Session] | None = None,
    strike_selector: Callable[..., object] = select_best_strike,
    submitter: Callable[..., object] = alpaca_client.submit_single_option_buy,
    seller: Callable[..., object] = alpaca_client.submit_single_option_sell,
    waiter: Callable[..., object] = alpaca_client.wait_for_order,
    fill_timeout_s: float = 10.0,
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

    # No count cap — concurrent positions are gated solely by the 20% capital
    # exposure cap enforced in the scheduler. Smaller positions → more allowed;
    # larger positions → fewer allowed. Risk is bounded by deployed dollars,
    # not arbitrary trade counts.

    option_type = "call" if decision.action == "BUY_CALL" else "put"
    expiry_date = _resolve_expiry(decision.expiry, today, decision.ticker)



    exit_pcts = _EXIT_PCT.get(mode, _EXIT_PCT["selective"])
    # capital_usd is the available exposure budget passed by the scheduler
    # (max_total_exposure - already_deployed). The full amount is deployed —
    # no per-trade fraction. The scheduler's exposure cap is the only limit.
    if capital_usd is None:
        from trademaster.capital import get_effective_capital
        capital_usd = await get_effective_capital(factory)
    if capital_usd <= Decimal("0"):
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None,
            reason="effective capital is $0 — no new positions",
        )
    position_usd = float(capital_usd)

    # Always select from the real chain — handles strike increments, missing
    # strikes, and budget constraints in one pass for every ticker.
    selected = await strike_selector(
        decision.ticker, expiry_date, option_type, decision.strike, position_usd,
    )
    if selected is None:
        log.info(
            "directional_execute_no_affordable_strike",
            ticker=decision.ticker,
            target_strike=decision.strike,
            budget=position_usd,
        )
        return DirectionalExecutionResult(
            executed=False, order=None, trade_id=None,
            reason=(
                f"no affordable quoted strike near ${decision.strike:.0f} "
                f"within ${position_usd:.0f} budget"
            ),
        )

    if float(selected.strike) != decision.strike:
        log.info(
            "directional_execute_strike_adjusted",
            llm_strike=decision.strike,
            actual_strike=float(selected.strike),
            occ=selected.occ,
        )

    occ = selected.occ
    quote = selected.quote
    one_contract_cost = float(quote.ask) * 100
    # Cap deployed amount at MAX_LOSS_PER_TRADE_USD so total qty × premium × 100
    # ≤ $500 — bounds the worst case if the option goes to zero.
    capped_position_usd = min(position_usd, float(MAX_LOSS_PER_TRADE_USD))
    qty = max(1, math.floor(capped_position_usd / one_contract_cost))
    if capped_position_usd < position_usd:
        log.info(
            "directional_execute_qty_capped_by_loss_cap",
            ticker=decision.ticker,
            budget=position_usd,
            cap=float(MAX_LOSS_PER_TRADE_USD),
            one_contract_cost=one_contract_cost,
            qty=qty,
        )

    entry_premium = quote.ask
    order = await submitter(qty=qty, occ_symbol=occ, limit_price=entry_premium)
    final = await waiter(order.order_id, timeout_s=fill_timeout_s)

    # Belt-and-suspenders: if not terminal after timeout, explicitly cancel so
    # the order doesn't linger in Alpaca as a dangling "new" order all day.
    _terminal = {"filled", "cancelled", "canceled", "expired", "rejected", "done_for_day"}
    if final.status not in _terminal:
        await alpaca_client.cancel_order(order.order_id)
        log.info("directional_execute_order_cancelled", occ=occ, status=final.status)

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
    # Compute PT/SL from actual fill price, not the pre-order ask.
    profit_target_premium = (
        filled_premium * (Decimal("1") + exit_pcts["pt"])
    ).quantize(Decimal("0.0001"))
    stop_premium = (
        filled_premium * (Decimal("1") - exit_pcts["sl"])
    ).quantize(Decimal("0.0001"))

    # ---- Post-fill position verification ----
    # Alpaca paper sometimes fills an option order but never registers it as a
    # position (ghost position). Verify immediately after fill so we can bail
    # out before the trade ages and accrues premium decay losses.
    import asyncio as _asyncio
    await _asyncio.sleep(2)   # give Alpaca ~2s to register the position
    try:
        live_positions = await alpaca_client.get_positions()
        position_registered = any(
            getattr(p, "symbol", "") == occ for p in live_positions
        )
    except Exception:  # noqa: BLE001
        position_registered = True  # can't verify → assume OK, proceed

    if not position_registered:
        log.warning(
            "directional_execute_ghost_position_detected",
            occ=occ, qty=qty,
            msg="Fill confirmed but position not in Alpaca book — attempting immediate sell",
        )
        # Try to sell immediately — if Alpaca accepts it we close flat and avoid the loss.
        try:
            sell_order = await seller(qty=qty, occ_symbol=occ, limit_price=filled_premium)
            sell_final = await waiter(sell_order.order_id, timeout_s=10.0)
            log.info(
                "directional_execute_ghost_sell_attempted",
                occ=occ, status=sell_final.status,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("directional_execute_ghost_sell_failed", occ=occ, error=str(e))

        return DirectionalExecutionResult(
            executed=False, order=final, trade_id=None,
            reason=f"ghost_position — fill confirmed but position not in Alpaca book; immediate sell attempted",
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
            conviction=decision.conviction or "HIGH",
            order=final,
            entry_reasoning=decision.reasoning,
        )

    return DirectionalExecutionResult(
        executed=True,
        order=final,
        trade_id=trade_id,
        reason=f"filled {qty}× {occ} at ${filled_premium}",
        qty=qty,
        occ=occ,
        entry_premium=filled_premium,
    )
