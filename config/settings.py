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
import os
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

    # ── Phase H: Grid Refresh Master Switch & Safety Gates ────────────
    enable_grid_refresh: bool = Field(
        default=False,
        description="Master switch for hybrid grid refresh (disabled = zero behavior change)",
    )
    enable_refresh_price_trigger: bool = Field(
        default=True, description="Use price deviation as a refresh trigger"
    )
    enable_refresh_time_trigger: bool = Field(
        default=True, description="Use time expiry as a refresh trigger"
    )
    refresh_adx_block_threshold: float = Field(
        default=35.0, ge=20.0, le=60.0,
        description="Block refresh if ADX above this AND trend is SHORT",
    )
    refresh_max_inventory_ratio: float = Field(
        default=0.40, ge=0.10, le=0.80,
        description="Block refresh if inventory/capital ratio exceeds this",
    )
    refresh_cooldown_seconds: int = Field(
        default=1800, ge=300, le=86400,
        description="Minimum seconds between refreshes per symbol",
    )
    refresh_min_move_pct: float = Field(
        default=0.02, ge=0.005, le=0.10,
        description="Block refresh if price moved less than this since last refresh",
    )
    refresh_skip_if_orders_above: int = Field(
        default=2, ge=0, le=10,
        description="Skip refresh if more than N buy orders still open",
    )
    refresh_failure_cooldown_seconds: int = Field(
        default=300, ge=60, le=3600,
        description="Cooldown after a failed refresh attempt",
    )

    leverage: int = Field(default=1, ge=1, le=10, description="Leverage (1 = spot)")
    loop_interval_seconds: int = Field(default=15, ge=5, le=300, description="Main loop interval")

    # --- Phase G: Asymmetric Grid Bias ---
    enable_asymmetric_grid: bool = Field(default=False, description="Enable asymmetric grid bias")
    asymmetric_bearish_buy_factor: float = Field(default=1.35, gt=1.0, lt=3.0, description="Widen buys in downtrend")
    asymmetric_bearish_sell_factor: float = Field(default=0.70, gt=0.0, lt=1.0, description="Tighten sells in downtrend")
    asymmetric_bullish_buy_factor: float = Field(default=0.80, gt=0.0, lt=1.0, description="Tighten buys in uptrend")
    asymmetric_bullish_sell_factor: float = Field(default=1.25, gt=1.0, lt=3.0, description="Widen sells in uptrend")
    asymmetric_min_profit_multiple: float = Field(default=1.15, ge=1.0, le=3.0, description="Min profit spread guard")

    # --- Phase G: Dynamic Profit Reinvestment ---
    enable_profit_reinvestment: bool = Field(default=False, description="Enable auto-compounding")
    reinvestment_recalc_interval_seconds: int = Field(default=3600, ge=300, le=86400, description="Seconds between baseline recalcs")
    reinvestment_equity_allocation_pct: float = Field(default=0.90, ge=0.50, le=0.99, description="Percentage of total equity to trade")
    reinvestment_max_step_growth_pct: float = Field(default=0.05, ge=0.005, le=0.20, description="Max baseline growth per recalc")
    reinvestment_min_baseline_floor_pct: float = Field(default=0.80, ge=0.30, le=0.99, description="Floor threshold for drawdown sizing")

    # ── Phase I: Dynamic Capital-Proportional Order Sizing ─────────
    # Instead of fixed GRID_ORDER_SIZE_USDT, compute each order as a
    # percentage of available capital:
    #   order_size = available_capital × order_size_pct_per_level
    #
    # Example: €2,000 free, 2 pairs × 5 levels, pct=0.05
    #   → €100/order, max deployment = 2×5×100 = €1,000 (50%)
    enable_dynamic_order_sizing: bool = Field(
        default=False,
        description="Use percentage-based sizing instead of fixed USDT amount (disabled = current behavior)",
    )
    order_size_pct_per_level: float = Field(
        default=0.05, ge=0.01, le=0.30,
        description="Fraction of available capital per grid level (5% = 0.05, 14% = 0.14)",
    )
    dynamic_sizing_min_order_usdt: float = Field(
        default=10.0, gt=0,
        description="Minimum order size floor — never below this regardless of percentage",
    )
    dynamic_sizing_max_order_usdt: float = Field(
        default=0.0, ge=0,
        description="Maximum order size cap (0 = no cap)",
    )

    # ── Liquidity pre-check (S3) ────────────────────────────────────
    liquidity_orderbook_levels: int = Field(
        default=25, ge=5, le=200,
        description="Orderbook depth levels fetched for bid-depth check",
    )
    liquidity_max_spread_pct: float = Field(
        default=0.005, ge=0.001, le=0.10,
        description="Legacy global spread limit (fallback if per-symbol not set)",
    )
    liquidity_min_depth_multiplier: float = Field(
        default=3.0, ge=0.5, le=20.0,
        description="Required bid depth = estimated_grid_capital × this multiplier",
    )
    # Per-symbol spread fallback (used when GRID_SPREAD_LIMIT_<SYM> not set)
    liquidity_spread_fallback: float = Field(
        default=0.005, ge=0.0005, le=0.05,
        description="Fallback spread limit for symbols without a specific limit",
    )
    spread_consecutive_failures_alert: int = Field(
        default=20, ge=5, le=100,
        description="Consecutive cycles with spread above limit before WARNING",
    )

    # ── Hard Stop-Loss ─────────────────────────────────────────────
    hard_stop_loss_pct: float = Field(
        default=0.08, ge=0.02, le=0.30,
        description="Stop-loss trigger distance below avg_cost (8% = 0.08)",
    )

    # ── Inventory Protection ────────────────────────────────────────
    max_inventory_ratio: float = Field(
        default=0.30, ge=0.05, le=0.80,
        description="Max inventory value / allocated capital ratio before blocking new buys",
    )
    inventory_exit_hours: float = Field(
        default=24.0, ge=1.0, le=168.0,
        description="Hours an inverse sell can be pending before exit strategy activates",
    )
    inventory_exit_above_cost_pct: float = Field(
        default=0.005, ge=0.0, le=0.05,
        description="Percentage above cost basis for inventory exit (0.5% = 0.005)",
    )

    # ── B.3 Trailing TP on inverse SELLs ────────────────────────────
    enable_tp_trailing: bool = Field(
        default=False,
        description="Ratchet inverse SELL orders upward when price runs >2×ATR above avg_cost",
    )
    tp_trailing_activation_atr_multiple: float = Field(
        default=2.0, ge=0.5, le=10.0,
        description="Activate trailing only when price > avg_cost × (1 + atr × this)",
    )
    tp_trailing_distance_atr_multiple: float = Field(
        default=1.0, ge=0.2, le=5.0,
        description="Trailing SELL stays this many ATRs below current price",
    )

    # ── C.2 DCA: progressive qty per BUY level ──────────────────────
    # Each BUY level idx N is qty = base_qty × (1 + dca_qty_increment × (N-1))
    # Default 0.0 = flat sizing (current behaviour). 0.3 = each deeper level
    # is 30% bigger than the previous, a soft martingale that lowers avg_cost
    # faster on adverse moves while keeping total exposure bounded.
    dca_qty_increment: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Per-level qty increment for BUY levels (0=flat, 0.3=+30% per deeper level)",
    )

    # ── C.3 Trend-rider mode (BUY-ladder when TRENDING_UP + ADX>threshold)
    enable_trend_rider: bool = Field(
        default=False,
        description="When regime=TRENDING_UP and ADX>trend_rider_adx_threshold, replace symmetric grid with tight BUY-ladder",
    )
    trend_rider_adx_threshold: float = Field(
        default=50.0, ge=30.0, le=80.0,
        description="ADX threshold above which the trend-rider mode activates",
    )
    trend_rider_buy_spacing_pct: float = Field(
        default=0.004, ge=0.001, le=0.03,
        description="Tight BUY spacing between ladder rungs",
    )
    trend_rider_levels: int = Field(
        default=3, ge=2, le=8,
        description="Number of BUY rungs in trend-rider ladder",
    )

    # ── Inventory Soft Stop-Loss ────────────────────────────────────
    enable_inventory_stop_loss: bool = Field(
        default=False,
        description="Enable soft stop-loss: sell all inventory at market if price drops below threshold",
    )
    inventory_stop_loss_pct: float = Field(
        default=0.10, ge=0.01, le=0.50,
        description="Stop-loss distance below avg_cost (10% = 0.10)",
    )
    inventory_stop_loss_per_symbol: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-symbol stop-loss override (takes priority over inventory_stop_loss_pct). "
            "High-volatility altcoins need wider thresholds. "
            "Example (JSON inline in .env): "
            '\'{"SOLUSDC": 0.15, "XRPUSDC": 0.15, "ADAUSDC": 0.18}\''
        ),
    )

    # ── Regime Classification ──────────────────────────────────────
    regime_adx_ranging: float = Field(
        default=20.0, ge=10.0, le=30.0,
        description="ADX below this → ranging market (ideal for grid)",
    )
    regime_adx_trending: float = Field(
        default=30.0, ge=20.0, le=50.0,
        description="ADX above this → strong trend",
    )
    regime_rsi_upper: float = Field(
        default=65.0, ge=55.0, le=80.0,
        description="RSI above this with high ADX → trending up",
    )
    regime_rsi_lower: float = Field(
        default=35.0, ge=20.0, le=45.0,
        description="RSI below this with high ADX → trending down",
    )

    # ── Volume Filter ──────────────────────────────────────────────
    volume_filter_enabled: bool = Field(
        default=False,
        description="Enable volume ratio filter for grid placement",
    )
    min_volume_ratio_filter: float = Field(
        default=0.70, ge=0.10, le=5.0,
        description="Minimum volume/median ratio required for grid placement",
    )

    # ── Dynamic Levels ─────────────────────────────────────────────
    dynamic_levels_enabled: bool = Field(
        default=False,
        description="Enable dynamic adjustment of grid levels based on ATR",
    )
    levels_low_vol_atr: float = Field(
        default=0.008, ge=0.002, le=0.02,
        description="ATR% below this → reduce levels",
    )
    levels_high_vol_atr: float = Field(
        default=0.015, ge=0.008, le=0.05,
        description="ATR% above this → increase levels",
    )
    levels_max: int = Field(
        default=7, ge=3, le=15,
        description="Maximum grid levels in high volatility",
    )

    def get_spread_limit(self, symbol: str) -> float:
        """Return the per-symbol spread limit, or fallback if not configured."""
        normalized = symbol.replace("/", "").replace("-", "").upper()
        env_key = f"GRID_SPREAD_LIMIT_{normalized}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            try:
                return float(env_val)
            except ValueError:
                pass
        return self.liquidity_spread_fallback

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
    daily_loss_pause_hours: float = Field(
        default=1.0,
        ge=0.5,
        le=24.0,
        description="Minimum pause hours after daily loss CB trigger (resumes early if equity recovers)",
    )

    # ── Phase J: Price Shock Circuit Breaker — Redesigned ─────────────

    # Minimum price samples before price shock evaluation activates.
    # Prevents cold-start false positives when the deque has too few points
    # to represent a real 1-hour window. At ~60s/loop, 10 samples ≈ 10 min
    # warmup. Valid range: 3–60. Warn below 5 (too sensitive), above 30
    # (warmup too long for frequent restarts).
    price_shock_min_samples: int = Field(
        default=10,
        ge=3,
        le=60,
        description="Minimum price samples for price shock evaluation (cold-start guard)",
    )

    # Consecutive evaluation cycles where price_move_1h must be BELOW
    # threshold before the pause is lifted. Prevents false resumption on
    # choppy markets. At lazy-eval interval of 5s, 3 cycles ≈ 15 s of
    # confirmed stability. Valid range: 2–10.
    price_shock_resume_consecutive_cycles: int = Field(
        default=3,
        ge=2,
        le=10,
        description="Consecutive clean cycles required before auto-resuming after price shock pause",
    )

    # Seconds a price shock pause can remain active before escalating to a
    # full emergency stop. Default 7200 = 2 hours. Genuine structural market
    # events (FTX collapse, March 2020 crash) typically sustain volatility
    # this long. Valid range: 1800 (30 min)–14400 (4 hours).
    price_shock_max_pause_duration_seconds: int = Field(
        default=7200,
        ge=1800,
        le=14400,
        description="Seconds before a sustained price shock escalates to emergency stop (default: 2h)",
    )

    # Whether to send Telegram notifications when price shock pause
    # activates and when it auto-resumes. Escalation to emergency stop
    # always notifies regardless of this flag.
    price_shock_notify_telegram: bool = Field(
        default=True,
        description="Send Telegram notifications for price shock pause/resume events",
    )

    # ── Emergency Liquidation ─────────────────────────────────────────
    emergency_liquidate_inventory: bool = Field(
        default=True,
        description=(
            "Market-sell all open inventory on emergency_stop(). "
            "Set to false to only cancel orders without liquidating."
        ),
    )


