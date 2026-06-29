"""Position reconciliation on daemon startup.

Compares Alpaca's live option positions against the DB's open Trade rows
and repairs any mismatch caused by a crash, restart, or ghost fill.

Two cases:
  1. DB open, Alpaca gone  → position was closed/expired outside the bot
     (manual close, expiry, broker action). Mark the DB row closed at the
     last known bid so the capital model is accurate.
  2. Alpaca open, DB missing → position was opened outside the bot or the
     DB write failed after a fill. Log a warning — we can't safely reconstruct
     entry price or strategy metadata, so the operator must handle manually.

Runs once at startup before the scheduler starts. Silent if everything matches.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import select

from integrations import alpaca_client
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger
from trademaster.timeutils import ET, to_et

log = get_logger(__name__)

_DIRECTIONAL_STRATEGIES = {"directional_call", "directional_put"}
_CONDOR_STRATEGIES = {"spy_0dte_ic"}


def _strike_from_occ(occ: str) -> Decimal:
    """OCC strike = the trailing 8 digits scaled by 1/1000 (…00732000 → 732.000)."""
    return Decimal(occ[-8:]) / 1000


def _condor_settlement_debit(
    spot: float,
    *,
    short_put: Decimal,
    long_put: Decimal,
    short_call: Decimal,
    long_call: Decimal,
    max_loss_per_contract: Decimal | None,
) -> Decimal:
    """Per-contract dollar debit to settle an expired iron condor at intrinsic value.

    Standard condor payoff — only one side can finish ITM. Each spread's loss is
    bounded by its wing width, and the whole thing is capped at the trade's
    recorded max loss. Returns $/contract (per-share intrinsic × 100), matching
    the credit unit in `entry_price`. A fully-worthless expiry (spot between the
    shorts) returns 0 → full credit kept.
    """
    put_side = max(0.0, float(short_put) - spot) - max(0.0, float(long_put) - spot)
    call_side = max(0.0, spot - float(short_call)) - max(0.0, spot - float(long_call))
    intrinsic_per_share = max(0.0, put_side) + max(0.0, call_side)
    debit = Decimal(str(round(intrinsic_per_share * 100, 2)))
    if max_loss_per_contract is not None and debit > max_loss_per_contract:
        debit = max_loss_per_contract
    return debit


async def _underlying_close_on(d: date, symbol: str = "SPY") -> float | None:
    """Settlement close for the underlying on expiry date `d` (ET session)."""
    try:
        bars = await alpaca_client.get_daily_bars(symbol, limit=10)
    except Exception as e:  # noqa: BLE001
        log.warning("reconciler_settlement_bars_failed", symbol=symbol, error=str(e))
        return None
    for b in bars:
        if to_et(b.timestamp).date() == d:
            return float(b.close)
    return None


async def reconcile_positions(session_factory=None) -> list[str]:
    """Reconcile DB open trades against live Alpaca positions.

    Returns a list of human-readable warning messages (empty if clean).
    """
    sf = session_factory or make_session_factory()
    warnings: list[str] = []

    # Fetch live Alpaca option positions
    try:
        live_positions = await alpaca_client.get_positions()
    except Exception as e:  # noqa: BLE001
        log.warning("reconciler_positions_fetch_failed", error=str(e))
        return [f"⚠️ Reconciler: could not fetch Alpaca positions ({e}) — skipping check"]

    live_occs: set[str] = {
        getattr(p, "symbol", "")
        for p in live_positions
        if "option" in str(getattr(p, "asset_class", "")).lower()
    }

    # Fetch DB open directional trades
    with sf() as session:
        db_open = list(session.execute(
            select(Trade).where(
                Trade.strategy.in_(_DIRECTIONAL_STRATEGIES),
                Trade.closed_at.is_(None),
            )
        ).scalars())

    db_occs: dict[str, Trade] = {
        (t.extra or {}).get("occ_symbol", t.symbol): t
        for t in db_open
    }

    # Case 1: DB open but not in Alpaca → closed outside the bot
    for occ, trade in db_occs.items():
        if occ not in live_occs:
            # Fetch last known quote for exit price approximation
            exit_price = trade.entry_price  # fallback: record at entry
            try:
                quote = await alpaca_client.get_single_option_quote(occ)
                if quote and quote.bid > 0:
                    exit_price = quote.bid
            except Exception:  # noqa: BLE001
                pass

            with sf() as session:
                row = session.get(Trade, trade.id)
                if row is not None and row.closed_at is None:
                    entry_p = Decimal(str(row.entry_price))
                    exit_p = Decimal(str(exit_price))
                    row.exit_price = exit_p
                    row.realized_pnl_usd = (exit_p - entry_p) * 100 * Decimal(str(row.qty))
                    row.closed_at = datetime.now(UTC)
                    extra = dict(row.extra or {})
                    extra["exit_reason"] = "reconciliation_not_in_broker"
                    extra["exit_reasoning"] = "Position missing from Alpaca on daemon startup — closed by reconciler"
                    row.extra = extra
                    session.commit()

            msg = (
                f"⚠️ Reconciler: trade #{trade.id} ({occ}) was open in DB but "
                f"not found in Alpaca — marked closed at ${float(exit_price):.2f}. "
                f"Check if it was manually closed or expired."
            )
            warnings.append(msg)
            log.warning(
                "reconciler_db_open_not_in_broker",
                trade_id=trade.id, occ=occ, exit_price=str(exit_price),
            )

    # Condor settlement: the exit monitor closes condors by submitting an MLEG
    # buy_to_close, but near 0DTE expiry legs get swept early (or lose quotes), so
    # that order can fail (APIError 42210000 "intent mismatch") or hit no_quotes —
    # and the daemon's 16:15 stop-timer fires before the 16:00 settlement is booked.
    # The directional reconciler above never sees condors, so a fully-won condor
    # would otherwise sit open forever. Settle expired condors here from the
    # underlying's expiry-day close: deterministic, needs no quotes or order.
    condor_leg_occs: set[str] = set()
    with sf() as session:
        db_condors = list(session.execute(
            select(Trade).where(
                Trade.strategy.in_(_CONDOR_STRATEGIES),
                Trade.closed_at.is_(None),
            )
        ).scalars())

    today_et = to_et(datetime.now(UTC)).date()
    for trade in db_condors:
        extra = trade.extra or {}
        legs = {k: extra.get(k) for k in ("short_put", "long_put", "short_call", "long_call")}
        for occ in legs.values():
            if occ:
                condor_leg_occs.add(occ)

        expiry = trade.opened_at.date() if trade.opened_at is not None else None
        exp_str = extra.get("expiry")
        if exp_str:
            try:
                expiry = date.fromisoformat(exp_str)
            except ValueError:
                pass
        if expiry is None or expiry >= today_et:
            continue  # not yet expired — a legitimately open (multi-day) condor

        if not all(legs.values()):
            warnings.append(
                f"⚠️ Reconciler: condor #{trade.id} expired {expiry} but is missing leg "
                f"OCCs in extra — cannot settle, left open. Handle manually."
            )
            log.warning("reconciler_condor_missing_legs", trade_id=trade.id, extra=extra)
            continue

        spot = await _underlying_close_on(expiry)
        if spot is None:
            warnings.append(
                f"⚠️ Reconciler: condor #{trade.id} expired {expiry} but no underlying "
                f"close found for that date — left open. Handle manually."
            )
            continue

        mlc = extra.get("max_loss_per_contract")
        debit = _condor_settlement_debit(
            spot,
            short_put=_strike_from_occ(legs["short_put"]),
            long_put=_strike_from_occ(legs["long_put"]),
            short_call=_strike_from_occ(legs["short_call"]),
            long_call=_strike_from_occ(legs["long_call"]),
            max_loss_per_contract=Decimal(str(mlc)) if mlc is not None else None,
        )

        with sf() as session:
            row = session.get(Trade, trade.id)
            if row is None or row.closed_at is not None:
                continue
            credit = Decimal(str(row.entry_price))
            qty = Decimal(str(row.qty))
            realized = (credit - debit) * qty
            row.exit_price = debit
            row.realized_pnl_usd = realized
            # Date the close to the 16:00 ET expiry, not "now" (next-morning startup),
            # so weekly-review week attribution stays correct.
            row.closed_at = datetime.combine(expiry, time(16, 0), tzinfo=ET).astimezone(UTC)
            ex = dict(row.extra or {})
            ex["exit_reason"] = "expired_settled"
            ex["exit_reasoning"] = (
                f"0DTE condor settled by reconciler from SPY {spot:.2f} close on "
                f"{expiry} (intrinsic debit ${float(debit):.2f}/contract). The intraday "
                f"MLEG close did not book (legs swept / no quotes at expiry)."
            )
            ex["settlement_spot"] = round(spot, 2)
            row.extra = ex
            session.commit()

        warnings.append(
            f"✅ Reconciler: condor #{trade.id} expired {expiry}, settled at SPY "
            f"{spot:.2f} → realized ${float(realized):+.2f} (debit ${float(debit):.2f}/ct)."
        )
        log.info(
            "reconciler_condor_settled",
            trade_id=trade.id, expiry=str(expiry), spot=spot,
            debit=str(debit), realized=str(realized),
        )

    # Case 2: Alpaca open but not in DB → opened outside the bot
    for occ in live_occs:
        if occ not in db_occs and occ not in condor_leg_occs:
            msg = (
                f"⚠️ Reconciler: option position {occ} found in Alpaca but "
                f"not in TradeMaster DB — was it opened manually? "
                f"Bot will NOT manage this position. Close it manually if needed."
            )
            warnings.append(msg)
            log.warning("reconciler_broker_open_not_in_db", occ=occ)

    if not warnings:
        log.info("reconciler_clean", db_open=len(db_occs), broker_open=len(live_occs))

    return warnings
