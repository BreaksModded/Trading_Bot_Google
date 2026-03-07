"""
Tests for FIX-5: _orders memory cleanup (BUG-6).
"""
import pytest
import asyncio
from datetime import datetime, timedelta, UTC
from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderStatus

@pytest.fixture
def manager(mock_exchange):
    return OrderManager(mock_exchange, "BTCUSDC")

@pytest.mark.asyncio
async def test_cleanup_removes_filled_orders_after_24h(manager):
    """
    BUG-6 Fix: _cleanup_stale_orders must remove FILLED/CANCELED orders 
    older than the threshold.
    """
    now = datetime.now(UTC)
    
    # 1. Old filled order (should be removed)
    manager._orders["old-1"] = ManagedOrder(
        order_id="old-1", level_id="l1", symbol="BTCUSDC", side="Buy", price=100, qty=1,
        status=OrderStatus.FILLED, updated_at=now - timedelta(hours=25)
    )
    
    # 2. Recent filled order (should stay)
    manager._orders["recent-1"] = ManagedOrder(
        order_id="recent-1", level_id="l2", symbol="BTCUSDC", side="Buy", price=100, qty=1,
        status=OrderStatus.FILLED, updated_at=now - timedelta(hours=1)
    )
    
    # 3. Old pending order (should stay!)
    manager._orders["pending-old"] = ManagedOrder(
        order_id="pending-old", level_id="l3", symbol="BTCUSDC", side="Buy", price=100, qty=1,
        status=OrderStatus.PENDING, updated_at=now - timedelta(hours=48)
    )
    
    purged = await manager._cleanup_stale_orders(max_age_hours=24.0)
    
    assert purged == 1
    assert "old-1" not in manager._orders
    assert "recent-1" in manager._orders
    assert "pending-old" in manager._orders
