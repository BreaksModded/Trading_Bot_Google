"""
Tests for FIX-3: Partial Fills (BUG-1).
"""
import pytest
from unittest.mock import AsyncMock
from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderStatus, OrderSide

@pytest.fixture
def manager(mock_exchange):
    return OrderManager(mock_exchange, "BTCUSDC")

@pytest.mark.asyncio
async def test_partial_fill_accumulates_position_qty(manager):
    """
    BUG-1 Fix: Multiple partial fills must accumulate position_qty 
    without marking the order as FILLED prematurely or placing multiple inverse orders.
    """
    order_id = "ORD-PARTIAL"
    manager._orders[order_id] = ManagedOrder(
        order_id=order_id, level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    manager.exchange.place_limit_order = AsyncMock(return_value="INV-1")
    
    # 1. First partial fill: 0.04 of 0.1
    fill_1 = {
        "execId": "EXEC-1", "orderId": order_id, "side": "Buy",
        "execQty": "0.04", "cumExecQty": "0.04", "qty": "0.1", "avgPrice": "50000.0",
        "orderStatus": "PartiallyFilled"
    }
    await manager.handle_fill(fill_1)
    
    assert manager._orders[order_id].status == OrderStatus.PENDING # Still pending
    assert manager._position_qty == pytest.approx(0.039996) # 0.04 - fee (0.0001)
    assert manager.exchange.place_limit_order.call_count == 0 # No inverse yet
    
    # 2. Second partial fill: 0.06 (completes the 0.1)
    fill_2 = {
        "execId": "EXEC-2", "orderId": order_id, "side": "Buy",
        "execQty": "0.06", "cumExecQty": "0.1", "qty": "0.1", "avgPrice": "50000.0",
        "orderStatus": "Filled"
    }
    await manager.handle_fill(fill_2)
    
    assert manager._orders[order_id].status == OrderStatus.FILLED
    assert manager._position_qty == pytest.approx(0.09999) # Total 0.1 - total fee
    assert manager.exchange.place_limit_order.call_count == 1 # Inverse placed only once
