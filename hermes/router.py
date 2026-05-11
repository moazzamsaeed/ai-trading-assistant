"""Routes a task type to the correct LLM provider/model.

Hermes calls `await route_to_model(task_type, prompt, ...)` and gets an
`LLMResponse` back. Responsibilities of this module:

- Map `TaskType` → (provider, model) via MODEL_MAP
- Enforce the monthly LLM budget (`bypass_budget=True` for critical paths)
- Dispatch to the right provider client
- Persist an `agent_runs` row on every attempt — success or failure
- Surface a uniform `RouterError` hierarchy to callers
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.orm import Session

from hermes.db import AgentRun, make_session_factory
from hermes.llm import anthropic_client, deepseek_client, google_client
from hermes.llm.budget import check_budget
from hermes.llm.types import LLMResponse, ProviderError, RouterError
from hermes.logging import get_logger

log = get_logger(__name__)


class TaskType(StrEnum):
    ORCHESTRATE = "orchestrate"
    PRE_MARKET_RESEARCH = "pre_market_research"
    INTRADAY_SCAN = "intraday_scan"
    FORMAT_ALERT = "format_alert"
    OPTIONS_STRATEGY = "options_strategy"
    CRYPTO_REGIME = "crypto_regime"
    EXECUTION_DECISION = "execution_decision"


MODEL_MAP: dict[TaskType, tuple[str, str]] = {
    TaskType.ORCHESTRATE: ("anthropic", "claude-opus-4-7"),
    TaskType.PRE_MARKET_RESEARCH: ("google", "gemini-3.1-pro-preview"),
    TaskType.INTRADAY_SCAN: ("deepseek", "deepseek-v4-flash"),
    TaskType.FORMAT_ALERT: ("deepseek", "deepseek-v4-flash"),
    TaskType.OPTIONS_STRATEGY: ("deepseek", "deepseek-v4-pro"),
    TaskType.CRYPTO_REGIME: ("deepseek", "deepseek-v4-pro"),
    TaskType.EXECUTION_DECISION: ("anthropic", "claude-opus-4-7"),
}


Dispatcher = Callable[..., Awaitable[LLMResponse]]

_DISPATCH: dict[str, Dispatcher] = {
    "anthropic": anthropic_client.complete,
    "google": google_client.complete,
    "deepseek": deepseek_client.complete,
}


def _persist_run(
    session: Session,
    *,
    task_type: TaskType,
    provider: str,
    model: str,
    started_at: datetime,
    finished_at: datetime,
    duration_ms: int,
    response: LLMResponse | None,
    error: str | None,
) -> None:
    run = AgentRun(
        task_type=task_type.value,
        provider=provider,
        model=model,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        input_tokens=response.input_tokens if response else None,
        output_tokens=response.output_tokens if response else None,
        cost_usd=response.cost_usd if response else None,
        error=error,
    )
    session.add(run)
    session.commit()


async def route_to_model(
    task_type: TaskType,
    prompt: str,
    *,
    bypass_budget: bool = False,
    session_factory: Callable[[], Session] | None = None,
    **client_kwargs,
) -> LLMResponse:
    """Dispatch a prompt to the model bound to `task_type`.

    `bypass_budget=True` skips the monthly cap — use ONLY for critical paths
    (kill-switch reasoning, halt analysis). All other callers must respect
    the budget.

    `session_factory` is injectable for tests; production callers omit it.
    """
    provider, model = MODEL_MAP[task_type]
    dispatcher = _DISPATCH[provider]

    factory = session_factory or make_session_factory()
    started_at = datetime.now(UTC)
    started_perf = time.perf_counter()

    with factory() as session:
        try:
            check_budget(session, bypass_budget=bypass_budget)
        except RouterError as e:
            finished_at = datetime.now(UTC)
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            _persist_run(
                session,
                task_type=task_type,
                provider=provider,
                model=model,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                response=None,
                error=f"{type(e).__name__}: {e}",
            )
            raise

    response: LLMResponse | None = None
    error_msg: str | None = None
    try:
        response = await dispatcher(prompt, model=model, **client_kwargs)
        return response
    except RouterError as e:
        error_msg = f"{type(e).__name__}: {e}"
        raise
    except Exception as e:  # noqa: BLE001 — any unknown failure must still be logged
        error_msg = f"Unhandled: {type(e).__name__}: {e}"
        raise ProviderError(error_msg) from e
    finally:
        finished_at = datetime.now(UTC)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        with factory() as session:
            _persist_run(
                session,
                task_type=task_type,
                provider=provider,
                model=model,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                response=response,
                error=error_msg,
            )
        log.info(
            "llm_call",
            task_type=task_type.value,
            provider=provider,
            model=model,
            duration_ms=duration_ms,
            ok=response is not None,
            cost_usd=str(response.cost_usd) if response else None,
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            error=error_msg,
        )


def estimated_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Convenience for cost previews outside of an active call."""
    from hermes.llm.pricing import calculate_cost

    return calculate_cost(model, input_tokens, output_tokens)
