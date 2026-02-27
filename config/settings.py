"""
Central configuration for the trading bot.

Uses Pydantic Settings for validation and environment variable loading.
All parameters can be overridden via environment variables or .env file.

Architecture note:
    We call load_dotenv() at module level so that .env values are injected
    into os.environ BEFORE any BaseSettings subclass is instantiated.
    This guarantees that nested BaseSettings classes with env_prefix
    correctly resolve their variables from the .env file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ── Project paths ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Inject .env into os.environ so nested BaseSettings classes can find them
load_dotenv(PROJECT_ROOT / ".env", override=False)


# ── Exchange ──────────────────────────────────────────────────
class ExchangeSettings(BaseSettings):
    """Bybit exchange connection settings."""

    model_config = SettingsConfigDict(env_prefix="BYBIT_")

    api_key: str = Field(default="", description="Bybit API key")
    api_secret: str = Field(default="", description="Bybit API secret")
    testnet: bool = Field(default=True, description="Use Bybit testnet")
    domain: str = Field(default="bybit", description="Bybit domain (e.g. 'bybit')")
    tld: str = Field(default="com", description="Bybit TLD (e.g. 'com', 'eu')")

    @property
    def ws_public_url(self) -> str:
        """WebSocket URL for public market data."""
        sub = "stream-testnet" if self.testnet else "stream"
        return f"wss://{sub}.{self.domain}.{self.tld}/v5/public/spot"

    @property
    def ws_private_url(self) -> str:
        """WebSocket URL for private account data."""
        sub = "stream-testnet" if self.testnet else "stream"
        return f"wss://{sub}.{self.domain}.{self.tld}/v5/private"


# ── Grid Strategy ─────────────────────────────────────────────
class GridSettings(BaseSettings):
    """Grid trading strategy parameters."""

    model_config = SettingsConfigDict(env_prefix="GRID_")

    symbol: str = Field(default="BTCUSDC", description="Primary trading pair")
    symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated list of trading pairs",
    )
    max_active_pairs: int = Field(default=2, ge=1, le=10, description="Max simultaneous pairs")
    capital_usdt: float = Field(default=150.0, description="Total capital in USDT/USDC")
    num_levels: int = Field(default=5, ge=2, le=10, description="Grid levels per side")
    min_spacing_pct: float = Field(
        default=0.006,
        ge=0.003,
        le=0.05,
        description="Minimum spacing between grid levels (0.6%%)",
    )
    atr_multiplier: float = Field(
        default=1.5,
        ge=0.5,
        le=5.0,
        description="ATR multiplier for dynamic spacing",
    )
    order_size_usdt: float = Field(
        default=25.0,
        ge=5.0,
        description="Base order size in USDT/USDC (anchor for ATR sizing)",
    )
    sizing_baseline_atr: float = Field(
        default=0.05,
        ge=0.01,
        le=0.20,
        description="Baseline ATR% for sizing formula (5%)",
    )
    max_order_size_usdt: float = Field(
        default=100.0,
        ge=10.0,
        description="Maximum allowed order size (flash-crash protection)",
    )
    # --- Grid refresh parameters ---
    grid_refresh_price_distance_pct: float = Field(
        default=0.040, ge=0.010, le=0.200, description="4% base"
    )
    grid_refresh_atr_multiplier: float = Field(
        default=2.5, ge=1.0, le=5.0, description="Adaptive ATR multiplier"
    )
    grid_refresh_max_age_hours: float = Field(
        default=6.0, ge=1.0, le=72.0, description="TTL for grid without fills"
    )
    grid_refresh_cooldown_minutes: float = Field(
        default=90.0, ge=5.0, le=360.0, description="Per-symbol cooldown after refresh"
    )
    grid_refresh_stale_confirm_cycles: int = Field(
        default=2, ge=1, le=10, description="Consecutive stale cycles before action"
    )
    grid_refresh_max_per_day: int = Field(
        default=4, ge=1, le=20, description="Hard cap per symbol per calendar day"
    )
    leverage: int = Field(default=1, ge=1, le=10, description="Leverage (1 = spot)")
    loop_interval_seconds: int = Field(default=15, ge=5, le=300, description="Main loop interval")

    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            parts = [p.strip().upper().replace("/", "").replace("-", "") for p in value.split(",")]
            return [p for p in parts if p]
        if isinstance(value, list):
            return [str(p).strip().upper().replace("/", "").replace("-", "") for p in value if str(p).strip()]
        return value

    @field_validator("order_size_usdt")
    @classmethod
    def validate_order_size(cls, v: float) -> float:
        """Ensure order size is reasonable."""
        if v < 5.0:
            raise ValueError("Order size must be at least 5 USDT")
        return v


# ── Indicators ────────────────────────────────────────────────
class IndicatorSettings(BaseSettings):
    """Technical indicator parameters."""

    model_config = SettingsConfigDict(env_prefix="INDICATOR_")

    atr_period: int = Field(default=14, ge=5, le=50, description="ATR calculation period")
    adx_period: int = Field(default=14, ge=5, le=50, description="ADX calculation period")
    adx_threshold: float = Field(
        default=25.0,
        ge=15.0,
        le=50.0,
        description="ADX threshold — above this, grid pauses",
    )
    ema_fast: int = Field(default=50, ge=5, le=100, description="Fast EMA period")
    ema_slow: int = Field(default=200, ge=50, le=500, description="Slow EMA period")
    min_volume_ratio: float = Field(
        default=1.0,
        ge=0.5,
        le=3.0,
        description="Minimum current/SMA volume ratio",
    )


# ── Risk Management ──────────────────────────────────────────
class RiskSettings(BaseSettings):
    """Risk management and circuit breaker parameters."""

    model_config = SettingsConfigDict(env_prefix="RISK_")

    max_drawdown_pct: float = Field(
        default=0.15,
        ge=0.05,
        le=0.50,
        description="Maximum drawdown before emergency stop (15%)",
    )
    max_daily_loss_pct: float = Field(
        default=0.01,
        ge=0.005,
        le=0.10,
        description="Maximum daily loss before pause (1%)",
    )
    max_price_move_1h_pct: float = Field(
        default=0.08,
        ge=0.03,
        le=0.20,
        description="Max price movement in 1h before emergency (8%)",
    )
    daily_pause_hours: int = Field(
        default=24,
        ge=1,
        le=72,
        description="Hours to pause after daily loss limit hit",
    )


# ── Telegram ──────────────────────────────────────────────────
class TelegramSettings(BaseSettings):
    """Telegram notification settings."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    bot_token: str = Field(default="", description="Telegram bot token")
    chat_id: str = Field(default="", description="Telegram chat ID for alerts")
    enabled: bool = Field(default=True, description="Enable Telegram notifications")


