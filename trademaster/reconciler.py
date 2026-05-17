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

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from integrations import alpaca_client
from trademaster.db import Trade, make_session_factory
from trademaster.logging import get_logger

log = get_logger(__name__)

_DIRECTIONAL_STRATEGIES = {"directional_call", "directional_put"}


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
        if getattr(p, "asset_class", "") in ("us_option", "option")
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

    # Case 2: Alpaca open but not in DB → opened outside the bot
    for occ in live_occs:
        if occ not in db_occs:
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
