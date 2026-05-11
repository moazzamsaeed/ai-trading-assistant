"""Router dispatch, agent_runs persistence, and budget enforcement tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trademaster import router
from trademaster.config import get_settings
from trademaster.db import AgentRun, Base, make_engine, make_session_factory
from trademaster.llm.types import AuthError, BudgetExceededError, LLMResponse


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _ok_response(provider: str, model: str) -> LLMResponse:
    return LLMResponse(
        text="ok",
        provider=provider,
        model=model,
        input_tokens=100,
        output_tokens=50,
        cost_usd=Decimal("0.01"),
        duration_ms=42,
    )


async def test_dispatches_to_correct_provider_for_each_task(monkeypatch, session_factory):
    calls: list[tuple[str, str]] = []

    async def fake_anthropic(prompt, *, model, **_):
        calls.append(("anthropic", model))
        return _ok_response("anthropic", model)

    async def fake_google(prompt, *, model, **_):
        calls.append(("google", model))
        return _ok_response("google", model)

    async def fake_deepseek(prompt, *, model, **_):
        calls.append(("deepseek", model))
        return _ok_response("deepseek", model)

    monkeypatch.setitem(router._DISPATCH, "anthropic", fake_anthropic)
    monkeypatch.setitem(router._DISPATCH, "google", fake_google)
    monkeypatch.setitem(router._DISPATCH, "deepseek", fake_deepseek)

    await router.route_to_model(
        router.TaskType.ORCHESTRATE, "p", session_factory=session_factory
    )
    await router.route_to_model(
        router.TaskType.PRE_MARKET_RESEARCH, "p", session_factory=session_factory
    )
    await router.route_to_model(
        router.TaskType.INTRADAY_SCAN, "p", session_factory=session_factory
    )

    assert ("anthropic", "claude-opus-4-7") in calls
    assert ("google", "gemini-3.1-pro-preview") in calls
    assert ("deepseek", "deepseek-v4-flash") in calls


async def test_agent_run_row_written_on_success(monkeypatch, session_factory):
    async def fake(prompt, *, model, **_):
        return _ok_response("anthropic", model)

    monkeypatch.setitem(router._DISPATCH, "anthropic", fake)

    await router.route_to_model(
        router.TaskType.ORCHESTRATE, "p", session_factory=session_factory
    )

    with session_factory() as s:
        rows = s.query(AgentRun).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "anthropic"
        assert row.model == "claude-opus-4-7"
        assert row.input_tokens == 100
        assert row.output_tokens == 50
        assert row.cost_usd == Decimal("0.01")
        assert row.error is None
        assert row.duration_ms is not None and row.duration_ms >= 0


async def test_agent_run_row_written_on_failure(monkeypatch, session_factory):
    async def fake(prompt, *, model, **_):
        raise AuthError("bad key")

    monkeypatch.setitem(router._DISPATCH, "anthropic", fake)

    with pytest.raises(AuthError):
        await router.route_to_model(
            router.TaskType.ORCHESTRATE, "p", session_factory=session_factory
        )

    with session_factory() as s:
        rows = s.query(AgentRun).all()
        assert len(rows) == 1
        assert rows[0].error is not None
        assert "AuthError" in rows[0].error
        assert rows[0].cost_usd is None


async def test_budget_blocks_call_over_cap(monkeypatch, session_factory):
    cap = get_settings().monthly_llm_budget_usd
    with session_factory() as s:
        s.add(
            AgentRun(
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                task_type="orchestrate",
                provider="anthropic",
                model="claude-opus-4-7",
                cost_usd=cap,
            )
        )
        s.commit()

    async def fake(prompt, *, model, **_):  # pragma: no cover — should not be called
        raise AssertionError("dispatcher should never run when over budget")

    monkeypatch.setitem(router._DISPATCH, "anthropic", fake)

    with pytest.raises(BudgetExceededError):
        await router.route_to_model(
            router.TaskType.ORCHESTRATE, "p", session_factory=session_factory
        )

    # the rejection itself must be recorded
    with session_factory() as s:
        rows = s.query(AgentRun).filter(AgentRun.error.isnot(None)).all()
        assert any("BudgetExceededError" in (r.error or "") for r in rows)


async def test_bypass_budget_allows_call_over_cap(monkeypatch, session_factory):
    cap = get_settings().monthly_llm_budget_usd
    with session_factory() as s:
        s.add(
            AgentRun(
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                task_type="orchestrate",
                provider="anthropic",
                model="claude-opus-4-7",
                cost_usd=cap + Decimal("100"),
            )
        )
        s.commit()

    async def fake(prompt, *, model, **_):
        return _ok_response("anthropic", model)

    monkeypatch.setitem(router._DISPATCH, "anthropic", fake)

    resp = await router.route_to_model(
        router.TaskType.ORCHESTRATE,
        "kill-switch",
        bypass_budget=True,
        session_factory=session_factory,
    )
    assert resp.text == "ok"
