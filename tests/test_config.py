"""Settings validation tests.

The cash-account guard (D-001) is the single most important check in this
file — Hermes must refuse to start on any non-cash account type.
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError


def _fresh_settings(monkeypatch, **env):
    """Reload the settings module with a controlled env."""
    for k in (
        "TRADING_MODE",
        "ACCOUNT_TYPE",
        "DAILY_LOSS_LIMIT_USD",
        "MAX_POSITION_SIZE_USD",
        "MAX_CONCURRENT_POSITIONS",
        "MAX_OPTIONS_CONTRACTS_PER_TRADE",
        "ALPACA_API_KEY",
        "ANTHROPIC_API_KEY",
        "DATABASE_URL",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    import trademaster.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    return cfg


def test_defaults_load_without_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env in cwd
    cfg = _fresh_settings(monkeypatch)
    s = cfg.get_settings()
    assert s.trading_mode == "paper"
    assert s.account_type == "cash"
    assert s.daily_loss_limit_usd > 0
    assert s.max_concurrent_positions > 0


def test_account_type_margin_is_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = _fresh_settings(monkeypatch, ACCOUNT_TYPE="margin")
    with pytest.raises(ValidationError) as exc:
        cfg.get_settings()
    assert "cash" in str(exc.value).lower()


def test_account_type_cash_uppercase_accepted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = _fresh_settings(monkeypatch, ACCOUNT_TYPE="CASH")
    s = cfg.get_settings()
    assert s.account_type == "cash"


def test_negative_loss_limit_is_rejected(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = _fresh_settings(monkeypatch, DAILY_LOSS_LIMIT_USD="-1")
    with pytest.raises(ValidationError):
        cfg.get_settings()


def test_require_live_keys_lists_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = _fresh_settings(monkeypatch)
    s = cfg.get_settings()
    with pytest.raises(RuntimeError) as exc:
        s.require_live_keys()
    msg = str(exc.value)
    for key in (
        "ALPACA_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_API_KEY",
        "DISCORD_BOT_TOKEN",
    ):
        assert key in msg
