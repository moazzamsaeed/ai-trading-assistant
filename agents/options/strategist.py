"""SPY 0DTE iron-condor strategist agent.

Pipeline (entry-window job, 9:45 ET):

  1. Pull current SPY quote + ATM IV from the chain
  2. Build a candidate iron-condor plan via strategies.spy_0dte_iron_condor
  3. Ask DeepSeek V4-Pro to confirm or veto, given market state
  4. If confirmed → risk_manager.validate_signal → if approved, return signal
  5. Persist the Signal row regardless

Phase 2.2 stops at "approved plan." Order submission to Alpaca lives in
Phase 2.3 so the paper-trade execution path stays a separate review.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from agents.options.executor import execute_iron_condor
from integrations import alpaca_client
from integrations.alpaca_client import OptionQuote, StockQuote
from strategies.spy_0dte_iron_condor import (
    IronCondorBuildError,
    IronCondorPlan,
    build_iron_condor,
)
from trademaster import risk_manager
from trademaster.config import get_settings
from trademaster.db import Signal as SignalRow
from trademaster.db import make_session_factory
from trademaster.logging import get_logger
from trademaster.models import Signal, SignalAction
from trademaster.options_math import delta_from_market_mid
from trademaster.risk_manager import RiskRejectionError
from trademaster.router import TaskType, route_to_model

log = get_logger(__name__)

AGENT_NAME = "options"
UNDERLYING = "SPY"

# Tightest chain slice that contains plausible 16-delta shorts + $5 wings on either side.
CHAIN_HALF_WIDTH = Decimal("20")

PROMPT_TEMPLATE = """You are the SPY 0DTE iron-condor strategist for TradeMaster.

Current market snapshot ({timestamp}):
- SPY mid: ${spy_mid}
- ATM implied volatility: {atm_iv}
- Time of day (ET): {now_et}

Candidate iron condor (expires today, {expiry}):
- Short put : strike ${sp_strike} · delta {sp_delta} · bid ${sp_bid} · ask ${sp_ask}
- Long put  : strike ${lp_strike} · delta {lp_delta} · bid ${lp_bid} · ask ${lp_ask}
- Short call: strike ${sc_strike} · delta {sc_delta} · bid ${sc_bid} · ask ${sc_ask}
- Long call : strike ${lc_strike} · delta {lc_delta} · bid ${lc_bid} · ask ${lc_ask}
- Net credit per contract: ${credit}
- Max loss per contract:   ${max_loss}
- Risk/reward:             1:{rr}

The strategy thesis (STRATEGIES.md):
- Enter only when IV is elevated (mean-reversion edge)
- Hold time is intraday only; force close at 15:50 ET
- Defined risk; cash account

Respond with a single JSON object (no other text):

{{
  "decision": "OPEN" or "HOLD",
  "confidence": 0.0 to 1.0,
  "reasoning": "two-to-four-sentence justification grounded in IV regime, \
spread quality (bid/ask widths), and risk/reward"
}}

