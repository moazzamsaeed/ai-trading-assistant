"""Monthly LLM-spend budget tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trademaster.config import get_settings
from trademaster.db import AgentRun, Base, make_engine, make_session_factory
from trademaster.llm.budget import check_budget, current_month_spend_usd
from trademaster.llm.types import BudgetExceededError


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s


def _add_run(session, cost: str, when: datetime) -> None:
    session.add(
        AgentRun(
            started_at=when,
            finished_at=when,
            task_type="orchestrate",
            provider="anthropic",
            model="claude-opus-4-7",
            cost_usd=Decimal(cost),
        )
    )
    session.commit()


def test_spend_is_zero_with_no_runs(session):
    assert current_month_spend_usd(session) == Decimal("0")


def test_spend_sums_current_month(session):
    now = datetime.now(UTC).replace(day=15, hour=12)
    _add_run(session, "1.50", now)
    _add_run(session, "2.50", now)
    assert current_month_spend_usd(session, now=now) == Decimal("4.00")


def test_spend_ignores_prior_months(session):
    now = datetime(2026, 5, 15, 12, tzinfo=UTC)
    last_month = datetime(2026, 4, 28, 12, tzinfo=UTC)
    _add_run(session, "99.00", last_month)
    _add_run(session, "1.50", now)
    assert current_month_spend_usd(session, now=now) == Decimal("1.50")


def test_check_budget_passes_below_cap(session):
    now = datetime.now(UTC).replace(day=10)
    _add_run(session, "10.00", now)
    # default cap is $100; $10 spend → fine
    assert check_budget(session, now=now) == Decimal("10.00")


def test_check_budget_warns_above_threshold(session, caplog):
    now = datetime.now(UTC).replace(day=10)
    _add_run(session, "85.00", now)
    # 85% > 80% warn threshold, < 100% cap → returns spend, logs warning
    spend = check_budget(session, now=now)
    assert spend == Decimal("85.00")


def test_check_budget_refuses_at_cap(session):
    now = datetime.now(UTC).replace(day=10)
    cap = get_settings().monthly_llm_budget_usd
    _add_run(session, str(cap), now)
    with pytest.raises(BudgetExceededError) as exc:
        check_budget(session, now=now)
    assert "budget exhausted" in str(exc.value).lower()


def test_check_budget_bypass_allows_over_cap(session):
    now = datetime.now(UTC).replace(day=10)
    cap = get_settings().monthly_llm_budget_usd
    _add_run(session, str(cap + Decimal("50")), now)
    # bypass returns current spend, does not raise
    spend = check_budget(session, bypass_budget=True, now=now)
    assert spend == cap + Decimal("50")