# ── Dashboard ─────────────────────────────────────────────────
class DashboardSettings(BaseSettings):
    """Dashboard and API settings."""

    model_config = SettingsConfigDict(env_prefix="DASHBOARD_")

    host: str = Field(default="127.0.0.1", description="Dashboard bind host")
    port: int = Field(default=8000, ge=1024, le=65535, description="Dashboard port")
    username: str = Field(default="admin", description="Dashboard login username")
    password: str = Field(default="changeme", description="Dashboard login password")
    allowed_cors_origins: list[str] = Field(
        default=["*"],
        description="Allowed CORS origins; set to specific domains in production",
    )


# ── JWT ───────────────────────────────────────────────────────
class JWTSettings(BaseSettings):
    """JWT authentication settings."""

    model_config = SettingsConfigDict(env_prefix="JWT_")

    secret_key: str = Field(
        default="CHANGE_THIS_TO_A_RANDOM_SECRET_KEY_AT_LEAST_32_CHARS",
        description="Secret key for JWT token signing",
    )
    algorithm: str = Field(default="HS256", description="JWT signing algorithm")
    expire_minutes: int = Field(
        default=1440,
        ge=15,
        le=10080,
        description="JWT token expiration in minutes (default 24h)",
    )


# ── Health Check ──────────────────────────────────────────────
class HealthCheckSettings(BaseSettings):
    """External health monitoring settings."""

    model_config = SettingsConfigDict(env_prefix="HEALTHCHECK_")

    ping_url: str = Field(default="", description="healthchecks.io ping URL")
    interval_seconds: int = Field(default=60, ge=10, le=300, description="Ping interval")
    enabled: bool = Field(default=True, description="Enable healthchecks.io pings")


# ── Dead Man's Switch ─────────────────────────────────────────
class DeadMansSwitchSettings(BaseSettings):
    """Dead Man's Switch configuration."""

    model_config = SettingsConfigDict(env_prefix="DMS_")

    heartbeat_file: str = Field(
        default="bot_heartbeat.txt",
        description="Path to heartbeat file (relative to project root)",
    )
    max_silence_seconds: int = Field(
        default=120,
        ge=30,
        le=600,
        description="Max seconds without heartbeat before emergency",
    )
    check_interval_seconds: int = Field(
        default=30,
        ge=10,
        le=60,
        description="How often DMS checks the heartbeat",
    )


