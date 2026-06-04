"""Pricing table and cost-calculation tests."""

from __future__ import annotations

from decimal import Decimal

from trademaster.llm.pricing import PRICING, calculate_cost
from trademaster.router import MODEL_MAP


def test_every_routed_model_has_pricing():
    """Drift guard: every model in MODEL_MAP must exist in PRICING."""
    routed_models = {model for _, model in MODEL_MAP.values()}
    missing = routed_models - PRICING.keys()
    assert not missing, f"Models in MODEL_MAP without pricing: {missing}"


def test_unknown_model_returns_zero():
    assert calculate_cost("nonexistent-model", 1000, 1000) == Decimal("0")


def test_anthropic_opus_cost_math():
    # $5/M input, $25/M output → 1M in + 1M out = $30
    cost = calculate_cost("claude-opus-4-7", 1_000_000, 1_000_000)
    assert cost == Decimal("30.00")


def test_gemini_cost_math():
    # $2/M input, $12/M output → 500k in + 250k out = $1 + $3 = $4
    cost = calculate_cost("gemini-3.1-pro-preview", 500_000, 250_000)
    assert cost == Decimal("4.00")


def test_deepseek_pro_cost_math():
    # $0.435/M input, $0.87/M output → 100k in + 50k out = $0.0435 + $0.0435 = $0.087
    cost = calculate_cost("deepseek-v4-pro", 100_000, 50_000)
    assert cost == Decimal("0.087")


def test_deepseek_flash_cost_math():
    # $0.14/M input, $0.28/M output → 1M in + 1M out = $0.42
    cost = calculate_cost("deepseek-v4-flash", 1_000_000, 1_000_000)
    assert cost == Decimal("0.42")


def test_zero_tokens_zero_cost():
    assert calculate_cost("claude-opus-4-7", 0, 0) == Decimal("0")


def test_haiku_45_cost_math_uses_corrected_rate():
    # Haiku 4.5 = $1/M input, $5/M output (was wrongly $0.80/$4.00). 1M+1M = $6.
    cost = calculate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert cost == Decimal("6.00")


def test_dated_and_undated_model_names_resolve_same():
    # API may return the id without the -YYYYMMDD snapshot suffix; must not $0.
    dated = calculate_cost("claude-haiku-4-5-20251001", 100_000, 50_000)
    undated = calculate_cost("claude-haiku-4-5", 100_000, 50_000)
    assert dated == undated
    assert undated > Decimal("0"), "undated alias must not silently price $0"


def test_unknown_model_warns(monkeypatch):
    """Unknown model returns $0 but must WARN (not silently vanish)."""
    from trademaster.llm import pricing as _pricing

    warnings: list = []
    monkeypatch.setattr(_pricing.log, "warning", lambda *a, **k: warnings.append((a, k)))
    cost = _pricing.calculate_cost("totally-made-up-model", 1000, 2000)
    assert cost == Decimal("0")
    assert warnings and warnings[0][0][0] == "pricing_model_unknown"
