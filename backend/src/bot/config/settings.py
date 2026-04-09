"""
Configuration loading and validation.

Precedence (highest wins):
  1. Environment variables  (e.g. KALSHI_API_KEY=...)
  2. config.yaml in the working directory
  3. Defaults defined in the model below

Usage:
    from bot.config.settings import get_settings
    cfg = get_settings()
    print(cfg.kalshi.base_url)

Secrets (API keys) should NEVER be in config.yaml. Use env vars:
    export KALSHI_API_KEY="..."
    export KALSHI_API_SECRET="..."
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-config models (plain Pydantic, not BaseSettings)
# ---------------------------------------------------------------------------


class KalshiConfig(BaseModel):
    """Kalshi exchange connectivity settings."""
    base_url: str = "https://trading-api.kalshi.com/trade-api/v2"
    ws_url: str = "wss://trading-api.kalshi.com/trade-api/ws/v2"
    api_key: str = ""          # Set via KALSHI__API_KEY env var
    api_secret: str = ""       # Set via KALSHI__API_SECRET env var
    # Request settings
    request_timeout_seconds: float = 10.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0


class DataFeedConfig(BaseModel):
    """How the bot consumes market data."""
    mode: str = "rest_poll"    # "rest_poll" | "websocket"
    poll_interval_seconds: float = 5.0
    tickers: list[str] = Field(default_factory=list)  # Markets to watch

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        allowed = {"rest_poll", "websocket"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v!r}")
        return v


class ExecutionConfig(BaseModel):
    """Execution engine settings."""
    mode: str = "paper"        # "paper" | "live"
    # Paper engine: how quickly simulated fills arrive
    paper_fill_delay_seconds: float = 0.1
    paper_slippage_cents: int = 1  # Simulated slippage in cents

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, v: str) -> str:
        allowed = {"paper", "live"}
        if v not in allowed:
            raise ValueError(f"mode must be one of {allowed}, got {v!r}")
        return v


class RiskConfig(BaseModel):
    """
    Hard limits enforced by the risk guard before any order is submitted.
    These are safety rails — adjust them conservatively.
    """
    # Position limits
    max_position_per_market: int = 50      # Max contracts in any single market
    max_total_contracts: int = 200         # Max contracts across all markets
    # Spending limits
    max_order_value_cents: int = 500       # Max cost of a single order ($5.00)
    max_daily_spend_cents: int = 5000      # Max total spend per day ($50.00)
    max_open_orders: int = 10              # Max simultaneous open orders
    # Kill switch
    max_daily_loss_cents: int = 2000       # Hard stop if daily loss exceeds $20
    # Signal quality gate (optional — set to 0.0 to disable)
    min_signal_confidence: float = 0.0    # TODO: set this once your strategy populates confidence


class StateConfig(BaseModel):
    """State persistence settings."""
    db_path: str = "data/state.db"        # SQLite database path
    checkpoint_interval_seconds: float = 60.0


class LoggingConfig(BaseModel):
    """Structured logging settings."""
    level: str = "INFO"                   # DEBUG | INFO | WARNING | ERROR
    format: str = "json"                  # "json" | "console"
    log_file: Optional[str] = None        # None = stdout only


class StrategyConfig(BaseModel):
    """Which strategy to run and its parameters."""
    strategy_id: str = "default"
    # strategy_class: str = "bot.strategy.my_strategy.MyStrategy"
    # Any additional params you want your strategy to read go here.
    # TODO: add your strategy-specific parameters below.
    params: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """
    Root configuration object. Populated from config.yaml + env vars.
    All sub-models can also be overridden via env vars using double-underscore
    notation: KALSHI__API_KEY, EXECUTION__MODE, RISK__MAX_DAILY_LOSS_CENTS, etc.
    """
    model_config = SettingsConfigDict(
        env_prefix="",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    kalshi: KalshiConfig = Field(default_factory=KalshiConfig)
    data_feed: DataFeedConfig = Field(default_factory=DataFeedConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)

    @property
    def is_paper_trading(self) -> bool:
        return self.execution.mode == "paper"


def _load_yaml(path: Path) -> dict:
    """Load a YAML config file. Returns empty dict if file doesn't exist."""
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings(config_path: str = "config.yaml") -> Settings:
    """
    Load and cache the application settings.
    Call get_settings.cache_clear() in tests to reload with different config.
    """
    yaml_values = _load_yaml(Path(config_path))
    return Settings(**yaml_values)
