"""
Tests for FIX-1: Inventory Stop-Loss Loop (BUG-2).
"""
import pytest
from unittest.mock import AsyncMock
from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderStatus

@pytest.fixture
def manager(mock_exchange):
    return OrderManager(mock_exchange, "BTCUSDC")

@pytest.mark.asyncio
async def test_inventory_sl_resets_position_qty_after_market_order(manager):
    """
    BUG-2 Fix: check_inventory_stop_loss must reset position_qty to 0
    immediately after placing the market order to prevent looping.
    """
    manager._position_qty = 0.5
    manager._avg_cost = 50000.0
    current_price = 40000.0 # Under 10% SL threshold
    
    manager.exchange.place_market_order.return_value = "mkt-123"
    
    triggered = await manager.check_inventory_stop_loss(current_price, stop_pct=0.1)
    
    assert triggered is True
    assert manager._position_qty == 0.0
    assert manager._avg_cost == 0.0
    # Verify the order was registered to be caught by handle_fill later
    assert "mkt-123" in manager._orders
    assert manager._orders["mkt-123"].qty == 0.5

@pytest.mark.asyncio
async def test_inventory_sl_does_not_loop_on_repeated_calls(manager):
    """
    Subsequent calls to check_inventory_stop_loss should return False
    once the position has been reset.
    """
    manager._position_qty = 0.5
    manager._avg_cost = 50000.0
    current_price = 40000.0
    
    manager.exchange.place_market_order.return_value = "mkt-123"
    
    # First call triggers
    await manager.check_inventory_stop_loss(current_price, stop_pct=0.1)
    
    # Second call should not trigger because position_qty is 0
    triggered_again = await manager.check_inventory_stop_loss(current_price, stop_pct=0.1)
    assert triggered_again is False
    assert manager.exchange.place_market_order.call_count == 1
