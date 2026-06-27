"""Shared pytest fixtures (futures bot).

The spot-era fixtures (risk_manager, sample_signal) were removed along with the
spot trading core; the futures unit tests are self-contained.
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from core.exchange import SpotSymbolRules
from data.database import Database


@pytest.fixture
def mock_exchange():
    """AsyncMock of the exchange client with realistic responses."""
    exchange = AsyncMock()
    exchange.place_limit_order.return_value = "test_order_123"
    exchange.get_open_orders.return_value = []
    exchange.get_order_history.return_value = []
    exchange.get_last_price.return_value = 50000.0
    exchange.get_spot_symbol_rules.return_value = SpotSymbolRules(
        qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.01"),
    )
    return exchange


@pytest.fixture
def in_memory_db(tmp_path):
    """Database instance using a temporary file. Isolated per test."""
    db_file = tmp_path / "test.sqlite3"
    db = Database(db_file)
    db.init_schema()
    yield db
    db.stop_writer()


@pytest.fixture
def sample_symbol_rules():
    """Symbol precision/limit rules for a typical mid-cap token."""
    return SpotSymbolRules(
        qty_step=Decimal("0.001"), min_qty=Decimal("0.001"), tick_size=Decimal("0.01"),
    )
