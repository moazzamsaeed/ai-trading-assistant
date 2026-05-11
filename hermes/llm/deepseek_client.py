"""DeepSeek client via the OpenAI-compatible `openai` SDK (async).

DeepSeek exposes an OpenAI-compatible Chat Completions endpoint at
api.deepseek.com — we point `AsyncOpenAI` at that base_url.
"""

from __future__ import annotations

import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
)
from openai import RateLimitError as OpenAIRateLimitError
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from hermes.config import get_settings
from hermes.llm.pricing import calculate_cost
from hermes.llm.types import AuthError, LLMResponse, ProviderError, RateLimitError
from hermes.logging import get_logger

log = get_logger(__name__)

PROVIDER = "deepseek"
BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_TOKENS = 4096


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=get_settings().deepseek_api_key.get_secret_value(),
        base_url=BASE_URL,
        timeout=DEFAULT_TIMEOUT_S,
    )


async def _call_once(client: AsyncOpenAI, *, model: str, prompt: str, max_tokens: int):
    try:
        return await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except AuthenticationError as e:
        raise AuthError(f"deepseek auth failed: {e}") from e
    except OpenAIRateLimitError as e:
        raise RateLimitError(f"deepseek rate limit: {e}") from e
    except (APITimeoutError, APIConnectionError) as e:
        raise ProviderError(f"deepseek transport error: {e}") from e
    except APIStatusError as e:
        if e.status_code in (401, 403):
            raise AuthError(f"deepseek {e.status_code}: {e}") from e
        raise ProviderError(f"deepseek {e.status_code}: {e}") from e


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMResponse:
    """Single-turn chat call. `model` is `deepseek-v4-pro` or `deepseek-v4-flash`."""
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
        resp = await wrapped(client, model=model, prompt=prompt, max_tokens=max_tokens)
    except RetryError as e:
        raise ProviderError(f"deepseek exhausted retries: {e}") from e

    duration_ms = int((time.perf_counter() - started) * 1000)
    text = resp.choices[0].message.content or ""
    usage = resp.usage
    return LLMResponse(
        text=text,
        provider=PROVIDER,
        model=model,
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        cost_usd=calculate_cost(model, usage.prompt_tokens, usage.completion_tokens),
        duration_ms=duration_ms,
    )