Reject (HOLD) if:
- IV looks subdued (would need strong evidence otherwise)
- Bid/ask spreads are wider than $0.10 on any leg (illiquid → bad fills)
- Risk/reward < 1:0.10 (credit too thin for the wing risk)
- Anything in the snapshot looks unusual
"""


# ----------------- helpers -----------------


def _atm_iv(chain: list[OptionQuote], spy_mid: Decimal) -> Decimal | None:
    """Average IV of the ATM call and put."""
    candidates = [
        q for q in chain
        if q.implied_volatility is not None and abs(q.strike - spy_mid) < Decimal("1")
    ]
    if not candidates:
        return None
    total = sum((q.implied_volatility for q in candidates), Decimal("0"))
    return total / Decimal(len(candidates))


def _enrich_chain_with_bs_greeks(
    chain: list[OptionQuote],
    *,
    spot: Decimal,
    now: datetime,
) -> list[OptionQuote]:
    """Fill in missing IV + delta from each option's market mid via BS.

    Alpaca's default (and indicative) options feed returns prices but no
    greeks. We solve IV from the market mid for every leg, then derive
    delta. Strikes whose mid is at the $0.01 minimum tick (no real
    market) get skipped — their greeks stay None and the strategy code
    treats them as untradeable.
    """
    spot_f = float(spot)
    enriched: list[OptionQuote] = []
    for q in chain:
        if q.delta is not None and q.implied_volatility is not None:
            enriched.append(q)
            continue
        # T in years from now until end-of-day on expiry (assume 4pm ET = 20:00 UTC)
        expiry_close = datetime(
            q.expiry.year, q.expiry.month, q.expiry.day, 20, 0, tzinfo=UTC
        )
        t_seconds = max((expiry_close - now).total_seconds(), 0.0)
        t_years = t_seconds / (365 * 24 * 3600)
        result = delta_from_market_mid(
            market_mid=float(q.mid),
            S=spot_f,
            K=float(q.strike),
            T=t_years,
            option_type=q.option_type,
        )
        if result is None:
            enriched.append(q)
            continue
        delta_f, iv_f = result
        enriched.append(
            OptionQuote(
                occ_symbol=q.occ_symbol,
                underlying=q.underlying,
                strike=q.strike,
                expiry=q.expiry,
                option_type=q.option_type,
                bid=q.bid,
                ask=q.ask,
                mid=q.mid,
                delta=Decimal(f"{delta_f:.4f}"),
                gamma=q.gamma,
                theta=q.theta,
                vega=q.vega,
                implied_volatility=Decimal(f"{iv_f:.4f}"),
            )
        )
    return enriched


def _format_plan_for_prompt(
    plan: IronCondorPlan,
    *,
    spy_mid: Decimal,
    atm_iv: Decimal | None,
    now: datetime,
) -> str:
    sp, lp, sc, lc = plan.short_put, plan.long_put, plan.short_call, plan.long_call
    rr = (plan.max_loss_per_contract / plan.credit_per_contract).quantize(Decimal("0.01"))
    return PROMPT_TEMPLATE.format(
        timestamp=now.isoformat(),
        spy_mid=spy_mid,
        atm_iv=(f"{atm_iv:.4f}" if atm_iv is not None else "unknown"),
        now_et=now.astimezone().strftime("%H:%M"),
        expiry=sp.expiry.isoformat(),
        sp_strike=sp.strike, sp_delta=sp.delta, sp_bid=sp.bid, sp_ask=sp.ask,
        lp_strike=lp.strike, lp_delta=lp.delta, lp_bid=lp.bid, lp_ask=lp.ask,
        sc_strike=sc.strike, sc_delta=sc.delta, sc_bid=sc.bid, sc_ask=sc.ask,
        lc_strike=lc.strike, lc_delta=lc.delta, lc_bid=lc.bid, lc_ask=lc.ask,
        credit=plan.credit_per_contract,
        max_loss=plan.max_loss_per_contract,
        rr=rr,
    )


def _parse_decision(text: str) -> tuple[str, float | None, str]:
    """Parse the JSON envelope from the strategist. Tolerates code-fence wrapping."""
    s = text.strip()
    if s.startswith("```"):
        # Strip fenced markdown blocks: ```json\n...\n```
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return "HOLD", None, f"unparseable strategist response: {text[:200]}"
    decision = str(obj.get("decision", "HOLD")).upper()
    if decision not in ("OPEN", "HOLD"):
        decision = "HOLD"
    confidence = obj.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    reasoning = str(obj.get("reasoning", ""))[:1500]
    return decision, confidence, reasoning


def format_manual_signal(plan: IronCondorPlan, signal: Signal) -> str:
    """Broker-ready entry instructions for #signals (manual trader).

    Lists every leg with strike, expiry, call/put, side, and target price.
    Includes net credit, max loss, and concrete exit thresholds in $/contract.
    """
    expiry_iso = plan.short_put.expiry.isoformat()
    qty = plan.qty
    # Exit thresholds in net-debit terms per contract.
    pt_debit = (plan.credit_per_contract / Decimal("2")).quantize(Decimal("0.01"))
    stop_debit = (plan.credit_per_contract * Decimal("3")).quantize(Decimal("0.01"))
    return (
        f"🎯 **SPY 0DTE Iron Condor — manual entry signal**\n"
        f"Expiry: **{expiry_iso}** · qty per side: **{qty}**\n"
        f"\n"
        f"**Legs (open as a credit spread):**\n"
        f"• SELL {qty} × SPY {expiry_iso} **${plan.short_put.strike} PUT**  "
        f"@ mid ${plan.short_put.mid:.2f}\n"
        f"• BUY  {qty} × SPY {expiry_iso} **${plan.long_put.strike} PUT**   "
        f"@ mid ${plan.long_put.mid:.2f}\n"
        f"• SELL {qty} × SPY {expiry_iso} **${plan.short_call.strike} CALL** "
        f"@ mid ${plan.short_call.mid:.2f}\n"
        f"• BUY  {qty} × SPY {expiry_iso} **${plan.long_call.strike} CALL**  "
        f"@ mid ${plan.long_call.mid:.2f}\n"
        f"\n"
        f"**Target net credit:** ${plan.credit_per_contract}/contract "
        f"(total ${plan.credit_received})\n"
        f"**Max loss:** ${plan.max_loss_per_contract}/contract "
        f"(total ${plan.max_loss})\n"
        f"\n"
        f"**Exit rules:**\n"
        f"• Profit target: close at **≤ ${pt_debit} net debit** (50% of credit)\n"
        f"• Stop loss: close at **≥ ${stop_debit} net debit** (loss = 2× credit)\n"
        f"• Force close: **15:50 ET** regardless of P&L\n"
        f"\n"
        f"Strategist confidence: {signal.confidence} · Rationale: {signal.reasoning}"
    )


def format_trade_telemetry(plan: IronCondorPlan, signal: Signal, execution) -> str:
    """Automated-execution telemetry for #trades (read-only)."""
    mode = get_settings().trading_mode.upper()
    pending_id = getattr(execution, "pending_id", None)
    if execution.executed:
        status = f"✅ EXECUTED ({mode}) · {execution.reason} · trade #{execution.trade_id}"
    elif pending_id is not None:
        status = (
            f"⏳ AWAITING APPROVAL ({mode}) · pending #{pending_id}\n"
            f"Run `/approve {pending_id}` to submit · `/reject {pending_id}` to skip · "
            f"`/pending` to list all"
        )
    else:
        status = f"⚠️ NOT EXECUTED ({mode}) · {execution.reason}"
    return (
        f"🤖 **Iron-condor execution** ({plan.short_put.expiry})\n"
        f"Strikes: SP ${plan.short_put.strike} / LP ${plan.long_put.strike} / "
        f"SC ${plan.short_call.strike} / LC ${plan.long_call.strike} · qty {plan.qty}\n"
        f"Target credit: ${plan.credit_per_contract}/contract · "
        f"Max loss: ${plan.max_loss_per_contract}/contract\n"
        f"{status}\n"
        f"Strategist confidence: {signal.confidence}"
    )


