import pytest
from unittest.mock import AsyncMock
from decimal import Decimal
from datetime import UTC, datetime

from data.database import Database
from core.exchange import SpotSymbolRules
from core.risk_manager import RiskManager
from core.strategy import StrategySignal
from data.models import GridLevel, OrderSide, TrendBias


@pytest.fixture
def mock_exchange():
    """Full AsyncMock of the exchange client with realistic responses."""
    exchange = AsyncMock()
    
    # Generic realistic successful order response (Bybit v5 returns order_id as string)
    exchange.place_limit_order.return_value = "test_order_123"
    
    exchange.get_open_orders.return_value = []
    exchange.get_order_history.return_value = []
    exchange.get_last_price.return_value = 50000.0
    exchange.get_spot_symbol_rules.return_value = SpotSymbolRules(
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        tick_size=Decimal("0.01")
    )
    
    return exchange


@pytest.fixture
def in_memory_db(tmp_path):
    """Database instance using a temporary file. Isolated per test."""
    db_file = tmp_path / "test.sqlite3"
    db = Database(db_file)
    # Initialize schema so tests can rely on tables existing
    db.init_schema()
    yield db
    db.stop_writer()


@pytest.fixture
def risk_manager():
    """RiskManager with safe test defaults."""
    return RiskManager(
        max_drawdown_pct=0.25,
        max_daily_loss_pct=0.10,
        max_hourly_move_pct=0.05
    )


@pytest.fixture
def sample_symbol_rules():
    """SpotSymbolRules for a typical mid-cap token."""
    return SpotSymbolRules(
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        tick_size=Decimal("0.01")
    )


@pytest.fixture
def sample_signal():
    """A realistic 5-level buy grid signal factory."""
    current_price = 50000.0
    spacing_pct = 0.01

    levels = []
    # Build 5 buy levels below current price
    target_notional = 100.0  # 100 USDT per level approx
    qty = target_notional / current_price # 0.002
    
    for idx in range(1, 6):
        price = current_price * (1 - spacing_pct * idx)
        levels.append(GridLevel(
            level_id=f"buy-{idx}-{int(price)}",
            price=price,
            side=OrderSide.BUY,
            qty=qty, 
            status="pending"
        ))

    return StrategySignal(
        generated_at=datetime.now(UTC),
        current_price=current_price,
        spacing_pct=spacing_pct,
        trend_bias=TrendBias.LONG,
        adx_value=20.0,
        atr_pct=0.02,
        volume_ratio=1.2,
        pause_new_grid=False,
        target_notional=target_notional,
        levels=levels,
        reason="grid_active",
        close_history=None
    )
