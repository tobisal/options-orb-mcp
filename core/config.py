"""Central configuration with a hard paper/live safety gate.

Settings are read from environment variables and an optional ``.env`` file
(see ``.env.example``). The single most important rule enforced here: live
trading is impossible unless the operator has *both* set ``ACCOUNT_MODE=live``
*and* provided the explicit ``LIVE_TRADING_CONFIRM`` interlock string.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root (two levels up from this file: core/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[1]

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISK"


class AccountMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    """Runtime configuration for every component of the system."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- IBKR connection ---------------------------------------------------
    ibkr_host: str = Field(default="127.0.0.1", alias="IBKR_HOST")
    ibkr_port: int = Field(default=7497, alias="IBKR_PORT")
    ibkr_client_id: int = Field(default=17, alias="IBKR_CLIENT_ID")
    # Market data type: auto | live | delayed | frozen | delayed_frozen.
    # "auto" requests real-time and auto-falls back to free delayed data when the
    # account lacks a real-time subscription (ideal for paper accounts).
    ibkr_market_data_type: str = Field(default="auto", alias="IBKR_MARKET_DATA_TYPE")
    # Suppress ib_async's own connection logging (set true to debug connections).
    ibkr_verbose: bool = Field(default=False, alias="IBKR_VERBOSE")
    # After a failed connect, skip real socket attempts for this many seconds.
    ibkr_retry_cooldown: float = Field(default=30.0, alias="IBKR_RETRY_COOLDOWN")

    # --- Safety gate -------------------------------------------------------
    account_mode: AccountMode = Field(default=AccountMode.PAPER, alias="ACCOUNT_MODE")
    live_trading_confirm: str = Field(default="", alias="LIVE_TRADING_CONFIRM")

    # --- Capital & risk ----------------------------------------------------
    account_currency: str = Field(default="GBP", alias="ACCOUNT_CURRENCY")
    starting_capital: float = Field(default=1000.0, alias="STARTING_CAPITAL")
    # 5% by default: one US-options defined-risk spread (100x multiplier) risks
    # roughly GBP 30-40, so a 2% cap makes even a single contract untradeable at
    # GBP 1000. Lower this once your capital grows.
    max_risk_per_trade: float = Field(default=0.05, alias="MAX_RISK_PER_TRADE")
    max_daily_loss: float = Field(default=0.10, alias="MAX_DAILY_LOSS")
    # Global concurrent cap. Default 9 = up to 3 windows x 3 trades per window.
    max_open_positions: int = Field(default=9, alias="MAX_OPEN_POSITIONS")
    # Concurrent open trades allowed per session window (Asia/London/New York).
    max_open_positions_per_window: int = Field(default=3, alias="MAX_OPEN_POSITIONS_PER_WINDOW")

    # --- Strategy defaults -------------------------------------------------
    default_symbol: str = Field(default="MES", alias="DEFAULT_SYMBOL")
    default_target_r: float = Field(default=1.5, alias="DEFAULT_TARGET_R")
    # orb = options vertical ORB; mes_5orb = MES futures break/retest.
    entry_strategy: str = Field(default="mes_5orb", alias="ENTRY_STRATEGY")

    # --- Storage -----------------------------------------------------------
    db_path: str = Field(default="data/trades.db", alias="DB_PATH")

    # --- Auto-trade resume -------------------------------------------------
    # When true, dashboard always starts the paper auto-trader on boot (even if
    # it was stopped before the last restart). Otherwise it resumes only when
    # the last Start left enabled=true in data/autotrade.json.
    auto_trade_autostart: bool = Field(default=False, alias="AUTO_TRADE_AUTOSTART")
    auto_trade_symbol: str | None = Field(default=None, alias="AUTO_TRADE_SYMBOL")
    auto_trade_window: str | None = Field(default=None, alias="AUTO_TRADE_WINDOW")
    auto_trade_interval: float | None = Field(default=None, alias="AUTO_TRADE_INTERVAL")
    auto_trade_demo: bool | None = Field(default=None, alias="AUTO_TRADE_DEMO")

    # --- Dashboard bind ----------------------------------------------------
    # Local default is loopback. Docker sets DASHBOARD_HOST=0.0.0.0.
    dashboard_host: str = Field(default="127.0.0.1", alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8787, alias="DASHBOARD_PORT")

    # --- Discord remote control --------------------------------------------
    discord_bot_token: str = Field(default="", alias="DISCORD_BOT_TOKEN")
    discord_guild_id: str = Field(default="", alias="DISCORD_GUILD_ID")
    discord_allowed_user_ids: str = Field(default="", alias="DISCORD_ALLOWED_USER_IDS")
    discord_log_channel_id: str = Field(default="", alias="DISCORD_LOG_CHANNEL_ID")
    discord_dashboard_url: str = Field(
        default="http://127.0.0.1:8787", alias="DISCORD_DASHBOARD_URL"
    )
    discord_log_verbose: bool = Field(default=False, alias="DISCORD_LOG_VERBOSE")

    @field_validator("max_risk_per_trade", "max_daily_loss")
    @classmethod
    def _fraction_between_zero_and_one(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("risk fractions must be in the interval (0, 1]")
        return v

    # ---------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        """True only when the mode is live AND the interlock phrase matches."""
        return (
            self.account_mode is AccountMode.LIVE
            and self.live_trading_confirm == LIVE_CONFIRM_PHRASE
        )

    @property
    def live_requested_but_unconfirmed(self) -> bool:
        """Live mode was selected but the confirmation interlock is missing."""
        return (
            self.account_mode is AccountMode.LIVE
            and self.live_trading_confirm != LIVE_CONFIRM_PHRASE
        )

    @property
    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else (REPO_ROOT / p)

    def trading_environment(self) -> str:
        return "LIVE" if self.is_live else "PAPER"

    def discord_allowlist(self) -> set[int]:
        ids: set[int] = set()
        for part in self.discord_allowed_user_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
