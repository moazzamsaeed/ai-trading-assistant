"""Per-model pricing table and cost calculator.

USD per million tokens. Update when providers change prices. Every change
should land with the date it took effect in a comment so historic
`agent_runs.cost_usd` rows are explainable.

Pricing as of 2026-05-10. See docs/DECISIONS.md if/when this needs an entry.
"""

from __future__ import annotations

from decimal import Decimal

# Tiered Gemini pricing kicks in above 200k input tokens. The single-turn
# agents we run today never approach that, so we use the lower tier. If a
# call exceeds 200k input tokens, we under-cost it — guard at call site if
# that ever matters.
PRICING: dict[str, dict[str, Decimal]] = {
    # Anthropic — flat per-token. https://platform.claude.com/docs
    "claude-opus-4-7": {
        "input": Decimal("5.00"),
        "output": Decimal("25.00"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
    },
    "claude-haiku-4-5-20251001": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
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
    # DeepSeek — discounted prices in effect until 2026-05-31; standard
    # prices ($1.74 input / $3.48 output) resume after.
    # https://api-docs.deepseek.com/quick_start/pricing
    "deepseek-v4-pro": {
        "input": Decimal("0.435"),
        "output": Decimal("0.87"),
    },
    "deepseek-v4-flash": {
        "input": Decimal("0.14"),
        "output": Decimal("0.28"),
    },
}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Compute USD cost for a call. Returns Decimal('0') if the model is unknown.

    Unknown models log Decimal('0') rather than raising — we never want a
    pricing lookup miss to break a trading decision. A separate test ensures
    every model in the router's MODEL_MAP exists in PRICING.
    """
    rates = PRICING.get(model)
    if rates is None:
        return Decimal("0")

    one_million = Decimal("1000000")
    input_cost = (Decimal(input_tokens) / one_million) * rates["input"]
    output_cost = (Decimal(output_tokens) / one_million) * rates["output"]
    return input_cost + output_cost
