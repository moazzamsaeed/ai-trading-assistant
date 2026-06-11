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

    # Event-day blackout (NFP/CPI/FOMC). Disabled 2026-06-05 to let the LLM
    # trade event days during the paper-validation phase — we want data on every
    # regime, including high-volatility catalyst days, before deciding whether
    # the blackout earns its keep. Flip to True to restore the skip.
    enable_event_blackout: bool = False

    # Per-trade catastrophic-loss cap, as a fraction of EFFECTIVE capital
    # (auto-scales with the account). Caps deployed premium so qty × premium ×
    # 100 ≤ pct × capital. Was a fixed $500; converted to 10% on 2026-06-05 when
    # capital moved to $25k so position size scales with the budget.
    max_loss_per_trade_pct: float = Field(default=0.10, gt=0, le=1.0)

    # Conviction-scaled sizing (fix C, 2026-06-09). MEDIUM-conviction trades
    # deploy this fraction of the per-trade budget that a HIGH trade would —
    # weaker edge gets less capital. Trade #60 (MEDIUM, RSI not confirming) lost
    # full size; HIGH puts the same day all won.
    medium_conviction_size_mult: float = Field(default=0.5, gt=0, le=1.0)
    # Extra downsize when RSI-9 does not confirm the trade direction at entry
    # (a put with RSI ≥ 50, or a call with RSI ≤ 50 — i.e. momentum is not yet
    # on our side). Multiplies on top of the conviction multiplier.
    weak_rsi_size_mult: float = Field(default=0.5, gt=0, le=1.0)

    # Re-entry freshness gate (fix B, 2026-06-09; tightened 2026-06-10). Once
    # this many consecutive same-direction trades have opened today, a further
    # same-direction entry must look like a FRESH leg — not a late chase of an
    # exhausted move. The 3rd same-direction trade was 0-for-4 (−$5,198): it
    # always entered as the morning move was spent and got caught by the bounce
    # (peaks of 0–10% vs 16–242% for trades 1–2). Default 2 → the 3rd+ entry is
    # gated. A direction flip resets the streak. 0 = disabled.
    reentry_same_direction_limit: int = Field(default=2, ge=0)
    # A gated re-entry is allowed only if EITHER it's pulled back ≥ this fraction
    # of the day's range from the move's extreme (consolidation → room for a new
    # leg), OR it's a fresh break to a new extreme with volume ≥ the floor below.
    reentry_pullback_range_frac: float = Field(default=0.30, gt=0, le=1.0)
    reentry_fresh_volume_min: float = Field(default=1.5, gt=0)

    # 0DTE indicator-independent early-cut (fix, 2026-06-11). Early in the session
    # RSI/EMA/volume haven't warmed up, so the ≥2-signal thesis gate can't fire
    # and the LLM mismanages 0DTE losers. On a 0DTE position past this loss with
    # price through VWAP against it, cut immediately — no LLM, no indicator wait.
    # (#64: a 0DTE call rode to −50% / −$1,490 while the LLM held it.)
    zdte_early_loss_cut_pct: float = Field(default=0.25, gt=0, le=1.0)

    # Evidence-based chop filter (fix, 2026-06-11). After this many "failed
    # breakouts" today — entries that peaked below chop_failed_peak_pct and
    # closed at a loss (immediate reversals) — pause ALL new directional entries
    # for chop_pause_minutes. A cluster of failed breakouts means the regime is
    # choppy and momentum entries keep getting trapped (today #64/#65/#66 each
    # peaked <10% then reversed). 0 = disabled. Pause measured from the most
    # recent failed-breakout close.
    chop_failed_breakout_limit: int = Field(default=2, ge=0)
    chop_failed_peak_pct: float = Field(default=10.0, gt=0)
    chop_pause_minutes: int = Field(default=45, ge=0)

    # The trailing stop trails CONTINUOUSLY at (peak − this gap) across the
    # whole in-profit range (once past the lowest ladder tier), so the stop
    # always sits within `gap` of the high-water mark. 0.10 → a +70% peak locks
    # +60%, +200% locks +190%, etc. (tightened 2026-06-08 from 0.20: trade #51
    # peaked +70% but the discrete ladder only locked +20%, giving back ~$1,200).
    trailing_stop_trail_gap_pct: float = Field(default=0.10, gt=0, le=1.0)

    # Scale-out / trailing-stop ladder override. Empty = use the code default
    # (DEFAULT_TRAILING_STOP_LEVELS in exit_monitor). Set a JSON array of
    # [trigger_pct, lock_pct, sell_frac] to A/B a different ladder without a
    # code change, e.g. '[[120,0.75,0],[80,0.45,0],[50,0.20,0.25],[25,0.08,0.25]]'.
    trailing_stop_levels: str = ""

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

    # Weekly loss limit: 25% of effective capital Mon–Sun (ET). Prevents a
    # string of bad days compounding into a full account wipeout. Halts trading
    # for the remainder of the week when hit.
    weekly_loss_limit_pct: float = Field(default=0.25, gt=0, le=1.0)

    # Tiered daily trade caps. **0 = UNLIMITED** (no per-day count cap) — set as
    # the default 2026-06-07: with capital at $25k and risk bounded by the daily
    # loss limit (15%), per-trade cap (10%), 30% exposure cap, and the per-ticker
    # / per-action cooldowns, the trade-count cap was the binding throttle and is
    # no longer wanted. Set a positive number to re-impose a cap.
    max_trades_per_day: int = Field(default=0, ge=0)        # 0 = unlimited
    max_medium_trades_per_day: int = Field(default=0, ge=0)  # 0 = unlimited

    # No entries before this ET time (opening volatility) or after no_entry_after_et.
    # Format: "HH:MM" 24-hour ET.
    no_entry_before_et: str = Field(default="10:00")
    no_entry_after_et: str = Field(default="14:30")

    # Max bid/ask spread as a fraction of mid price. Options with wider spreads
    # are illiquid — you pay too much slippage entering and exiting.
    # 0.50 = reject any option where spread > 50% of mid (e.g. bid=0.80, ask=1.20).
    max_bid_ask_spread_pct: float = Field(default=0.50, gt=0, le=1.0)

    # Iron-condor-only legacy caps (risk_manager path). The directional flow
    # uses dynamic capital sizing in capital.py + scheduler.py and ignores
    # these. Keep them for the iron-condor strategist if/when it's re-enabled.
    max_position_size_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_concurrent_positions: int = Field(default=5, gt=0)
    max_options_contracts_per_trade: int = Field(default=5, gt=0)

    # Max total capital deployed across all open directional positions at once.
    # 30% of effective capital — the full remaining budget is used per trade
    # (no per-trade fraction). With SPY-only focus, concentration is the goal.
    max_total_exposure_pct: float = Field(default=0.30, gt=0, le=1.0)

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
