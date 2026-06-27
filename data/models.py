"""Domain models shared by backend modules and API routes.

Uses dataclasses with slots for memory efficiency and StrEnum for
string-compatible enum values that work natively with Bybit's API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


# ── Enums ─────────────────────────────────────────────────────────────


class BotStatus(StrEnum):
    """Lifecycle states for the trading bot."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    EMERGENCY = "emergency"


class OrderSide(StrEnum):
    """Order side representation compatible with Bybit."""

    BUY = "Buy"
    SELL = "Sell"


class OrderStatus(StrEnum):
    """Internal order status lifecycle."""

    PENDING = "pending"
    FILLED = "filled"
    CANCELED = "canceled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"


class EventLevel(StrEnum):
    """Event severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class TrendBias(StrEnum):
    """Directional bias from EMA filter."""

    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class PositionSide(StrEnum):
    """Net futures position direction (one-way mode)."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


# ── Futures Models ────────────────────────────────────────────────────


@dataclass(slots=True)
class FuturesPosition:
    """Live futures position snapshot from the exchange (the source of truth).

    Replaces the spot bot's reconstructed _position_qty / _avg_cost. For
    futures we always read the authoritative position from get_positions(),
    which eliminates the desync that caused thousands of balance errors.
    """

    symbol: str
    side: str  # PositionSide value: "long" | "short" | "flat"
    size: float  # contract qty in base units; 0 = flat
    entry_price: float
    mark_price: float
    liq_price: float
    leverage: float
    unrealized_pnl: float
    position_value: float
    margin: float
    updated_at: datetime

    @property
    def is_flat(self) -> bool:
        return self.size <= 0.0 or self.side == PositionSide.FLAT


# ── Trade Models ──────────────────────────────────────────────────────


@dataclass(slots=True)
class TradeRecord:
    """Executed trade persisted in SQLite."""

    timestamp: datetime
    side: str
    price: float
    qty: float
    fee: float
    pnl: float
    status: str
    symbol: str = "BTCUSDC"
    order_type: str = "Limit"
    exchange_order_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Grid Models ───────────────────────────────────────────────────────


@dataclass(slots=True)
class GridLevel:
    """Single grid level calculated by the strategy."""

    level_id: str
    price: float
    side: str
    qty: float
    status: str = OrderStatus.PENDING


@dataclass(slots=True)
class GridState:
    """Persisted snapshot of current grid orders."""

    symbol: str
    spacing_pct: float
    trend_bias: str
    levels: list[GridLevel]
    last_sync_time: datetime
    grid_created_at: datetime | None = None
    grid_anchor_price: float | None = None
    pending_retries: list[dict[str, Any]] = field(default_factory=list)
    # Phase G: asymmetric spacing persistence
    buy_spacing_pct: float = 0.0
    sell_spacing_pct: float = 0.0
    # Phase H: refresh metadata
    last_refresh_price: float | None = None
    refresh_count: int = 0
    original_anchor_price: float | None = None
    # Bug 3: Cost basis persistence
    position_qty: float = 0.0
    avg_cost: float = 0.0


# ── Performance Models ────────────────────────────────────────────────


@dataclass(slots=True)
class PerformanceMetrics:
    """Snapshot of performance KPIs persisted each cycle."""

    timestamp: datetime
    equity: float
    pnl_total: float
    pnl_daily: float
    drawdown_pct: float
    win_rate: float
    sharpe_ratio: float
    total_trades: int


@dataclass(slots=True)
class DailyPnL:
    """Daily PnL record for the performance chart."""

    date: str
    pnl: float
    cumulative_pnl: float
    trades_count: int
    capital: float


@dataclass(slots=True)
class EquityPoint:
    """Single point in the equity curve."""

    timestamp: datetime
    capital: float
    drawdown_pct: float = 0.0


# ── Event Models ──────────────────────────────────────────────────────


@dataclass(slots=True)
class EventLogRecord:
    """Structured operational event."""

    timestamp: datetime
    level: str
    module: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CircuitBreakerEvent:
    """Circuit breaker activation record."""

    timestamp: datetime
    breaker_type: str
    trigger_value: float
    threshold: float
    action_taken: str
    details: str = ""


# ── Config Models ─────────────────────────────────────────────────────


@dataclass(slots=True)
class ConfigSnapshot:
    """Stored historical configuration point."""

    timestamp: datetime
    values: dict[str, Any]
