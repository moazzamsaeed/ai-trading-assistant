"""Shared LLM response and error types.

Every provider client returns `LLMResponse` on success or raises a
`RouterError` subclass on failure. The router catches these uniformly and
logs them to `agent_runs`.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class LLMResponse(BaseModel):
    """Normalized response across all providers."""

    model_config = ConfigDict(frozen=True)

    text: str
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    duration_ms: int = Field(ge=0)


class RouterError(Exception):
    """Base class for all router/provider errors."""


class AuthError(RouterError):
    """API key missing or invalid. Not retryable."""


class RateLimitError(RouterError):
    """Rate-limit exhausted after retries."""


class ProviderError(RouterError):
    """5xx, timeout, malformed response, or other server-side failure after retries."""


class BudgetExceededError(RouterError):
    """Monthly LLM spend would exceed cap. Bypass with `bypass_budget=True`."""
