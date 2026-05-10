"""Routes a task type to the correct LLM provider/model.

Hermes calls `route_to_model(task_type, prompt, ...)` and gets back the
response. Provider SDKs are called directly here; no proxy layer.
"""

from __future__ import annotations

from enum import StrEnum


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
    TaskType.PRE_MARKET_RESEARCH: ("google", "gemini-3.1-pro"),
    TaskType.INTRADAY_SCAN: ("deepseek", "deepseek-v4-flash"),
    TaskType.FORMAT_ALERT: ("deepseek", "deepseek-v4-flash"),
    TaskType.OPTIONS_STRATEGY: ("deepseek", "deepseek-v4-pro"),
    TaskType.CRYPTO_REGIME: ("deepseek", "deepseek-v4-pro"),
    TaskType.EXECUTION_DECISION: ("anthropic", "claude-opus-4-7"),
}


def route_to_model(task_type: TaskType, prompt: str) -> str:
    raise NotImplementedError("Phase 1 — to be implemented")
