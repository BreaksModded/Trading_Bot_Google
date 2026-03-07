"""
Tests for FIX-2: REST vs WebSocket Deduplication (BUG-3).
"""
import pytest
from unittest.mock import AsyncMock
from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderStatus, OrderSide

@pytest.fixture
def manager(mock_exchange):
    return OrderManager(mock_exchange, "BTCUSDC")

@pytest.mark.asyncio
async def test_handle_fill_idempotent_when_already_filled(manager):
    """
    BUG-3 Fix: handle_fill must not process the same order twice
    if it's already marked as FILLED (e.g., by REST fallback).
    """
    order_id = "ORD-123"
    manager._orders[order_id] = ManagedOrder(
        order_id=order_id, level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=50000.0, qty=0.1, status=OrderStatus.FILLED # Already FILLED
    )
    manager._position_qty = 0.1
    manager._avg_cost = 50000.0
    
    fill_event = {
        "execId": "WS-EXEC-456", # Different key than REST composite
        "orderId": order_id,
        "side": "Buy",
        "execQty": "0.1",
        "avgPrice": "50000.0",
    }
    
    manager.exchange.place_limit_order = AsyncMock()
    
    # Process fill
    result = await manager.handle_fill(fill_event)
    
    # Should be ignored (None)
    assert result is None
    # Should not place another inverse order
    assert manager.exchange.place_limit_order.call_count == 0
    # Position should not be doubled
    assert manager._position_qty == 0.1

@pytest.mark.asyncio
async def test_no_duplicate_inverse_order_on_ws_after_rest(manager):
    """
    Full sequence: REST fills order -> WS arrives later -> No double action.
    """
    order_id = "ORD-999"
    manager._orders[order_id] = ManagedOrder(
        order_id=order_id, level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    # 1. Simulate REST fill (uses composite key)
    rest_fill = {"orderId": order_id, "execQty": "0.1", "avgPrice": "50000.0", "side": "Buy"}
    manager.exchange.place_limit_order.return_value = "INV-REST"
    await manager.handle_fill(rest_fill)
    
    assert manager._orders[order_id].status == OrderStatus.FILLED
    assert manager.exchange.place_limit_order.call_count == 1
    
    # 2. Simulate WS fill arriving (uses execId key)
    ws_fill = {"orderId": order_id, "execId": "EXEC-WS-1", "execQty": "0.1", "avgPrice": "50000.0", "side": "Buy"}
    await manager.handle_fill(ws_fill)
    
    # Call count should still be 1
    assert manager.exchange.place_limit_order.call_count == 1
    assert manager._position_qty == pytest.approx(0.09999)
