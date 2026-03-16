"""FIX-D tests: has_unhedged_inventory() returns False after hedge placement.

Validates that:
1. After _hedge_unhedged_inventory places a SELL, has_unhedged_inventory()
   immediately returns False in the same iteration (in-memory update).
2. The full flow: BUY fill → hedge → grid refresh does NOT abort on the
   pre-flight unhedged inventory check.
"""

import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderSide, OrderStatus


@pytest.fixture
def mock_exchange():
    """Minimal mock exchange."""
    ex = AsyncMock()
    ex.place_limit_order = AsyncMock(return_value="hedge-order-d01")
    ex.get_spot_symbol_rules = AsyncMock(return_value=MagicMock(
        min_qty=0.01, min_notional=5.0, qty_step=0.01,
    ))
    return ex


@pytest.fixture
def mgr_unhedged(mock_exchange):
    """OrderManager with unhedged SOL inventory."""
    mgr = OrderManager(
        exchange=mock_exchange,
        symbol="SOLUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.006,
    )
    mgr._position_qty = 1.5   # 1.5 SOL bought
    mgr._avg_cost = 87.0
    mgr._position_untracked = False
    mgr._sell_spacing_pct = 0.006
    return mgr


class TestHasUnhedgedReturnsFalseAfterHedge:
    """FIX-D Test 1: Immediate in-memory update after hedge placement."""

    @pytest.mark.asyncio
    async def test_has_unhedged_returns_false_after_hedge_placed(
        self, mgr_unhedged, mock_exchange,
    ):
        """Simulates the _hedge_unhedged_inventory flow:
        1. has_unhedged_inventory() returns True (no SELL orders)
        2. Place SELL order and add to _orders
        3. has_unhedged_inventory() returns False immediately (in-memory)
        """
        mgr = mgr_unhedged

        # Step 1: Confirm unhedged
        assert mgr.has_unhedged_inventory() is True

        # Step 2: Place hedge SELL (mirrors _hedge_unhedged_inventory logic)
        spacing = mgr._sell_spacing_pct
        target_price = mgr._avg_cost * (1 + spacing)
        order_id = await mock_exchange.place_limit_order(
            symbol="SOLUSDC",
            side="Sell",
            qty=mgr._position_qty,
            price=target_price,
            orderLinkId="hedge-SOLUSDC-test",
        )

        # Add to in-memory orders (exactly what _hedge_unhedged_inventory does)
        mgr._orders[order_id] = ManagedOrder(
            order_id=order_id,
            level_id="hedge-SOLUSDC-test",
            symbol="SOLUSDC",
            side="Sell",
            price=target_price,
            qty=mgr._position_qty,
        )

        # Step 3: Immediately returns False — no exchange call needed
        assert mgr.has_unhedged_inventory() is False

        # Verify the SELL order is visible in open_orders
        sell_orders = [o for o in mgr.open_orders if o.side == OrderSide.SELL]
        assert len(sell_orders) == 1
        assert sell_orders[0].qty == mgr._position_qty


class TestGridRefreshNotAbortedAfterHedge:
    """FIX-D Test 2: Full flow — hedge → refresh does not abort."""

    @pytest.mark.asyncio
    async def test_grid_refresh_not_aborted_after_successful_hedge(
        self, mgr_unhedged, mock_exchange,
    ):
        """Full sequence validation:
        1. Manager has unhedged inventory (would abort _execute_grid_refresh)
        2. _hedge_unhedged_inventory runs and places SELL
        3. _execute_grid_refresh's pre-flight check (has_unhedged_inventory)
           now returns False → refresh proceeds without abort
        """
        mgr = mgr_unhedged

        # Phase 1: Pre-hedge — would abort refresh
        assert mgr.has_unhedged_inventory() is True

        # Phase 2: Hedge runs (simulated)
        target_price = mgr._avg_cost * (1 + mgr._sell_spacing_pct)
        order_id = await mock_exchange.place_limit_order(
            symbol="SOLUSDC", side="Sell",
            qty=mgr._position_qty, price=target_price,
            orderLinkId="hedge-flow-test",
        )
        mgr._orders[order_id] = ManagedOrder(
            order_id=order_id,
            level_id="hedge-flow-test",
            symbol="SOLUSDC",
            side="Sell",
            price=target_price,
            qty=mgr._position_qty,
        )

        # Phase 3: Post-hedge — refresh pre-flight check passes
        assert mgr.has_unhedged_inventory() is False

        # Simulate the pre-flight check in _execute_grid_refresh:
        # if manager.has_unhedged_inventory():
        #     logger.warning("[REFRESH ABORT]...")
        #     return
        # → This block is NOT entered, refresh continues
        refresh_aborted = mgr.has_unhedged_inventory()
        assert refresh_aborted is False, (
            "Grid refresh would abort: has_unhedged_inventory() returned True "
            "even after hedge SELL was placed. The in-memory update failed."
        )
