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

from trademaster.db import AgentRun, make_session_factory
from trademaster.llm import anthropic_client, deepseek_client, google_client
from trademaster.llm.budget import check_budget
from trademaster.llm.types import LLMResponse, ProviderError, RouterError
from trademaster.logging import get_logger

log = get_logger(__name__)


class TaskType(StrEnum):
    ORCHESTRATE = "orchestrate"
    PRE_MARKET_RESEARCH = "pre_market_research"
    INTRADAY_SCAN = "intraday_scan"
    FORMAT_ALERT = "format_alert"
    OPTIONS_STRATEGY = "options_strategy"
    CRYPTO_REGIME = "crypto_regime"
    EXECUTION_DECISION = "execution_decision"
    EXIT_DECISION = "exit_decision"
    # Directional entry decision — split out from INTRADAY_SCAN 2026-06-15 so the
    # entry model can be upgraded independently of the news scan / market
    # analysis (which stay on DeepSeek). Evidence: scripts/replay_model_comparison
    # showed DeepSeek whipsaws direction in chop (CCPCPP on 06-11's −$3.3k day)
    # while Sonnet/Opus hold a single direction (0 flips). See
    # memory/project_entry_model_replay.md. Reversible — flip back by pointing
    # DIRECTIONAL_ENTRY at deepseek-v4-flash below (user toggles this).
    DIRECTIONAL_ENTRY = "directional_entry"


MODEL_MAP: dict[TaskType, tuple[str, str]] = {
    TaskType.ORCHESTRATE: ("anthropic", "claude-opus-4-7"),
    # Primary swapped google→anthropic 2026-06-24: Gemini 2.5 Pro 503'd at ~8 AM
    # ET three mornings straight (sustained "high demand"), failing the briefing
    # daily. Claude Sonnet is reliable for this once-daily long-form synthesis;
    # Gemini is now the fallback. Pair with the long timeout in premarket.py.
    TaskType.PRE_MARKET_RESEARCH: ("anthropic", "claude-sonnet-4-6"),
    TaskType.INTRADAY_SCAN: ("deepseek", "deepseek-v4-flash"),
    # Directional ENTRY runs on Sonnet 4.6 (2026-06-15). To revert to the prior
    # config, change this one line back to ("deepseek", "deepseek-v4-flash").
    TaskType.DIRECTIONAL_ENTRY: ("anthropic", "claude-sonnet-4-6"),
    TaskType.FORMAT_ALERT: ("deepseek", "deepseek-v4-flash"),
    TaskType.OPTIONS_STRATEGY: ("deepseek", "deepseek-v4-pro"),
    TaskType.CRYPTO_REGIME: ("deepseek", "deepseek-v4-pro"),
    TaskType.EXECUTION_DECISION: ("anthropic", "claude-opus-4-7"),
    TaskType.EXIT_DECISION: ("anthropic", "claude-sonnet-4-6"),
}

# Fallback providers used when the primary raises ProviderError (timeout, 5xx).
# Only defined for tasks where missing a scan has real cost — not for
# low-stakes formatting tasks.
FALLBACK_MAP: dict[TaskType, tuple[str, str]] = {
    TaskType.INTRADAY_SCAN: ("anthropic", "claude-haiku-4-5-20251001"),
    # Entry runs on Sonnet (Anthropic); fall back cross-provider to the prior
    # entry model so an Anthropic outage doesn't blind the entry scan.
    TaskType.DIRECTIONAL_ENTRY: ("deepseek", "deepseek-v4-flash"),
    TaskType.OPTIONS_STRATEGY: ("anthropic", "claude-haiku-4-5-20251001"),
    # Premarket fires once at 8 AM ET — no fallback means a missed briefing for
    # the whole day. Gemini is the fallback now that Anthropic is primary (swap
    # of 2026-06-24); if Anthropic is down, Gemini still produces the briefing.
    TaskType.PRE_MARKET_RESEARCH: ("google", "gemini-2.5-pro"),
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
    except ProviderError as e:
        error_msg = f"{type(e).__name__}: {e}"
        # Automatic fallback: if this task has a fallback provider defined,
        # retry immediately rather than letting the scan miss entirely.
        fallback = FALLBACK_MAP.get(task_type)
        if fallback:
            fb_provider, fb_model = fallback
            log.warning(
                "llm_fallback_triggered",
                task_type=task_type.value,
                primary_provider=provider,
                primary_model=model,
                fallback_provider=fb_provider,
                fallback_model=fb_model,
                primary_error=error_msg,
            )
            fb_dispatcher = _DISPATCH[fb_provider]
            fb_started = datetime.now(UTC)
            fb_perf = time.perf_counter()
            fb_response: LLMResponse | None = None
            fb_error: str | None = None
            try:
                fb_response = await fb_dispatcher(prompt, model=fb_model, **client_kwargs)
                return fb_response
            except Exception as fb_e:  # noqa: BLE001
                fb_error = f"{type(fb_e).__name__}: {fb_e}"
                raise ProviderError(f"Primary and fallback both failed. Primary: {error_msg} | Fallback: {fb_error}") from fb_e
            finally:
                fb_finished = datetime.now(UTC)
                fb_ms = int((time.perf_counter() - fb_perf) * 1000)
                with factory() as session:
                    _persist_run(
                        session,
                        task_type=task_type,
                        provider=fb_provider,
                        model=fb_model,
                        started_at=fb_started,
                        finished_at=fb_finished,
                        duration_ms=fb_ms,
                        response=fb_response,
                        error=fb_error,
                    )
                log.info(
                    "llm_call",
                    task_type=task_type.value,
                    provider=fb_provider,
                    model=fb_model,
                    duration_ms=fb_ms,
                    ok=fb_response is not None,
                    cost_usd=str(fb_response.cost_usd) if fb_response else None,
                    input_tokens=fb_response.input_tokens if fb_response else None,
                    output_tokens=fb_response.output_tokens if fb_response else None,
                    error=fb_error,
                    fallback=True,
                )
        raise
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
    from trademaster.llm.pricing import calculate_cost

    return calculate_cost(model, input_tokens, output_tokens)
