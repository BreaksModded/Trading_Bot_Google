"""FIX BUG-8: Tests for hedge sell qty balance capping.

Validates that:
1. When internal qty > exchange free balance, sell_qty is capped to real_balance.
2. When internal qty <= exchange free balance, sell_qty remains as position_qty.
3. When resulting sell_qty < min_qty, the hedge is skipped.
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
    bot.exchange = mock_exchange
    bot.quote_coin = "USDC"
    
    mgr = OrderManager(
        exchange=mock_exchange,
        symbol="SOLUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.006,
    )
    mgr._position_untracked = False
    
    bot.order_managers = {"SOLUSDC": mgr}
    bot._log_and_notify = AsyncMock()
    
    # We need the real _hedge_unhedged_inventory logic to test the capping
    from main import TradingBot
    bot._hedge_unhedged_inventory = TradingBot._hedge_unhedged_inventory.__get__(bot, TradingBot)
    
    return bot, mgr


class TestHedgeBalanceCapping:
    """Tests that the hedge quantity respects the real exchange balance."""

    @pytest.mark.asyncio
    async def test_hedge_uses_real_balance_when_less_than_internal(self, mock_exchange):
        """When the internal position_qty is 0.5578 but Bybit only has 0.30 available,
        the SELL order must be capped to 0.30 to avoid ErrCode 170131.
        """
        bot, mgr = create_bot(mock_exchange)
        
        # Setup: internal 0.5578, real 0.30
        mgr._position_qty = 0.5578
        mgr._avg_cost = 90.0
        
        signal = MagicMock()
        signal.current_price = 95.0
        
        free_balances = {"SOL": 0.30}
        
        # Run the hedge
        await bot._hedge_unhedged_inventory(
            signals={"SOLUSDC": signal},
            free_balances=free_balances
        )
        
        # Verify call qty
        mock_exchange.place_limit_order.assert_called_once()
        call_kwargs = mock_exchange.place_limit_order.call_args.kwargs
        assert call_kwargs["qty"] == 0.30, f"Expected capped qty 0.30, got {call_kwargs['qty']}"

    @pytest.mark.asyncio
    async def test_hedge_uses_internal_when_less_than_real_balance(self, mock_exchange):
        """When everything is normal (position_qty 0.30 and exchange has 1.0),
        use the standard position_qty.
        """
        bot, mgr = create_bot(mock_exchange)
        
        # Setup: internal 0.30, real 1.0
        mgr._position_qty = 0.30
        mgr._avg_cost = 90.0
        
        signal = MagicMock()
        signal.current_price = 95.0
        
        free_balances = {"SOL": 1.0}
        
        # Run
        await bot._hedge_unhedged_inventory(
            signals={"SOLUSDC": signal},
            free_balances=free_balances
        )
        
        # Verify
        mock_exchange.place_limit_order.assert_called_once()
        call_kwargs = mock_exchange.place_limit_order.call_args.kwargs
        assert call_kwargs["qty"] == 0.30

    @pytest.mark.asyncio
    async def test_hedge_skips_when_qty_below_minimum(self, mock_exchange):
        """If capping results in a qty below exchange minimum, skip to avoid ErrCode 170131."""
        bot, mgr = create_bot(mock_exchange)
        
        # Rules min_qty is 0.01 (from mock_exchange fixture)
        # Setup: exchange has only 0.005
        mgr._position_qty = 0.1
        mgr._avg_cost = 90.0
        
        signal = MagicMock()
        signal.current_price = 95.0
        
        free_balances = {"SOL": 0.005}
        
        # Run
        await bot._hedge_unhedged_inventory(
            signals={"SOLUSDC": signal},
            free_balances=free_balances
        )
        
        # Verify NO order was placed
        mock_exchange.place_limit_order.assert_not_called()
