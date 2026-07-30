"""Effective trading capital — tracks account size across days.

The 20% exposure cap, 15% daily-loss limit, and 10% position sizing all
scale with this value, so a $1k loss yesterday means a smaller cap today
and a $1k gain means a larger cap tomorrow.

Two modes:
- **paper**: `trading_capital_usd` (configured base, e.g. $5,000) plus the
  cumulative realized P&L of every closed trade in the DB since the
  optional `baseline_reset_at`. Paper accounts start with $100k from
  Alpaca, which would make sizing meaningless — so we keep a virtual
  account on the side.
- **live**: `account.equity` straight from Alpaca. Already reflects all
  realized + unrealized P&L on the funded account.

Floors at 0 (no negative capital → no execution).

Note on the daily-loss-limit fixed point: because capital is recomputed
each scan and includes today's realized losses, the effective halt point
under continuous shrinking is `base × pct / (1 + pct)` ≈ $652 on a $5k
account with a 15% setting, not the nominal $750. The math is conservative
(you stop bleeding sooner). If you want the nominal-$750 behavior, the
limit would need to anchor to the start-of-day capital rather than
current capital — a deliberate design tradeoff we kept simple for now.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from integrations import alpaca_client
from trademaster.config import get_settings
from trademaster.db import Trade, get_cumulative_realized_pnl, make_session_factory

_DIRECTIONAL_STRATEGIES = ("directional_call", "directional_put")


async def get_effective_capital(
    session_factory=None, *, strategy_group: str | None = None
) -> Decimal:
    """Return the trading capital available for sizing/limits right now.

    `strategy_group` selects an isolated capital pool (paper mode only):
      - "directional" → `directional_capital_usd` + realized P&L of directional
        trades only. Keeps the directional engine's sizing + loss limits on its
        own $10k pool, separate from the iron condor's $50k.
      - None → the legacy shared pool: `trading_capital_usd` + all realized P&L.
    Live mode returns Alpaca account equity regardless (a single funded account
    can't be split), so pool isolation is a paper-mode construct.
    """
    settings = get_settings()

    if settings.trading_mode == "live":
        try:
            account = await alpaca_client.get_account()
            return max(Decimal("0"), account.equity)
        except Exception:  # noqa: BLE001
            # Connectivity blip: return 0 so the exposure cap (also 0)
            # blocks new trades. Falling back to a stale `trading_capital_usd`
            # could over-deploy if the real account is smaller.
            return Decimal("0")

    # Paper: configured base + cumulative realized P&L (since baseline reset),
    # scoped to the pool's strategies.
    sf = session_factory or make_session_factory()
    if strategy_group == "directional":
        base = settings.directional_capital_usd
        realized = get_cumulative_realized_pnl(sf, strategies=_DIRECTIONAL_STRATEGIES)
    else:
        base = settings.trading_capital_usd
        realized = get_cumulative_realized_pnl(sf)
    return max(Decimal("0"), base + realized)


async def directional_unrealized_pnl(session_factory=None) -> Decimal:
    """Unrealized P&L of open DIRECTIONAL option positions only.

    Matches live Alpaca positions to the OCC symbols of open directional trades,
    so the directional loss limit reflects its own $10k pool and never trips on
    the iron condor's open-position swings. Returns 0 on any error (a blip must
    never halt trading) or when directional has nothing open."""
    sf = session_factory or make_session_factory()
    with sf() as session:
        occs = {
            t.symbol
            for t in session.execute(
                select(Trade).where(
                    Trade.strategy.in_(_DIRECTIONAL_STRATEGIES),
                    Trade.closed_at.is_(None),
                )
            ).scalars()
        }
    if not occs:
        return Decimal("0")
    try:
        positions = await alpaca_client.get_positions()
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return sum(
        (
            Decimal(str(getattr(p, "unrealized_pl", 0) or 0))
            for p in positions
            if getattr(p, "symbol", None) in occs
        ),
        Decimal("0"),
    )


def directional_deployed_usd(session: Session) -> Decimal:
    """Sum of capital-at-risk across open directional option trades only.

    Long-option dollars at risk = premium × qty × 100 (contract multiplier).
    Used by the 20% exposure cap and by Discord status — both report on the
    directional flow only, since the cap doesn't govern iron condors.
    """
    open_trades = list(
        session.execute(
            select(Trade).where(
                Trade.strategy.in_(_DIRECTIONAL_STRATEGIES),
                Trade.closed_at.is_(None),
            )
        ).scalars()
    )
    total = Decimal("0")
    for t in open_trades:
        total += Decimal(str(t.entry_price)) * Decimal(str(t.qty)) * Decimal("100")
    return total
