"""Unit tests for the OrderManager class and Phase F gathered placements."""
import pytest
import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from core.order_manager import OrderManager, ManagedOrder
from data.models import GridLevel, OrderSide, OrderStatus

@pytest.fixture
def manager(mock_exchange):
    """Instantiate a clean OrderManager linked to a mocked ExchangeGateway."""
    return OrderManager(
        exchange=mock_exchange,
        symbol="BTCUSDT",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.01
    )

@pytest.mark.asyncio
async def test_place_grid_orders_stores_all_levels(manager, sample_signal):
    """Mock exchange to return unique order IDs. Assert all N levels are stored."""
    async def mock_place(**kw):
        return {
            "orderId": f"ord_{kw['price']}",
            "orderLinkId": kw.get("orderLinkId"),
            "status": "NEW"
        }
    manager.exchange.place_limit_order.side_effect = mock_place

    await manager.place_grid_orders(sample_signal.levels, sample_signal.spacing_pct)
    
    orders = manager.open_orders
    assert len(orders) == len(sample_signal.levels)
    for lvl in sample_signal.levels:
        assert any(lvl.price == o.price for o in orders)

@patch("core.order_manager.logger.warning")
@pytest.mark.asyncio
async def test_place_grid_orders_partial_failure(mock_logger, manager, sample_signal):
    """Mock exchange to fail on level 3 of 5. Assert: 1,2,4,5 are stored, 3 is NOT."""
    fail_level = sample_signal.levels[2]
    
    async def mock_place(**kw):
        if fail_level.level_id in kw["orderLinkId"]:
            raise Exception("API Error")
        return {"orderId": f"ord_{kw['price']}", "orderLinkId": kw.get("orderLinkId"), "status": "NEW"}
    
    manager.exchange.place_limit_order.side_effect = mock_place
    await manager.place_grid_orders(sample_signal.levels, sample_signal.spacing_pct)
    
    orders = manager.open_orders
    assert len(orders) == 4
    assert not any(fail_level.price == o.price for o in orders)
    assert any("levels failed" in call.args[0] and call.args[2] == 1 for call in mock_logger.call_args_list)

@patch("core.order_manager.logger.warning")
@pytest.mark.asyncio
async def test_place_grid_orders_total_failure(mock_logger, manager, sample_signal):
    manager.exchange.place_limit_order.side_effect = Exception("Global API Error")
    await manager.place_grid_orders(sample_signal.levels, sample_signal.spacing_pct)
    assert len(manager.open_orders) == 0
    assert any("levels failed" in call.args[0] and call.args[2] == 5 for call in mock_logger.call_args_list)

@pytest.mark.asyncio
async def test_order_deduplication_prevents_double_placement(manager, sample_signal):
    lvl = sample_signal.levels[0]
    manager._orders["existing_123"] = ManagedOrder(
        order_id="existing_123", level_id=lvl.level_id, symbol="BTCUSDT", side="Buy", price=40000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    await manager.place_grid_orders([lvl], sample_signal.spacing_pct)
    manager.exchange.place_limit_order.assert_not_called()

@pytest.mark.asyncio
async def test_gather_places_levels_concurrently(manager):
    levels = [GridLevel(level_id=f"lvl_{i}", price=50000.0-i, side="Buy", qty=0.1) for i in range(10)]
    
    async def delayed_place(**kw):
        await asyncio.sleep(0.05)
        return {"orderId": f"ord_{kw['price']}", "orderLinkId": kw.get("orderLinkId"), "status": "NEW"}
    
    manager.exchange.place_limit_order.side_effect = delayed_place
    
    start_t = time.monotonic()
    await manager.place_grid_orders(levels, 0.01)
    duration = time.monotonic() - start_t
    
    assert duration < 0.2
    assert len(manager.open_orders) == 10

@pytest.mark.asyncio
async def test_gather_return_exceptions_true_prevents_cancellation(manager):
    levels = [GridLevel(level_id=f"lvl_{i}", price=50000.0-i, side="Buy", qty=0.1) for i in range(10)]
    
    async def picky_place(**kw):
        if "lvl_4" in kw["orderLinkId"]: # 5th level
            raise Exception("Failure")
        return {"orderId": f"ord_{kw['price']}", "orderLinkId": kw.get("orderLinkId"), "status": "NEW"}
        
    manager.exchange.place_limit_order.side_effect = picky_place
    await manager.place_grid_orders(levels, 0.01)
    
    orders = manager.open_orders
    assert len(orders) == 9

@pytest.mark.asyncio
async def test_handle_fill_triggers_inverse_grid_placement(manager):
    manager._orders["ext_1"] = ManagedOrder(
        order_id="ext_1", level_id="lvl_1", symbol="BTCUSDT", side="Buy", price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    manager.exchange.place_limit_order.return_value = "ext_inv_1"
    
    fill_event = {"orderId": "ext_1", "status": "Filled", "execQty": "0.1", "price": "50000.0", "side": "Buy"}
    await manager.handle_fill(fill_event)
    
    assert manager._orders["ext_1"].status == OrderStatus.FILLED
    assert "ext_inv_1" in manager._orders
    
    manager.exchange.place_limit_order.assert_called_once()
    call_args = manager.exchange.place_limit_order.call_args[1]
    assert call_args["side"] == "Sell"
    assert call_args["qty"] == 0.1
    assert call_args["price"] > 50000.0

@pytest.mark.asyncio
async def test_handle_fill_unknown_order_is_ignored_safely(manager):
    fill_event = {"orderId": "unknown", "status": "Filled"}
    result = await manager.handle_fill(fill_event)
    assert result is None
    manager.exchange.place_limit_order.assert_not_called()

@pytest.mark.asyncio
async def test_sync_with_exchange_reconciles_filled_order_on_ws_miss(manager):
    manager._orders["ext_1"] = ManagedOrder(
        order_id="ext_1", level_id="lvl_1", symbol="BTCUSDT", side="Buy", price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    manager.exchange.get_open_orders.return_value = []
    manager.exchange.get_order_history.return_value = {"orderStatus": "Filled", "orderId": "ext_1"}
    
    with patch.object(manager, 'handle_fill') as mock_handle_fill:
        await manager.sync_with_exchange()
        mock_handle_fill.assert_called_once()

@pytest.mark.asyncio
async def test_sync_with_exchange_marks_actually_canceled_order(manager):
    manager._orders["ext_1"] = ManagedOrder(
        order_id="ext_1", level_id="lvl_1", symbol="BTCUSDT", side="Buy", price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    manager.exchange.get_open_orders.return_value = []
    manager.exchange.get_order_history.return_value = {"orderStatus": "Cancelled", "orderId": "ext_1"}
    
    await manager.sync_with_exchange()
    assert manager._orders["ext_1"].status == OrderStatus.CANCELED

@patch("core.order_manager.logger.warning")
@pytest.mark.asyncio
async def test_sync_rest_fallback_handles_api_failure_gracefully(mock_logger, manager):
    manager._orders["ext_1"] = ManagedOrder(
        order_id="ext_1", level_id="lvl_1", symbol="BTCUSDT", side="Buy", price=50000.0, qty=0.1, status=OrderStatus.PENDING
    )
    manager.exchange.get_open_orders.return_value = []
    manager.exchange.get_order_history.side_effect = Exception("History unavailable")
    
    await manager.sync_with_exchange()
    assert manager._orders["ext_1"].status == OrderStatus.PENDING
    assert any("History unavailable" in str(call) or "Error reconciling order" in str(call.args) for call in mock_logger.call_args_list)