# ── Master Settings ──────────────────────────────────────────
class Settings(BaseSettings):
    """
    Master configuration aggregating all component settings.

    Each nested settings class is a BaseSettings with its own env_prefix,
    so variables like DASHBOARD_USERNAME are resolved automatically.
    The load_dotenv() call at module level ensures .env values are available.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Sub-settings (each reads its own prefixed env vars)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    grid: GridSettings = Field(default_factory=GridSettings)
    indicators: IndicatorSettings = Field(default_factory=IndicatorSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    jwt: JWTSettings = Field(default_factory=JWTSettings)
    healthcheck: HealthCheckSettings = Field(default_factory=HealthCheckSettings)
    dms: DeadMansSwitchSettings = Field(default_factory=DeadMansSwitchSettings)

    # Paths
    database_path: str = Field(
        default="data/trading_bot.db",
        description="SQLite database file path (relative to project root)",
    )
    log_level: str = Field(default="INFO", description="Log level")
    log_file: str = Field(
        default="logs/trading_bot.log",
        description="Log file path (relative to project root)",
    )

    @property
    def db_full_path(self) -> Path:
        """Full absolute path to the database file."""
        return PROJECT_ROOT / self.database_path

    @property
    def log_full_path(self) -> Path:
        """Full absolute path to the log file."""
        return PROJECT_ROOT / self.log_file

    @property
    def heartbeat_full_path(self) -> Path:
        """Full absolute path to the heartbeat file."""
        return PROJECT_ROOT / self.dms.heartbeat_file

    @staticmethod
    def parse_quote_coin(symbol: str) -> str:
        """Infer quote coin from spot symbol (e.g. BTCUSDT -> USDT)."""
        normalized = symbol.strip().upper().replace("/", "").replace("-", "")
        known_quotes = ("USDT", "USDC", "FDUSD", "DAI", "EUR", "USD", "BTC", "ETH")
        for quote in known_quotes:
            if normalized.endswith(quote):
                return quote
        return "USDT"

    @property
    def quote_coin(self) -> str:
        """Return quote coin derived from configured symbol."""
        return self.parse_quote_coin(self.grid.symbol)

    @property
    def active_symbols(self) -> list[str]:
        """Return normalized list of symbols to trade."""
        if self.grid.symbols:
            return self.grid.symbols
        return [self.grid.symbol.strip().upper().replace("/", "").replace("-", "")]

    def strategy_dict(self) -> dict:
        """Return strategy parameters consumable by StrategyConfig."""
        return {
            "symbol": self.grid.symbol,
            "num_levels": self.grid.num_levels,
            "min_spacing_pct": self.grid.min_spacing_pct,
            "atr_multiplier": self.grid.atr_multiplier,
            "order_size_usdt": self.grid.order_size_usdt,
            "sizing_baseline_atr": self.grid.sizing_baseline_atr,
            "max_order_size_usdt": self.grid.max_order_size_usdt,
            "adx_threshold": self.indicators.adx_threshold,
            "ema_fast": self.indicators.ema_fast,
            "ema_slow": self.indicators.ema_slow,
        }

    def risk_dict(self) -> dict:
        """Return risk manager parameters."""
        return {
            "max_drawdown_pct": self.risk.max_drawdown_pct,
            "max_daily_loss_pct": self.risk.max_daily_loss_pct,
            "max_hourly_move_pct": self.risk.max_price_move_1h_pct,
        }

    def public_dict(self) -> dict:
        """Return safe settings data excluding secrets."""
        data = {
            "symbol": self.grid.symbol,
            "symbols": self.active_symbols,
            "grid": self.grid.model_dump(),
            "indicators": self.indicators.model_dump(),
            "risk": self.risk.model_dump(),
            "exchange_testnet": self.exchange.testnet,
        }
        return data

    def save_defaults(self, path: Path | None = None) -> None:
        """Save current grid settings as defaults.json."""
        target = path or (PROJECT_ROOT / "config" / "defaults.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self.strategy_dict(), f, indent=2, ensure_ascii=False)


def load_settings() -> Settings:
    """Load and validate all settings from environment and .env file."""
    return Settings()
