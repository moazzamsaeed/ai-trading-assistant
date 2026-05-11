"""Google Gemini client via the modern `google-genai` SDK (async).

`google-genai` replaced the older `google-generativeai` package in 2025 and
is the recommended SDK as of 2026. Async calls go through `client.aio`.
"""

from __future__ import annotations

import time

from google import genai
from google.genai import errors as genai_errors
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from traderouter.config import get_settings
from traderouter.llm.pricing import calculate_cost
from traderouter.llm.types import AuthError, LLMResponse, ProviderError, RateLimitError
from traderouter.logging import get_logger

log = get_logger(__name__)

PROVIDER = "google"
DEFAULT_TIMEOUT_S = 30.0


def _client() -> genai.Client:
    return genai.Client(api_key=get_settings().google_api_key.get_secret_value())


async def _call_once(client: genai.Client, *, model: str, prompt: str):
    try:
        return await client.aio.models.generate_content(model=model, contents=prompt)
    except genai_errors.APIError as e:
        # google-genai raises APIError with a `.code` attribute (HTTP status).
        status = getattr(e, "code", None) or getattr(e, "status_code", None)
        if status in (401, 403):
            raise AuthError(f"google {status}: {e}") from e
        if status == 429:
            raise RateLimitError(f"google rate limit: {e}") from e
        raise ProviderError(f"google {status or 'unknown'}: {e}") from e
    except Exception as e:
        # Network errors, timeouts, etc.
        raise ProviderError(f"google transport error: {e}") from e


async def complete(prompt: str, *, model: str = "gemini-3.1-pro-preview") -> LLMResponse:
    """Single-turn generate_content call. Retries on rate-limit and 5xx."""
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
        resp = await wrapped(client, model=model, prompt=prompt)
    except RetryError as e:
        raise ProviderError(f"google exhausted retries: {e}") from e

    duration_ms = int((time.perf_counter() - started) * 1000)
    usage = resp.usage_metadata
    input_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    text = resp.text or ""
    return LLMResponse(
        text=text,
        provider=PROVIDER,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=calculate_cost(model, input_tokens, output_tokens),
        duration_ms=duration_ms,
    )
