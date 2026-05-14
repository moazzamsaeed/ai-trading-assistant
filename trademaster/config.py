"""Settings loader.

Loads from `.env` via pydantic-settings. The `account_type` field is locked
to `"cash"` (D-001) — TradeMaster will refuse to start if anything else is set.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    trading_mode: Literal["paper", "live"] = "paper"
    account_type: Literal["cash"] = "cash"

    enable_iron_condor: bool = False
    directional_mode: Literal["aggressive", "selective"] = "selective"

    # Starting capital baseline for the directional flow. The actual
    # effective capital is computed dynamically — see trademaster/capital.py.
    # In paper mode, effective = this base + cumulative realized P&L (since
    # baseline_reset_at). In live mode, effective = account.equity directly.
    trading_capital_usd: Decimal = Field(default=Decimal("5000"), gt=0)

    # Baseline reset: if set, the dynamic-capital calc ignores all trades
    # closed before this UTC timestamp. Use to start fresh after major
    # strategy changes without losing the audit history. Set via .env:
    #   BASELINE_RESET_AT=2026-05-14T03:30:00Z
    baseline_reset_at: datetime | None = None

    # Daily loss limit: 15% of effective capital. Counts realized P&L (closed
    # trades today) + unrealized (open positions). When hit, trading halts
    # until the next calendar day (ET). Note: because capital itself shrinks
    # with today's realized losses, the actual halt point is base × pct/(1+pct)
    # ≈ $652 on a $5k account, not the nominal $750. Conservative by design.
    daily_loss_limit_pct: float = Field(default=0.15, gt=0, le=1.0)

    # Iron-condor-only legacy caps (risk_manager path). The directional flow
    # uses dynamic capital sizing in capital.py + scheduler.py and ignores
    # these. Keep them for the iron-condor strategist if/when it's re-enabled.
    max_position_size_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_concurrent_positions: int = Field(default=5, gt=0)
    max_options_contracts_per_trade: int = Field(default=5, gt=0)

    # Max total capital deployed across all open directional positions at once.
    # 20% of trading_capital_usd = $1,000 on a $5k account. There is no count
    # cap — concurrency is bounded by deployed dollars, not trade count.
    max_total_exposure_pct: float = Field(default=0.20, gt=0, le=1.0)

    monthly_llm_budget_usd: Decimal = Field(default=Decimal("100"), gt=0)

    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"

    anthropic_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    google_api_key: SecretStr = SecretStr("")

    discord_bot_token: SecretStr = SecretStr("")
    discord_guild_id: str = ""
    discord_channel_signals: str = ""
    discord_channel_trades: str = ""
    discord_channel_research: str = ""
    discord_channel_logs: str = ""
    discord_channel_commands: str = ""
    discord_channel_watchlist: str = ""

    database_url: str = "sqlite:///data/trademaster.db"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def daily_loss_limit_usd(self) -> Decimal:
        """Derived from daily_loss_limit_pct × trading_capital_usd."""
        return (self.trading_capital_usd * Decimal(str(self.daily_loss_limit_pct))).quantize(
            Decimal("0.01")
        )

    @field_validator("account_type", mode="before")
    @classmethod
    def _cash_only(cls, v: object) -> object:
        if isinstance(v, str):
            if v.lower() != "cash":
                raise ValueError(
                    "ACCOUNT_TYPE must be 'cash'. Margin and leverage are forbidden (D-001)."
                )
            return v.lower()
        return v

    def require_live_keys(self) -> None:
        """Fail fast if any provider key needed for runtime is missing.

        Called by the orchestrator at startup, not by tests.
        """
        missing = [
            name
            for name, value in {
                "ALPACA_API_KEY": self.alpaca_api_key,
                "ALPACA_API_SECRET": self.alpaca_api_secret,
                "ANTHROPIC_API_KEY": self.anthropic_api_key,
                "DEEPSEEK_API_KEY": self.deepseek_api_key,
                "GOOGLE_API_KEY": self.google_api_key,
                "DISCORD_BOT_TOKEN": self.discord_bot_token,
            }.items()
            if not value.get_secret_value()
        ]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)}"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
