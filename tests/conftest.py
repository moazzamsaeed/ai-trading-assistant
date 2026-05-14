"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Tests should not silently inherit production-only .env values
    (e.g. BASELINE_RESET_AT, which would filter out test-seeded trades).

    We disable .env loading entirely; tests that need specific values
    use monkeypatch.setenv() + get_settings.cache_clear(), which still
    works because actual OS env vars take precedence over the (now
    disabled) .env file.
    """
    from trademaster import config as _cfg
    # SettingsConfigDict is a TypedDict-style dict — directly suppress env_file
    _cfg.Settings.model_config["env_file"] = None
    monkeypatch.delenv("BASELINE_RESET_AT", raising=False)
    _cfg.get_settings.cache_clear()
    yield
    # Restore for any process-level callers (defensive — tests shouldn't reuse)
    _cfg.Settings.model_config["env_file"] = ".env"
    _cfg.get_settings.cache_clear()
