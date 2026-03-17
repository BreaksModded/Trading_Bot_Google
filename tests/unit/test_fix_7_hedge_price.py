"""FIX BUG-7: Tests for hedge sell price market floor.

Validates that:
1. When avg_cost is below market, hedge price is adjusted up to current_price * 1.001
2. When avg_cost is above market, hedge price uses normal calculation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.order_manager import OrderManager


@pytest.fixture
def mock_exchange():
    """Minimal mock exchange."""
    ex = AsyncMock()
    ex.place_limit_order = AsyncMock(return_value="test-order")
    ex.get_spot_symbol_rules = AsyncMock(return_value=MagicMock(
        min_qty=0.01, min_notional=5.0, qty_step=0.01,
    ))
    return ex


def create_bot(mock_exchange):
    """Create a minimal bot with an order manager."""
    bot = MagicMock()
    bot.exchange = mock_exchange  # Critical: _hedge_unhedged_inventory uses self.exchange
    bot.quote_coin = "USDC"
    
    # We only need the order manager setup
    mgr = OrderManager(
        exchange=mock_exchange,
        symbol="SOLUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.006,
    )
    # Set the manager up as unhedged
    mgr._position_qty = 1.0
    mgr._position_untracked = False
    
    bot.order_managers = {"SOLUSDC": mgr}
    bot._log_and_notify = AsyncMock()
    
    # We need the real _hedge_unhedged_inventory logic to test the price
    from main import TradingBot
    bot._hedge_unhedged_inventory = TradingBot._hedge_unhedged_inventory.__get__(bot, TradingBot)
    
    return bot, mgr


class TestHedgePriceFloor:
    """Tests that the hedge price handles market spikes correctly."""

    @pytest.mark.asyncio
    async def test_hedge_price_adjusted_when_below_market(self, mock_exchange):
        """When the market price spikes above the cost basis, the hedge SELL
        must not be placed below the current market price, or the exchange
        will reject it (e.g. Bybit ErrCode 170194).
        """
        bot, mgr = create_bot(mock_exchange)
        
        # Setup: avg_cost is lower than market
        mgr._avg_cost = 87.0
        mgr._sell_spacing_pct = 0.006
        
        # current_price is 95.0
        signal = MagicMock()
        signal.current_price = 95.0
        
        # Run the hedge
        await bot._hedge_unhedged_inventory(
            signals={"SOLUSDC": signal},
            free_balances={"SOL": 10.0}
        )
        
        # Verify the price used
        mock_exchange.place_limit_order.assert_called_once()
        call_kwargs = mock_exchange.place_limit_order.call_args.kwargs
        placed_price = call_kwargs["price"]
        
        # The normal calculation would be 87.0 * 1.006 = 87.522
        # But the market floor should be used: 95.0 * 1.001 = 95.095
        expected_floor = 95.0 * 1.001
        
        assert placed_price == expected_floor, (
            f"Expected adjusted price {expected_floor}, but got {placed_price}"
        )
        assert placed_price > 87.522  # Must be strictly greater than normal calc

    @pytest.mark.asyncio
    async def test_hedge_price_normal_when_above_market(self, mock_exchange):
        """When the market price drops below the cost basis (normal drawn-down state),
        the hedge SELL uses the standard breakeven + spacing calculation.
        """
        bot, mgr = create_bot(mock_exchange)
        
        # Setup: avg_cost is higher than market
        mgr._avg_cost = 97.0
        mgr._sell_spacing_pct = 0.006
        
        # current_price is 95.0
        signal = MagicMock()
        signal.current_price = 95.0
        
        # Run the hedge
        await bot._hedge_unhedged_inventory(
            signals={"SOLUSDC": signal},
            free_balances={"SOL": 10.0}
        )
        
        # Verify the price used
        mock_exchange.place_limit_order.assert_called_once()
        call_kwargs = mock_exchange.place_limit_order.call_args.kwargs
        placed_price = call_kwargs["price"]
        
        # The normal calculation should be used: 97.0 * 1.006 = 97.582
        # Market floor is 95.0 * 1.001 = 95.095
        expected_normal = 97.0 * 1.006
        
        assert placed_price == expected_normal, (
            f"Expected normal calc price {expected_normal}, but got {placed_price}"
        )
        assert placed_price > 95.095  # The normal calc was higher than floor
