"""Anthropic Messages API client (async, single-turn).

Maps `anthropic` SDK exceptions to our typed RouterError hierarchy and
retries transient failures with exponential backoff.
"""

from __future__ import annotations

import time

import anthropic
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from trademaster.config import get_settings
from trademaster.llm.pricing import calculate_cost
from trademaster.llm.types import AuthError, LLMResponse, ProviderError, RateLimitError
from trademaster.logging import get_logger

log = get_logger(__name__)

PROVIDER = "anthropic"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 4096


def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=get_settings().anthropic_api_key.get_secret_value(),
        timeout=DEFAULT_TIMEOUT_S,
    )


async def _call_once(
    client: anthropic.AsyncAnthropic,
    *,
    model: str,
    prompt: str,
    max_tokens: int,
) -> anthropic.types.Message:
    try:
        return await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise AuthError(f"anthropic auth failed: {e}") from e
    except anthropic.RateLimitError as e:
        raise RateLimitError(f"anthropic rate limit: {e}") from e
    except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
        raise ProviderError(f"anthropic transport error: {e}") from e
    except anthropic.APIStatusError as e:
        # 4xx auth-adjacent → AuthError; 5xx → ProviderError; other 4xx → ProviderError
        if e.status_code in (401, 403):
            raise AuthError(f"anthropic {e.status_code}: {e}") from e
        raise ProviderError(f"anthropic {e.status_code}: {e}") from e


async def complete(
    prompt: str,
    *,
    model: str = "claude-opus-4-7",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMResponse:
    """Single-turn Messages API call. Retries on rate-limit and 5xx."""
    client = _client()
    started = time.perf_counter()

    retrying = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((RateLimitError, ProviderError)),
        reraise=True,
    )
    wrapped = retrying(_call_once)

    try:
        msg = await wrapped(client, model=model, prompt=prompt, max_tokens=max_tokens)
    except RetryError as e:
        raise ProviderError(f"anthropic exhausted retries: {e}") from e

    duration_ms = int((time.perf_counter() - started) * 1000)
    text = msg.content[0].text if msg.content else ""
    return LLMResponse(
        text=text,
        provider=PROVIDER,
        model=model,
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        cost_usd=calculate_cost(model, msg.usage.input_tokens, msg.usage.output_tokens),
        duration_ms=duration_ms,
    )
