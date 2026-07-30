"""Signals-only shadow P&L tracker.

When the directional engine runs in `directional_signals_only` mode it posts
its broker-ready signals to #signals but books no real trades. To get a forward
read on signal quality — specifically, does the engine's HIGH-conviction bucket
clear the ~43% break-even win rate the loss analysis measured
(project_directional_loss_analysis) — this records each posted signal as a
SHADOW trade and scores it mark-to-market with the strategy's OWN exit
thresholds. **No orders are ever placed.**

Model (deliberately simple, slightly conservative — measures per-contract edge
and win rate, not exact book P&L):
  - entry premium  = the ask at signal time (what you'd pay), qty fixed at 1
  - mark           = current option mid on each scoring pass
  - take profit    = mark ≥ entry × (1 + pt)      (pt from _EXIT_PCT[mode])
  - stop           = mark ≤ entry × (1 − sl)      (sl from _EXIT_PCT[mode])
  - force close    = at/after 15:50 ET, close at the current mark
  - realized P&L   = (exit_mark − entry) × 100 × qty

It is an APPROXIMATION of the real exit stack (which adds trailing stops, a
theta backstop, and thesis-invalidation). The PT/stop/force-close skeleton
drives most of the P&L; the extra exits mostly cut losers a little sooner, so
this read is if anything mildly pessimistic on winners and lenient on the tail
— fine for a win-rate/edge signal. Shadow rows use strategy
'directional_shadow', which is deliberately NOT in DIRECTIONAL_STRATEGIES, so
the real exit monitor / reconciler / capital calc all ignore them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Callable

from agents.directional.executor import _EXIT_PCT, _resolve_expiry, select_best_strike
from integrations import alpaca_client
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.timeutils import to_et

log = get_logger(__name__)

SHADOW_STRATEGY = "directional_shadow"
FORCE_CLOSE_AFTER = time(15, 50)
# Budget passed to the strike selector — large enough that any single 0DTE
# contract is affordable, so it picks the strike closest to the target (ATM).
_STRIKE_BUDGET = 100_000.0


def _mid(quote) -> Decimal | None:
    """Fair mark from a quote, tolerating a missing mid (fall back to bid/ask avg)."""
    if quote is None:
        return None
    mid = getattr(quote, "mid", None)
    if mid is not None and Decimal(str(mid)) > 0:
        return Decimal(str(mid))
    bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
    if bid is not None and ask is not None:
        return ((Decimal(str(bid)) + Decimal(str(ask))) / 2).quantize(Decimal("0.01"))
    return None


async def record_shadow_signal(
    decision,
    *,
    mode: str,
    today: date | None = None,
    session_factory: Callable[[], object] | None = None,
    strike_selector: Callable[..., object] = select_best_strike,
) -> int | None:
    """Persist a posted signals-only signal as an open shadow trade.

    Prices the entry off the live chain (ask at the selected strike). Returns the
    shadow trade id, or None if the action is HOLD or no strike could be priced.
    Best-effort: never raises into the scan job."""
    if decision.action not in ("BUY_CALL", "BUY_PUT"):
        return None
    factory = session_factory or make_session_factory()
    today = today or datetime.now(UTC).date()
    option_type = "call" if decision.action == "BUY_CALL" else "put"
    try:
        expiry_date = _resolve_expiry(decision.expiry, today, decision.ticker)
        selected = await strike_selector(
            decision.ticker, expiry_date, option_type,
            decision.strike, _STRIKE_BUDGET,
        )
    except Exception as e:  # noqa: BLE001 — pricing is best-effort
        log.warning("shadow_record_price_failed", ticker=decision.ticker, error=str(e))
        return None
    if selected is None:
        log.info("shadow_record_no_strike", ticker=decision.ticker, strike=decision.strike)
        return None

    entry = Decimal(str(selected.quote.ask)).quantize(Decimal("0.01"))
    if entry <= 0:
        log.info("shadow_record_no_premium", ticker=decision.ticker, occ=selected.occ)
        return None

    pcts = _EXIT_PCT.get(mode, _EXIT_PCT["selective"])
    extra = {
        "shadow": True,
        "ticker": decision.ticker,
        "action": decision.action,
        "occ": selected.occ,
        "conviction": decision.conviction,
        "mode": mode,
        "strike": str(selected.strike),
        "expiry": expiry_date.isoformat(),
        "pt_pct": str(pcts["pt"]),
        "sl_pct": str(pcts["sl"]),
        "peak_premium": str(entry),
        "reasoning": (decision.reasoning or "")[:300],
    }
    with factory() as session:
        row = Trade(
            symbol=selected.occ,
            asset_class="option",
            side="buy",
            strategy=SHADOW_STRATEGY,
            qty=Decimal("1"),
            entry_price=entry,
            opened_at=datetime.now(UTC),
            extra=extra,
        )
        session.add(row)
        session.commit()
        tid = int(row.id)
    log.info(
        "shadow_signal_recorded",
        trade_id=tid, ticker=decision.ticker, action=decision.action,
        conviction=decision.conviction, occ=selected.occ, entry=str(entry),
    )
    return tid


def _open_shadow_trades(session) -> list[Trade]:
    from sqlalchemy import select
    stmt = select(Trade).where(
        Trade.strategy == SHADOW_STRATEGY, Trade.closed_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


async def score_shadow_signals(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], object] | None = None,
    quote_fetcher: Callable[..., object] = alpaca_client.get_single_option_quote,
    force_close: bool | None = None,
) -> list[dict]:
    """Mark every open shadow trade to market and close on PT / stop / force.

    Returns one dict per shadow processed (status hold/closed). Never raises on a
    single trade — a bad quote just holds that row for the next pass."""
    now = now or datetime.now(UTC)
    factory = session_factory or make_session_factory()
    if force_close is None:
        force_close = to_et(now).time() >= FORCE_CLOSE_AFTER

    with factory() as session:
        trades = _open_shadow_trades(session)
    results: list[dict] = []

    for trade in trades:
        extra = dict(trade.extra or {})
        entry = Decimal(str(trade.entry_price))
        try:
            quote = await quote_fetcher(extra.get("occ") or trade.symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("shadow_score_quote_failed", trade_id=trade.id, error=str(e))
            quote = None
        mark = _mid(quote)

        if mark is None:
            # No live market. At/after force-close a 0DTE long with no bid is
            # effectively worthless — realize at 0. Otherwise hold for next pass.
            if not force_close:
                results.append({"trade_id": trade.id, "status": "hold_no_quote"})
                continue
            mark = Decimal("0")

        peak = max(Decimal(str(extra.get("peak_premium", entry))), mark)
        extra["peak_premium"] = str(peak)
        pt = Decimal(str(extra.get("pt_pct", "0.5")))
        sl = Decimal(str(extra.get("sl_pct", "0.3")))
        pt_price = (entry * (1 + pt)).quantize(Decimal("0.01"))
        sl_price = (entry * (1 - sl)).quantize(Decimal("0.01"))

        reason = None
        if mark >= pt_price:
            reason = "shadow_profit_target"
        elif mark <= sl_price:
            reason = "shadow_stop"
        elif force_close:
            reason = "shadow_force_close"

        if reason is None:
            # Still open — persist the updated peak only.
            with factory() as session:
                row = session.get(Trade, trade.id)
                if row is not None:
                    row.extra = extra
                    session.commit()
            results.append({"trade_id": trade.id, "status": "hold", "mark": str(mark)})
            continue

        pnl = ((mark - entry) * Decimal("100") * Decimal(str(trade.qty))).quantize(Decimal("0.01"))
        extra["exit_reason"] = reason
        with factory() as session:
            row = session.get(Trade, trade.id)
            if row is not None and row.closed_at is None:
                row.exit_price = mark
                row.realized_pnl_usd = pnl
                row.closed_at = now
                row.extra = extra
                session.commit()
        log.info(
            "shadow_signal_closed",
            trade_id=trade.id, reason=reason, entry=str(entry),
            exit=str(mark), pnl=str(pnl), conviction=extra.get("conviction"),
        )
        results.append({
            "trade_id": trade.id, "status": "closed", "reason": reason,
            "pnl": str(pnl), "conviction": extra.get("conviction"),
        })
    return results


def shadow_summary(session_factory: Callable[[], object] | None = None) -> str | None:
    """Compact win-rate/P&L report by conviction over all closed shadow trades.

    Returns None when there are no closed shadow trades yet (nothing to post)."""
    from sqlalchemy import select
    factory = session_factory or make_session_factory()
    with factory() as session:
        rows = list(session.execute(
            select(Trade).where(
                Trade.strategy == SHADOW_STRATEGY, Trade.closed_at.is_not(None)
            )
        ).scalars())
    if not rows:
        return None

    by_conv: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        conv = (r.extra or {}).get("conviction") or "none"
        by_conv[conv].append(float(r.realized_pnl_usd or 0))
    all_pnl = [float(r.realized_pnl_usd or 0) for r in rows]
    n = len(all_pnl)
    wins = sum(1 for p in all_pnl if p > 0)

    lines = [
        f"📄 **Directional signals-only shadow P&L** (per 1-contract, no capital risked)",
        f"Overall: {n} signals · {wins / n * 100:.0f}% win · "
        f"net ${sum(all_pnl):+,.0f} · avg ${sum(all_pnl) / n:+.0f}/contract",
        "Break-even at this book's ~1.33:1 payoff ≈ 43% win.",
    ]
    for conv in ("HIGH", "MEDIUM", "LOW", "none"):
        v = by_conv.get(conv)
        if not v:
            continue
        w = sum(1 for p in v if p > 0)
        lines.append(
            f"• {conv}: {len(v)} · {w / len(v) * 100:.0f}% win · net ${sum(v):+,.0f}"
        )
    return "\n".join(lines)
