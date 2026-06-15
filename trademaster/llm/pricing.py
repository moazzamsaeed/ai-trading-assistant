"""Per-model pricing table and cost calculator.

USD per million tokens. Update when providers change prices. Every change
should land with the date it took effect in a comment so historic
`agent_runs.cost_usd` rows are explainable.

Pricing as of 2026-05-10; Haiku 4.5 rate corrected and DeepSeek rates
re-verified 2026-06-04. See docs/DECISIONS.md if/when this needs an entry.
"""

from __future__ import annotations

import re
from decimal import Decimal

from trademaster.logging import get_logger

log = get_logger(__name__)

# Tiered Gemini pricing kicks in above 200k input tokens. The single-turn
# agents we run today never approach that, so we use the lower tier. If a
# call exceeds 200k input tokens, we under-cost it — guard at call site if
# that ever matters.
PRICING: dict[str, dict[str, Decimal]] = {
    # Anthropic — flat per-token. https://platform.claude.com/docs
    "claude-opus-4-8": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
    },
    "claude-opus-4-7": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
    },
    # Haiku 4.5 list price (2026-06-04 fix): was $0.80/$4.00 — those were the
    # older Haiku 3.5 rates and under-counted every Haiku call. Haiku 4.5 is
    # $1.00 input / $5.00 output per million.
    "claude-haiku-4-5-20251001": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
    },
    # Google — tier 1 (≤200k input). Tier 2 doubles roughly.
    # https://ai.google.dev/gemini-api/docs/models
    "gemini-3.1-pro-preview": {
        "input": Decimal("2.00"),
        "output": Decimal("12.00"),
    },
    # Gemini 2.5 Pro — stable production model used as the reliable
    # alternative to 3.1-pro-preview (D-016). Tier 1 (≤200k input).
    "gemini-2.5-pro": {
        "input": Decimal("1.25"),
        "output": Decimal("10.00"),
    },
    # DeepSeek V4 — current standard list prices (cache-miss input), re-verified
    # 2026-06-04 against https://api-docs.deepseek.com/quick_start/pricing.
    # No time-limited discount is active; these ARE the standard rates (the
    # earlier note about a 2026-05-31 discount expiry / jump to $1.74/$3.48 was
    # a stale V3-era assumption — V4 pricing is genuinely this low). Cache-hit
    # input is far cheaper (~$0.003/M) but we don't track cache hits separately.
    "deepseek-v4-pro": {
        "input": Decimal("0.435"),
        "output": Decimal("0.87"),
    },
    "deepseek-v4-flash": {
        "input": Decimal("0.14"),
        "output": Decimal("0.28"),
    },
}


def _date_suffix_re() -> re.Pattern[str]:
    return re.compile(r"-\d{8}$")


def _lookup_rates(model: str) -> dict[str, Decimal] | None:
    """Resolve pricing for a model, tolerating dated/undated name variants.

    The API sometimes returns a model id with a trailing `-YYYYMMDD` snapshot
    date (or without it) that doesn't match the table key verbatim — which used
    to silently price the call at $0 (the 2026-05-14/15 calls). Match exact
    first, then normalise by stripping the date suffix on both sides.
    """
    rates = PRICING.get(model)
    if rates is not None:
        return rates
    suffix = _date_suffix_re()
    base = suffix.sub("", model)
    for key, r in PRICING.items():
        if suffix.sub("", key) == base:
            return r
    return None


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Compute USD cost for a call. Returns Decimal('0') if the model is unknown.

    Unknown models log Decimal('0') rather than raising — we never want a
    pricing lookup miss to break a trading decision. But it now logs a WARNING
    so a silent $0 (which under-counts spend, as it did 2026-05-14/15) is
    visible instead of vanishing. A separate test ensures every model in the
    router's MODEL_MAP exists in PRICING.
    """
    rates = _lookup_rates(model)
    if rates is None:
        log.warning(
            "pricing_model_unknown",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            note="cost logged as $0 — add this model to PRICING",
        )
        return Decimal("0")

    one_million = Decimal("1000000")
    input_cost = (Decimal(input_tokens) / one_million) * rates["input"]
    output_cost = (Decimal(output_tokens) / one_million) * rates["output"]
    return input_cost + output_cost
