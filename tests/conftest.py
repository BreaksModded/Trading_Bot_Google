"""
Shared test fixtures for the trading bot test suite.

Provides reusable fixtures: settings, database, OHLCV DataFrames,
mock exchange, and mock strategy components.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from config.settings import Settings
from data.database import Database
from data.models import (
    EventLevel,
    OrderSide,
    TradeRecord,
)


# ── Settings ──────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    """Create Settings with safe defaults for testing (no .env loading)."""
    return Settings(
        _env_file=None,
        database_path="test_bot.db",
        log_level="DEBUG",
    )


# ── Database ──────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "test_bot.db"
    db = Database(db_path)
    return db


@pytest.fixture
def populated_db(tmp_db: Database) -> Database:
    """
    Database pre-populated with sample trades for metric testing.

    Creates 10 trades: 6 sells (4 winning, 2 losing) and 4 buys.
    """
    base_time = datetime.utcnow() - timedelta(days=5)

    # 4 Buy trades
    for i in range(4):
        trade = TradeRecord(
            timestamp=datetime.utcnow(),
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            price=50000 + i * 100,
            qty=0.001,
            fee=0.005,
            pnl=0.0,
            status="filled",
            exchange_order_id=f"buy_{i}",
        )
        tmp_db.insert_trade(trade)

    # 4 Winning sell trades
    for i in range(4):
        trade = TradeRecord(
            timestamp=datetime.utcnow(),
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=50200 + i * 100,
            qty=0.001,
            fee=0.005,
            pnl=0.15,  # Small profit
            status="filled",
            exchange_order_id=f"sell_win_{i}",
        )
        tmp_db.insert_trade(trade)

    # 2 Losing sell trades
    for i in range(2):
        trade = TradeRecord(
            timestamp=datetime.utcnow(),
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            price=49800 + i * 50,
            qty=0.001,
            fee=0.005,
            pnl=-0.10,  # Small loss
            status="filled",
            exchange_order_id=f"sell_loss_{i}",
        )
        tmp_db.insert_trade(trade)

    return tmp_db


# ── OHLCV DataFrames ─────────────────────────────────────────────────


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """
    Generate a realistic OHLCV DataFrame with 300 candles.

    Uses random walk around BTC-like prices with controlled volatility.
    Sufficient for ATR(14), ADX(14), EMA(200) calculations.
    """
    np.random.seed(42)
    n = 300
    timestamps = pd.date_range("2024-01-01", periods=n, freq="h")

    # Random walk price with mean reversion
    price = 50000.0
    prices = []
    for _ in range(n):
        change = np.random.normal(0, 100)  # ~$100 std per hour
        price += change
        price = max(price, 30000)  # Floor
        prices.append(price)

    close = np.array(prices)
    high = close + np.abs(np.random.normal(50, 30, n))
    low = close - np.abs(np.random.normal(50, 30, n))
    open_ = close + np.random.normal(0, 20, n)
    volume = np.random.uniform(100, 10000, n)

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })
    df = df.set_index("timestamp")
    return df


@pytest.fixture
def small_df() -> pd.DataFrame:
    """Small OHLCV DataFrame with only 5 rows (for error testing)."""
    timestamps = pd.date_range("2024-01-01", periods=5, freq="h")
    data = {
        "open": [50000, 50100, 50050, 50200, 50150],
        "high": [50100, 50200, 50100, 50300, 50200],
        "low": [49900, 50000, 49950, 50100, 50050],
        "close": [50050, 50080, 50060, 50250, 50180],
        "volume": [1000, 1200, 800, 1500, 900],
    }
    df = pd.DataFrame(data, index=timestamps)
    return df


@pytest.fixture
def ranging_df() -> pd.DataFrame:
    """
    OHLCV DataFrame simulating a ranging market (low ADX).

    Oscillates in a tight range — ideal for grid trading.
    """
    np.random.seed(123)
    n = 300
    timestamps = pd.date_range("2024-01-01", periods=n, freq="h")

    # Tight oscillation
    t = np.arange(n)
    base = 50000 + 200 * np.sin(2 * np.pi * t / 48)  # 48-hour cycle
    noise = np.random.normal(0, 30, n)
    close = base + noise

    high = close + np.abs(np.random.normal(20, 10, n))
    low = close - np.abs(np.random.normal(20, 10, n))
    open_ = close + np.random.normal(0, 10, n)
    volume = np.random.uniform(100, 5000, n)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=timestamps)
    return df


@pytest.fixture
def trending_df() -> pd.DataFrame:
    """
    OHLCV DataFrame simulating a strong trend (high ADX).

    Steady upward price movement.
    """
    np.random.seed(456)
    n = 300
    timestamps = pd.date_range("2024-01-01", periods=n, freq="h")

    # Strong uptrend
    base = 50000 + np.arange(n) * 30  # +$30 per hour
    noise = np.random.normal(0, 20, n)
    close = base + noise

    high = close + np.abs(np.random.normal(30, 15, n))
    low = close - np.abs(np.random.normal(30, 15, n))
    open_ = close + np.random.normal(0, 15, n)
    volume = np.random.uniform(100, 5000, n)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=timestamps)
    return df


# ── Mock Exchange ─────────────────────────────────────────────────────


@pytest.fixture
def mock_exchange() -> MagicMock:
    """Create a mock exchange with common methods configured."""
    exchange = MagicMock()
    exchange.get_ticker_price.return_value = 50000.0
    exchange.get_balance.return_value = 150.0
    exchange.get_total_equity.return_value = 150.0
    exchange.place_limit_order.return_value = "mock_order_id_001"
    exchange.cancel_all_orders.return_value = 5
    exchange.cancel_order.return_value = True
    exchange.get_open_orders.return_value = []
    exchange.get_order_status.return_value = {"orderStatus": "Filled"}
    exchange.is_connected = True
    exchange.latency_ms = 15.0
    exchange._testnet = True
    exchange._symbol = "BTCUSDT"
    return exchange