# ----------------- public entry point -----------------


async def run_iron_condor_strategist(
    *,
    target_short_abs_delta: Decimal = Decimal("0.16"),
    wing_width: Decimal = Decimal("5"),
    qty: int = 1,
    now: datetime | None = None,
    session_factory: Callable[[], Session] | None = None,
    stock_fetcher: Callable[[str], object] = alpaca_client.get_latest_stock_quote,
    chain_fetcher: Callable[..., object] = alpaca_client.get_options_chain,
    account_fetcher: Callable[[], object] | None = None,
    executor: Callable[..., object] = execute_iron_condor,
) -> tuple[Signal, str | None, str | None]:
    """Run the full strategist pipeline.

    Returns `(signal, signals_text, trade_text)`:
      - `signals_text` is the broker-ready manual signal for #signals.
        None when HOLD or risk-rejected (don't spam manual traders with
        non-actionable noise).
      - `trade_text` is the automated-execution telemetry for #trades.
        None when HOLD or risk-rejected (no execution happened).
    """
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    expiry: date = now.date()

    # 1. SPY quote
    spy: StockQuote = await stock_fetcher(UNDERLYING)
    spy_mid = spy.mid if spy.mid > 0 else (spy.bid or spy.ask)

    # 2. Options chain (narrow window around ATM)
    chain = await chain_fetcher(
        UNDERLYING,
        expiry=expiry,
        strike_lo=spy_mid - CHAIN_HALF_WIDTH,
        strike_hi=spy_mid + CHAIN_HALF_WIDTH,
    )

    # 2b. Alpaca's indicative options feed doesn't include greeks/IV. Fill them
    # in from BS inversion on each leg's market mid — see trademaster.options_math.
    chain = _enrich_chain_with_bs_greeks(chain, spot=spy_mid, now=now)

    # 3. Build the plan. If construction fails, persist a HOLD with the error.
    try:
        plan = build_iron_condor(
            chain,
            qty=qty,
            target_short_abs_delta=target_short_abs_delta,
            wing_width=wing_width,
        )
    except IronCondorBuildError as e:
        signal = await _persist_hold(
            factory,
            reasoning=f"plan construction failed: {e}",
            extra={"error": str(e), "spy_mid": str(spy_mid)},
        )
        return signal, None, None

    atm_iv = _atm_iv(chain, spy_mid)

    # 4. Ask DeepSeek V4-Pro
    prompt = _format_plan_for_prompt(plan, spy_mid=spy_mid, atm_iv=atm_iv, now=now)
    response = await route_to_model(
        TaskType.OPTIONS_STRATEGY, prompt, session_factory=factory
    )
    decision, confidence, reasoning = _parse_decision(response.text)
    log.info(
        "options_strategist_decision",
        decision=decision,
        confidence=confidence,
        credit=str(plan.credit_per_contract),
        max_loss=str(plan.max_loss_per_contract),
    )

    extra_common = {
        "spy_mid": str(spy_mid),
        "atm_iv": str(atm_iv) if atm_iv is not None else None,
        "short_put_strike": str(plan.short_put.strike),
        "short_call_strike": str(plan.short_call.strike),
        "wing_width": str(wing_width),
        "credit_per_contract": str(plan.credit_per_contract),
        "max_loss_per_contract": str(plan.max_loss_per_contract),
        "model": response.model,
        "cost_usd": str(response.cost_usd),
    }

    if decision == "HOLD":
        signal = await _persist_hold(
            factory, reasoning=reasoning or "strategist declined", extra=extra_common
        )
        return signal, None, None

    # 5. OPEN — risk-manager gate. Failure becomes a HOLD record.
    order = plan.to_trade_order()
    open_signal = Signal(
        task_type=TaskType.OPTIONS_STRATEGY.value,
        agent=AGENT_NAME,
        action=SignalAction.OPEN,
        symbol=UNDERLYING,
        confidence=confidence,
        reasoning=reasoning,
        order=order,
        extra=extra_common,
    )

    persisted_id = await _persist(factory, open_signal, accepted=None)

    validate_kwargs: dict = {"signal_id": persisted_id, "session_factory": factory}
    if account_fetcher is not None:
        validate_kwargs["account_fetcher"] = account_fetcher
    try:
        await risk_manager.validate_signal(open_signal, **validate_kwargs)
    except RiskRejectionError as e:
        with factory() as s:
            row = s.get(SignalRow, persisted_id)
            if row is not None:
                row.accepted = False
                row.rejection_reason = str(e)
                s.commit()
        log.info("options_strategist_rejected_by_risk", reason=str(e))
        return open_signal, None, None

    with factory() as s:
        row = s.get(SignalRow, persisted_id)
        if row is not None:
            row.accepted = True
            s.commit()

    # Always emit the manual signal once risk approves — the user can act on
    # it regardless of the bot's auto-execution outcome.
    signals_text = format_manual_signal(plan, open_signal)

    # Paper mode auto-executes; live mode creates a pending_orders row
    # awaiting Discord /approve (D-014). Either way, the executor returns
    # an ExecutionResult that we surface to #trades.
    execution = await executor(
        plan,
        session_factory=factory,
        summary=signals_text,
        signal_id=persisted_id,
    )
    log.info(
        "options_strategist_execution",
        executed=execution.executed,
        reason=execution.reason,
        trade_id=execution.trade_id,
        pending_id=getattr(execution, "pending_id", None),
    )
    trade_text = format_trade_telemetry(plan, open_signal, execution)
    return open_signal, signals_text, trade_text


# ----------------- persistence helpers -----------------


async def _persist_hold(
    factory: Callable[[], Session],
    *,
    reasoning: str,
    extra: dict,
) -> Signal:
    sig = Signal(
        task_type=TaskType.OPTIONS_STRATEGY.value,
        agent=AGENT_NAME,
        action=SignalAction.HOLD,
        symbol=UNDERLYING,
        reasoning=reasoning,
        extra=extra,
    )
    await _persist(factory, sig, accepted=True)
    return sig


async def _persist(
    factory: Callable[[], Session],
    sig: Signal,
    *,
    accepted: bool | None,
) -> int:
    with factory() as s:
        row = SignalRow(
            task_type=sig.task_type,
            agent=sig.agent,
            action=sig.action.value,
            symbol=sig.symbol,
            confidence=sig.confidence,
            reasoning=sig.reasoning,
            payload=sig.extra,
            accepted=accepted,
        )
        s.add(row)
        s.commit()
        return int(row.id)
