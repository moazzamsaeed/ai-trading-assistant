"""Monthly LLM-spend budget tracker.

Queries `agent_runs.cost_usd` for the current calendar month (UTC) and
compares against `settings.monthly_llm_budget_usd`. Warns at 80%, refuses
new non-essential calls at 100%.

A trading decision in the middle of a kill-switch should never be blocked
by budget — callers pass `bypass_budget=True` for paths that must run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hermes.config import get_settings
from hermes.db import AgentRun
from hermes.llm.types import BudgetExceededError
from hermes.logging import get_logger

log = get_logger(__name__)

WARN_THRESHOLD = Decimal("0.80")


def _start_of_month_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def current_month_spend_usd(session: Session, now: datetime | None = None) -> Decimal:
    """Sum agent_runs.cost_usd for the current calendar month (UTC).

    Returns Decimal('0') if there are no runs yet this month.
    """
    start = _start_of_month_utc(now)
    stmt = select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
        AgentRun.started_at >= start
    )
    total = session.execute(stmt).scalar_one()
    return Decimal(str(total))


def check_budget(
    session: Session,
    *,
    bypass_budget: bool = False,
    now: datetime | None = None,
) -> Decimal:
    """Raise BudgetExceededError if the cap is reached. Returns current spend.

    Logs a warning when spend crosses WARN_THRESHOLD. `bypass_budget=True`
    skips enforcement but still returns the current total for logging.
    """
    spend = current_month_spend_usd(session, now)
    cap = get_settings().monthly_llm_budget_usd

    if bypass_budget:
        return spend

    if spend >= cap:
        raise BudgetExceededError(
            f"Monthly LLM budget exhausted: ${spend:.2f} / ${cap:.2f}. "
            f"Pass bypass_budget=True for critical paths."
        )

    if spend >= cap * WARN_THRESHOLD:
        log.warning(
            "llm_budget_warning",
            spend_usd=str(spend),
            cap_usd=str(cap),
            pct_used=float(spend / cap),
        )

    return spend