# ── Futures (Linear Perpetuals) ───────────────────────────────
class FuturesSettings(BaseSettings):
    """Linear perpetual (USDT) futures neutral-grid configuration.

    Replaces the legacy spot grid. A NEUTRAL grid places LONG limit orders
    below the mid price and SHORT limit orders above it, profiting from
    oscillation in *both* directions. Risk is bounded by a hard stop-loss and
    conservative leverage so the liquidation price sits far outside the grid
    range. Single-symbol by design (the spot multi-pair sprawl is removed).
    """

    model_config = SettingsConfigDict(env_prefix="FUTURES_")

    enabled: bool = Field(default=True, description="Run the futures bot (vs legacy spot)")
    symbol: str = Field(default="ETHUSDT", description="Linear perpetual symbol (USDT-margined)")
    category: str = Field(default="linear", description="Bybit product category")
    leverage: int = Field(default=2, ge=1, le=10, description="Account leverage (conservative 2-3x)")
    position_mode: str = Field(default="one-way", description="one-way | hedge")
    margin_mode: str = Field(default="ISOLATED", description="ISOLATED (set on exchange side)")
    timeframe: str = Field(default="5", description="Kline interval for indicators (minutes)")
    loop_interval_seconds: int = Field(default=10, ge=3, le=120, description="Main loop cadence")

    # ── Grid geometry ──
    grid_levels: int = Field(default=8, ge=2, le=30, description="Limit levels per side of mid")
    use_atr_range: bool = Field(default=True, description="Derive half-range from ATR instead of fixed pct")
    grid_range_atr_multiple: float = Field(
        default=2.5, ge=0.5, le=6.0, description="Half-range = ATR%% x this multiple",
    )
    grid_range_pct: float = Field(
        default=0.10, ge=0.02, le=0.50, description="Fixed half-range if use_atr_range=False (10%%)",
    )
    min_spacing_pct: float = Field(
        default=0.004, ge=0.001, le=0.05,
        description="Minimum profit per grid step (must cover fees + funding buffer)",
    )

    # ── Sizing ──
    capital_fraction: float = Field(
        default=0.80, ge=0.10, le=1.00, description="Fraction of available margin to deploy",
    )
    order_size_usdt: float = Field(
        default=0.0, ge=0.0, description="Fixed notional per level; 0 = auto from capital",
    )
    min_order_usdt: float = Field(default=5.0, gt=0, description="Exchange minimum notional floor")

    # ── Risk ──
    stop_loss_pct: float = Field(
        default=0.12, ge=0.02, le=0.50,
        description="Hard stop: close everything if price exits the grid range by this much",
    )
    min_liquidation_buffer_pct: float = Field(
        default=0.15, ge=0.05, le=0.50,
        description="Require the liquidation price to sit at least this far beyond the grid",
    )
    max_adverse_funding_rate: float = Field(
        default=0.001, ge=0.0, le=0.01,
        description="Pause new entries if 8h funding is worse than this against the net position",
    )

    # ── Recenter ──
    recenter_after_stop: bool = Field(
        default=True, description="Rebuild a fresh grid after a stop-loss episode",
    )
    recenter_cooldown_minutes: int = Field(
        default=30, ge=0, le=720, description="Cooldown before rebuilding after a stop",
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
    futures: FuturesSettings = Field(default_factory=FuturesSettings)
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

    def validate_trading_params(self) -> None:
        """Run logical validations on the loaded configuration pool. 
        Aborts execution if parameters are mutually incompatible or unsafe.
        """
        import logging
        log = logging.getLogger("config")
        
        errors = []
        
        # 1. EMA sanity
        if self.indicators.ema_fast >= self.indicators.ema_slow:
            errors.append(f"EMA Fast ({self.indicators.ema_fast}) must be < EMA Slow ({self.indicators.ema_slow})")
            
        # 2. Risk sanity
        if self.risk.max_daily_loss_pct >= self.risk.max_drawdown_pct:
            errors.append(
                f"Daily Loss Pct ({self.risk.max_daily_loss_pct:.1%}) must be strictly less than "
                f"Emergency Drawdown Pct ({self.risk.max_drawdown_pct:.1%})"
            )
            
        # 3. Capital allocation sanity
        symbols_count = max(1, len(self.active_symbols))
        capital_per_symbol = self.grid.capital_usdt / symbols_count
        required_per_symbol = self.grid.order_size_usdt * self.grid.num_levels
        if capital_per_symbol < required_per_symbol:
            log.warning(
                "Capital WARNING: Allocated capital per symbol ($%.2f) is less than "
                "the amount required for a full grid ($%.2f). Max inventory gates may trigger early.",
                capital_per_symbol, required_per_symbol
            )

        # 3b. order_size_usdt sanity guard (catches GRID_ORDER_SIZE_USDT=300 with small capital)
        max_safe_order = self.grid.capital_usdt / max(1, self.grid.max_active_pairs) * 0.5
        if self.grid.order_size_usdt > max_safe_order:
            log.warning(
                "order_size_usdt=%.2f may be too large for capital=%.2f "
                "with max_active_pairs=%d (safe max ≈ %.2f). "
                "Consider reducing GRID_ORDER_SIZE_USDT.",
                self.grid.order_size_usdt, self.grid.capital_usdt,
                self.grid.max_active_pairs, max_safe_order,
            )
            
        # 4. Regime sanity
        if self.grid.regime_adx_ranging >= self.grid.regime_adx_trending:
            errors.append(f"Regime ADX Ranging ({self.grid.regime_adx_ranging}) must be < Trending ({self.grid.regime_adx_trending})")
            
        if self.grid.regime_rsi_lower >= self.grid.regime_rsi_upper:
            errors.append(f"Regime RSI Lower ({self.grid.regime_rsi_lower}) must be < Upper ({self.grid.regime_rsi_upper})")
            
        # 5. Inventory gate sanity
        if self.grid.max_inventory_ratio >= 1.0:
            errors.append(f"max_inventory_ratio ({self.grid.max_inventory_ratio}) should be strictly < 1.0 (100% of allocation)")

        if errors:
            for err in errors:
                log.error("CONFIG ERROR: %s", err)
            raise ValueError("Trading configuration validation failed. Check settings and restart.")

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
            # Phase G: asymmetric grid (was missing — settings flags never reached the strategy)
            "enable_asymmetric_grid": self.grid.enable_asymmetric_grid,
            "asymmetric_bearish_buy_factor": self.grid.asymmetric_bearish_buy_factor,
            "asymmetric_bearish_sell_factor": self.grid.asymmetric_bearish_sell_factor,
            "asymmetric_bullish_buy_factor": self.grid.asymmetric_bullish_buy_factor,
            "asymmetric_bullish_sell_factor": self.grid.asymmetric_bullish_sell_factor,
            "asymmetric_min_profit_multiple": self.grid.asymmetric_min_profit_multiple,
            # C.2 DCA
            "dca_qty_increment": self.grid.dca_qty_increment,
            # C.3 Trend-rider
            "enable_trend_rider": self.grid.enable_trend_rider,
            "trend_rider_adx_threshold": self.grid.trend_rider_adx_threshold,
            "trend_rider_buy_spacing_pct": self.grid.trend_rider_buy_spacing_pct,
            "trend_rider_levels": self.grid.trend_rider_levels,
        }

    def risk_dict(self) -> dict:
        """Return risk manager parameters."""
        return {
            "max_drawdown_pct": self.risk.max_drawdown_pct,
            "max_daily_loss_pct": self.risk.max_daily_loss_pct,
            "max_hourly_move_pct": self.risk.max_price_move_1h_pct,
            "min_price_shock_samples": self.risk.price_shock_min_samples,
            "price_shock_resume_cycles": self.risk.price_shock_resume_consecutive_cycles,
            "price_shock_max_pause_secs": self.risk.price_shock_max_pause_duration_seconds,
            "daily_loss_pause_hours": self.risk.daily_loss_pause_hours,
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
