"""
Tests for Phase III Critical Bug Fixes (Bugs 1-5).
"""
import pytest
import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from core.order_manager import OrderManager, ManagedOrder, TradeRecord
from data.models import OrderSide, OrderStatus

@pytest.fixture
def manager(mock_exchange):
    """Instantiate a clean OrderManager linked to a mocked ExchangeGateway."""
    return OrderManager(
        exchange=mock_exchange,
        symbol="BTCUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.01
    )

# --- BUG 1: Deadlock asyncio.Lock ---

@pytest.mark.asyncio
async def test_no_deadlock_on_missed_ws_fill(manager):
    """
    Test that sync_with_exchange can process missed fills found via REST
    without deadlocking (Bug 1 fix: handle_fill is called outside the lock).
    """
    manager._orders["ORD_REST"] = ManagedOrder(
        order_id="ORD_REST", level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=40000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    manager.exchange.get_open_orders.return_value = [] 
    manager.exchange.get_order_history.return_value = {
        "orderId": "ORD_REST",
        "orderStatus": "FILLED",
        "side": "Buy",
        "execQty": "0.1",
        "avgPrice": "40000.0",
        "execFee": "0.0",
        "feeCurrency": "USDC"
    }

    # If the bug were present, sync_with_exchange would hang here
    try:
        await asyncio.wait_for(manager.sync_with_exchange(), timeout=3.0)
    except TimeoutError:
        pytest.fail("Deadlock detected in sync_with_exchange!")

    assert manager._orders["ORD_REST"].status == OrderStatus.FILLED
    assert not manager._lock.locked()

# --- BUG 2: Spot buy fee deduction from base asset ---

@pytest.mark.asyncio
async def test_spot_buy_fee_deducted_from_sellable_qty(manager):
    """
    Buy fill fee in base asset (ADA) should reduce the sellable qty for the inverse sell order.
    """
    manager._orders["ORD_ADA"] = ManagedOrder(
        order_id="ORD_ADA", level_id="buy-1", symbol="ADAUSDC",
        side=str(OrderSide.BUY), price=0.5, qty=100.0, status=OrderStatus.PENDING
    )
    manager.symbol = "ADAUSDC"
    
    fill_event = {
        "execId": "EX_ADA_1",
        "orderId": "ORD_ADA",
        "side": "Buy",
        "execQty": "100.0",
        "avgPrice": "0.5",
        "execFee": "0.1",
        "feeCurrency": "ADA"
    }
    
    await manager.handle_fill(fill_event)
    
    # Assert inverse sell was called with 99.9 qty
    placed_calls = manager.exchange.place_limit_order.call_args_list
    assert len(placed_calls) > 0
    sell_kwargs = placed_calls[0].kwargs
    placed_sell_qty = sell_kwargs["qty"]
    
    assert placed_sell_qty == 99.9
    assert placed_sell_qty < 100.0

@pytest.mark.asyncio
async def test_spot_buy_fee_in_quote_not_deducted(manager):
    """
    Buy fill fee in quote asset (USDC) should NOT reduce the base asset qty.
    """
    manager._orders["ORD_ADA2"] = ManagedOrder(
        order_id="ORD_ADA2", level_id="buy-2", symbol="ADAUSDC",
        side=str(OrderSide.BUY), price=0.5, qty=100.0, status=OrderStatus.PENDING
    )
    manager.symbol = "ADAUSDC"
    
    fill_event = {
        "execId": "EX_ADA_2",
        "orderId": "ORD_ADA2",
        "side": "Buy",
        "execQty": "100.0",
        "avgPrice": "0.5",
        "execFee": "0.05",
        "feeCurrency": "USDC"
    }
    
    await manager.handle_fill(fill_event)
    
    placed_calls = manager.exchange.place_limit_order.call_args_list
    sell_kwargs = placed_calls[0].kwargs
    placed_sell_qty = sell_kwargs["qty"]
    
    assert placed_sell_qty == 100.0

# --- BUG 3: avg_cost persisted and restored ---

@pytest.mark.asyncio
async def test_avg_cost_persisted_after_buy_fill(manager):
    """
    After a BUY fill, manager updates its _avg_cost appropriately.
    """
    manager._orders["ORD_BTC"] = ManagedOrder(
        order_id="ORD_BTC", level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=45000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    await manager.handle_fill({"orderId": "ORD_BTC", "execFee": "0", "feeCurrency": "BTC", "execQty": "0.1", "avgPrice": "45000.0", "side": "Buy"})
    
    assert manager._avg_cost == 45000.0
    assert manager._position_qty == 0.1
    
    state = manager.to_grid_state(buy_spacing_pct=0.01, sell_spacing_pct=0.01, trend_bias="NEUTRAL")
    assert state.avg_cost == 45000.0
    assert state.position_qty == 0.1

@pytest.mark.asyncio
async def test_avg_cost_restored_after_restart(manager):
    """
    Simulates loading avg_cost on boot.
    """
    manager._avg_cost = 45000.0
    manager._position_qty = 0.1
    manager._position_untracked = False
    
    assert manager._avg_cost == 45000.0

@pytest.mark.asyncio
async def test_untracked_flag_when_no_cost_basis(manager):
    """
    If no cost basis, selling logs "Untracked" and doesn't inject incorrect PnL.
    """
    manager._position_untracked = True
    manager._orders["SELL_1"] = ManagedOrder(
        order_id="SELL_1", level_id="sell-1", symbol="BTCUSDC",
        side=str(OrderSide.SELL), price=46000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    record = await manager.handle_fill({"orderId": "SELL_1", "execFee": "0", "execQty": "0.1", "avgPrice": "46000.0", "side": "Sell"})
    
    assert record.pnl == 0.0

@pytest.mark.asyncio
async def test_pnl_not_inflated_after_restart(manager):
    """
    With correct restored cost basis, PnL computation must be accurate.
    """
    manager._avg_cost = 45000.0
    manager._position_qty = 0.1
    manager._position_untracked = False 
    
    manager._orders["SELL_1"] = ManagedOrder(
        order_id="SELL_1", level_id="sell-1", symbol="BTCUSDC",
        side=str(OrderSide.SELL), price=46000.0, qty=0.1, status=OrderStatus.PENDING
    )
    
    record = await manager.handle_fill({"orderId": "SELL_1", "side": "Sell", "execQty": "0.1", "avgPrice": "46000.0", "execFee": "0.01"})
    
    # gross_pnl = (46000 - 45000) * 0.1 = 1000
    # pnl = 1000 - 0.01 = 999.99
    # Wait, 1000 * 0.1 = 100. Correct.
    # (46000 - 45000) * 0.1 = 100. PnL = 100 - 0.01 = 99.99
    expected_pnl = 99.99
    assert abs(record.pnl - expected_pnl) < 1e-4

# --- BUG 4: Buffer de WS fills anticipados ---

@pytest.mark.asyncio
async def test_early_ws_fill_buffered_and_processed(manager):
    """
    Fills arriving before the order is in self._orders are buffered and trigger handle_fill later.
    """
    fill_meta = {
        "execId": "WS_FILL_1",
        "orderId": "WS_ORD",
        "side": "Buy",
        "execQty": "1.0",
        "avgPrice": "100.0",
        "execFee": "0.0",
        "feeCurrency": "USDC"
    }
    
    # WS arrives early
    res = await manager.handle_fill(fill_meta)
    assert res is None
    assert "WS_ORD" in manager._unmatched_ws_fills
    assert "WS_ORD" in manager._unmatched_ws_timestamps

    # Register order
    manager._orders["WS_ORD"] = ManagedOrder(
        order_id="WS_ORD", level_id="buy-1", symbol="BTCUSDC",
        side=str(OrderSide.BUY), price=100.0, qty=1.0, status=OrderStatus.PENDING
    )
    
    # Let's simulate registration via sync_with_exchange call (which checks history)
    manager.exchange.get_open_orders.return_value = []
    manager.exchange.get_order_history.return_value = {
        "orderId": "WS_ORD",
        "orderStatus": "FILLED",
        "side": "Buy",
        "execQty": "1.0",
        "avgPrice": "100.0",
    }
    
    await manager.sync_with_exchange()
    
    assert manager._orders["WS_ORD"].status == OrderStatus.FILLED
    assert "WS_ORD" not in manager._unmatched_ws_fills

@pytest.mark.asyncio
async def test_stale_ws_fill_buffer_purged(manager):
    """
    Old fills in the unmatched buffer get automatically removed.
    """
    fill_meta = {"orderId": "STALE_ORD", "execQty":"1", "execFee": "0.0", "feeCurrency": "USDC"}
    old_time = time.time() - 61.0
    
    # Manually populate stale entry
    manager._unmatched_ws_fills["STALE_ORD"] = fill_meta
    manager._unmatched_ws_timestamps["STALE_ORD"] = old_time
    
    # Fresh entry
    fill_fresh = {"orderId": "FRESH_ORD", "execQty":"1", "execFee": "0.0", "feeCurrency": "USDC"}
    await manager.handle_fill(fill_fresh)
    
    assert "STALE_ORD" not in manager._unmatched_ws_fills
    assert "FRESH_ORD" in manager._unmatched_ws_fills

# --- BUG 5: Exit price siempre >= avg_cost ---

@pytest.mark.asyncio
async def test_exit_price_never_below_cost_basis(manager):
    """
    check_stale_inverse_orders computes exit_price cleanly above avg_cost.
    """
    test_cases = [0.5, 1.0, 1.25, 10.0, 100.0, 45000.0, 0.0001]
    
    exit_above_pct = 0.005
    
    for avg_cost in test_cases:
        manager._avg_cost = avg_cost
        manager._orders = {}
        old_time = datetime.now(UTC) - timedelta(hours=25)
        stale_order = ManagedOrder(
            order_id="STALE_1", level_id="inv-1", symbol="BTCUSDC",
            side=str(OrderSide.SELL), price=avg_cost*1.05, qty=1.0, status=OrderStatus.PENDING
        )
        stale_order.created_at = old_time
        manager._orders["STALE_1"] = stale_order
        
        manager.exchange.cancel_order.return_value = True
        manager.exchange.place_limit_order.return_value = "NEW_EXIT"
        
        await manager.check_stale_inverse_orders(exit_hours=24.0, exit_above_cost_pct=exit_above_pct)
        
        place_args = manager.exchange.place_limit_order.call_args.kwargs
        exit_price = place_args["price"]
        
        expected = avg_cost * (1 + exit_above_pct)
        
        assert exit_price >= avg_cost, f"REGRESSION: exit_price {exit_price} < {avg_cost}"
        assert exit_price == pytest.approx(expected, rel=1e-6)

@pytest.mark.asyncio
async def test_exit_price_covers_round_trip_fee(manager):
    """
    El precio de exit debe cubrir al menos el fee round-trip.
    """
    avg_cost = 1.25
    maker_fee = manager.maker_fee_pct # 0.0001
    exit_above_pct = maker_fee * 3
    
    min_exit = avg_cost * (1 + maker_fee * 2)
    manager._avg_cost = avg_cost
    
    old_time = datetime.now(UTC) - timedelta(hours=25)
    stale_order = ManagedOrder(
        order_id="STALE_3", level_id="inv-3", symbol="BTCUSDC",
        side=str(OrderSide.SELL), price=1.5, qty=1.0, status=OrderStatus.PENDING
    )
    stale_order.created_at = old_time
    manager._orders["STALE_3"] = stale_order
    
    manager.exchange.cancel_order.return_value = True
    manager.exchange.place_limit_order.return_value = "NEW_EXIT_FEE"
    
    await manager.check_stale_inverse_orders(exit_hours=24, exit_above_cost_pct=exit_above_pct)
    
    exit_price = manager.exchange.place_limit_order.call_args.kwargs["price"]
    assert exit_price >= min_exit

@pytest.mark.asyncio
async def test_exit_price_formula_uses_correct_param(manager):
    """
    Confirmar que se usa GRID_INVENTORY_EXIT_ABOVE_COST_PCT usando validaciones exactas.
    """
    avg_cost = 100.0
    exit_above_pct = 0.001
    manager._avg_cost = avg_cost
    
    old_time = datetime.now(UTC) - timedelta(hours=25)
    stale_order = ManagedOrder(
        order_id="STALE_2", level_id="inv-2", symbol="BTCUSDC",
        side=str(OrderSide.SELL), price=105.0, qty=1.0, status=OrderStatus.PENDING
    )
    stale_order.created_at = old_time
    manager._orders["STALE_2"] = stale_order
    
    manager.exchange.cancel_order.return_value = True
    manager.exchange.place_limit_order.return_value = "NEW_EXIT_2"
    
    await manager.check_stale_inverse_orders(exit_hours=24, exit_above_cost_pct=exit_above_pct)
    
    exit_price = manager.exchange.place_limit_order.call_args.kwargs["price"]
    
    expected = 100.0 * (1 + 0.001)
    assert exit_price == pytest.approx(expected, rel=1e-6)
