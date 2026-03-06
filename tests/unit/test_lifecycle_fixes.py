"""Tests for lifecycle fixes: shutdown(), emergency_stop(), and avg_cost reconstruction.

Fix 1: shutdown() cancels only entry BUY orders — preserves SELL exit orders.
Fix 2: avg_cost reconstructed from DB trade history on crash recovery.
Fix 3: emergency_stop() market-sells all inventory before shutting down.
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

from core.order_manager import OrderManager, ManagedOrder
from data.models import OrderSide, OrderStatus
from main import _reconstruct_avg_cost


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_manager(exchange, symbol="BTCUSDC"):
    return OrderManager(
        exchange=exchange,
        symbol=symbol,
        maker_fee_pct=0.001,
        max_open_orders=20,
        default_spacing_pct=0.006,
    )


def _add_order(manager: OrderManager, order_id: str, side: str, level_id: str, price: float = 30000.0, qty: float = 0.001) -> ManagedOrder:
    """Directly inject a PENDING order into a manager's internal state."""
    order = ManagedOrder(
        order_id=order_id,
        level_id=level_id,
        symbol=manager.symbol,
        side=side,
        price=price,
        qty=qty,
    )
    order.status = OrderStatus.PENDING
    manager._orders[order_id] = order
    return order


def _make_trade_row(side: str, price: float, qty: float, timestamp: str) -> dict:
    return {
        "side": side,
        "price": price,
        "qty": qty,
        "timestamp": timestamp,
        "fee": 0.001,
        "pnl": 0.0,
        "symbol": "BTCUSDC",
    }


# ── FIX 1: shutdown() preserves SELL exit orders ──────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_preserves_sell_orders(mock_exchange):
    """shutdown() cancels BUY entries but NOT SELL exit (inverse) orders."""
    manager = _make_manager(mock_exchange)

    buy1 = _add_order(manager, "buy-ord-1", "Buy", "entry-buy-1-30000-111")
    buy2 = _add_order(manager, "buy-ord-2", "Buy", "entry-buy-2-29800-111")
    sell1 = _add_order(manager, "sell-ord-1", "Sell", "inv-buy-1-30000-111", price=30180.0)
    sell2 = _add_order(manager, "sell-ord-2", "Sell", "inverse-buy-2-29800-111", price=29980.0)

    mock_exchange.cancel_order = AsyncMock()

    await manager.cancel_entry_orders_only()

    cancelled_ids = {c.kwargs["order_id"] for c in mock_exchange.cancel_order.call_args_list}
    assert "buy-ord-1" in cancelled_ids
    assert "buy-ord-2" in cancelled_ids
    assert "sell-ord-1" not in cancelled_ids, "SELL exit order must NOT be cancelled on shutdown"
    assert "sell-ord-2" not in cancelled_ids, "SELL exit order must NOT be cancelled on shutdown"

    assert manager._orders["sell-ord-1"].status == OrderStatus.PENDING
    assert manager._orders["sell-ord-2"].status == OrderStatus.PENDING
    assert manager._orders["buy-ord-1"].status == OrderStatus.CANCELED
    assert manager._orders["buy-ord-2"].status == OrderStatus.CANCELED


@pytest.mark.asyncio
async def test_shutdown_with_no_open_orders(mock_exchange):
    """cancel_entry_orders_only() with no open orders completes without error."""
    manager = _make_manager(mock_exchange)
    mock_exchange.cancel_order = AsyncMock()

    count = await manager.cancel_entry_orders_only()

    assert count == 0
    mock_exchange.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_with_only_sell_orders(mock_exchange):
    """cancel_entry_orders_only() with only SELL orders does not cancel anything."""
    manager = _make_manager(mock_exchange)
    _add_order(manager, "sell-ord-1", "Sell", "inv-buy-1-30000-111", price=30180.0)
    _add_order(manager, "sell-ord-2", "Sell", "inverse-buy-2-29800-111", price=29980.0)
    mock_exchange.cancel_order = AsyncMock()

    count = await manager.cancel_entry_orders_only()

    assert count == 0
    mock_exchange.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_cancels_only_entry_not_all(mock_exchange):
    """cancel_entry_orders_only() returns count of cancelled entries, not total orders."""
    manager = _make_manager(mock_exchange)
    _add_order(manager, "buy-ord-1", "Buy", "entry-buy-1-30000-111")
    _add_order(manager, "sell-ord-1", "Sell", "inv-buy-1-30000-111", price=30180.0)
    mock_exchange.cancel_order = AsyncMock()

    count = await manager.cancel_entry_orders_only()

    assert count == 1


# ── FIX 2: avg_cost reconstruction from DB ────────────────────────────────────


def test_reconstruct_avg_cost_single_buy():
    """Single BUY trade → avg_cost equals that trade's price."""
    trades = [
        _make_trade_row("Buy", 85000.0, 0.001, "2026-03-05T10:00:00"),
    ]
    result = _reconstruct_avg_cost(trades)
    assert abs(result - 85000.0) < 0.01


def test_reconstruct_avg_cost_multiple_buys_weighted():
    """Multiple BUYs → weighted average, not simple average."""
    trades = [
        _make_trade_row("Buy", 86000.0, 0.002, "2026-03-05T11:00:00"),
        _make_trade_row("Buy", 84000.0, 0.001, "2026-03-05T10:00:00"),
    ]
    # weighted: (86000*0.002 + 84000*0.001) / 0.003 = (172 + 84) / 0.003 = 256/0.003 ≈ 85333.33
    expected = (86000.0 * 0.002 + 84000.0 * 0.001) / 0.003
    result = _reconstruct_avg_cost(trades)
    assert abs(result - expected) < 0.01


def test_reconstruct_avg_cost_uses_only_buys_after_last_sell():
    """BUY trades before the last SELL are ignored — only uses the current cycle."""
    trades = [
        # Most recent first (DESC order from DB)
        _make_trade_row("Buy", 86000.0, 0.001, "2026-03-05T12:00:00"),  # after sell → use
        _make_trade_row("Sell", 85500.0, 0.001, "2026-03-05T11:00:00"),  # last sell marker
        _make_trade_row("Buy", 84000.0, 0.001, "2026-03-05T10:00:00"),  # before sell → ignore
    ]
    result = _reconstruct_avg_cost(trades)
    assert abs(result - 86000.0) < 0.01, "Should only use BUY after the last SELL"


def test_reconstruct_avg_cost_no_buys_after_sell_returns_zero():
    """If only SELLs exist (no BUYs after last SELL), returns 0.0."""
    trades = [
        _make_trade_row("Sell", 85500.0, 0.001, "2026-03-05T11:00:00"),
        _make_trade_row("Buy", 84000.0, 0.001, "2026-03-05T10:00:00"),
    ]
    result = _reconstruct_avg_cost(trades)
    assert result == 0.0


def test_reconstruct_avg_cost_empty_trades_returns_zero():
    """Empty trade list → returns 0.0 (fallback to untracked)."""
    result = _reconstruct_avg_cost([])
    assert result == 0.0


def test_reconstruct_avg_cost_ignores_invalid_rows():
    """Rows with price=0 or qty=0 are skipped."""
    trades = [
        _make_trade_row("Buy", 0.0, 0.001, "2026-03-05T11:00:00"),    # invalid price
        _make_trade_row("Buy", 85000.0, 0.0, "2026-03-05T10:30:00"),  # invalid qty
        _make_trade_row("Buy", 85000.0, 0.001, "2026-03-05T10:00:00"),  # valid
    ]
    result = _reconstruct_avg_cost(trades)
    assert abs(result - 85000.0) < 0.01


def test_reconstruct_avg_cost_no_sell_uses_all_buys():
    """With no SELL trades at all, all BUYs are used."""
    trades = [
        _make_trade_row("Buy", 86000.0, 0.001, "2026-03-05T11:00:00"),
        _make_trade_row("Buy", 84000.0, 0.001, "2026-03-05T10:00:00"),
    ]
    expected = (86000.0 * 0.001 + 84000.0 * 0.001) / 0.002
    result = _reconstruct_avg_cost(trades)
    assert abs(result - expected) < 0.01


# ── FIX 3: emergency_stop() liquidates inventory ──────────────────────────────


@pytest.mark.asyncio
async def test_emergency_stop_liquidates_all_inventory(mock_exchange):
    """emergency_stop() calls place_market_order for each symbol with open inventory."""
    manager1 = _make_manager(mock_exchange, "BTCUSDC")
    manager1._position_qty = 0.001
    manager1._avg_cost = 85000.0

    manager2 = _make_manager(mock_exchange, "XRPUSDC")
    manager2._position_qty = 50.0
    manager2._avg_cost = 2.5

    mock_exchange.place_market_order = AsyncMock()
    mock_exchange.cancel_all_orders = AsyncMock()
    mock_exchange.stop_websockets = AsyncMock()

    # Build a minimal TradingBot-like object to test the logic in isolation
    bot = _make_minimal_bot(
        order_managers={"BTCUSDC": manager1, "XRPUSDC": manager2},
        exchange=mock_exchange,
        emergency_liquidate=True,
    )

    await bot.emergency_stop(reason="test_emergency")

    calls = mock_exchange.place_market_order.call_args_list
    sold_symbols = {c.kwargs["symbol"] for c in calls}
    assert "BTCUSDC" in sold_symbols
    assert "XRPUSDC" in sold_symbols
    assert manager1._position_qty == 0.0
    assert manager1._avg_cost == 0.0
    assert manager2._position_qty == 0.0
    assert manager2._avg_cost == 0.0


@pytest.mark.asyncio
async def test_emergency_stop_skips_symbols_without_inventory(mock_exchange):
    """emergency_stop() does not call place_market_order for symbols with no inventory."""
    manager = _make_manager(mock_exchange, "BTCUSDC")
    manager._position_qty = 0.0  # no inventory

    mock_exchange.place_market_order = AsyncMock()
    mock_exchange.cancel_all_orders = AsyncMock()
    mock_exchange.stop_websockets = AsyncMock()

    bot = _make_minimal_bot(
        order_managers={"BTCUSDC": manager},
        exchange=mock_exchange,
        emergency_liquidate=True,
    )

    await bot.emergency_stop(reason="test_emergency")

    mock_exchange.place_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_emergency_stop_continues_if_one_sell_fails(mock_exchange):
    """If market-sell fails for one symbol, emergency_stop continues with the rest."""
    manager1 = _make_manager(mock_exchange, "BTCUSDC")
    manager1._position_qty = 0.001

    manager2 = _make_manager(mock_exchange, "XRPUSDC")
    manager2._position_qty = 50.0

    async def _fail_first(*, symbol, **kw):
        if symbol == "BTCUSDC":
            raise RuntimeError("Exchange rejected market order")

    mock_exchange.place_market_order = AsyncMock(side_effect=_fail_first)
    mock_exchange.cancel_all_orders = AsyncMock()
    mock_exchange.stop_websockets = AsyncMock()

    bot = _make_minimal_bot(
        order_managers={"BTCUSDC": manager1, "XRPUSDC": manager2},
        exchange=mock_exchange,
        emergency_liquidate=True,
    )

    # Must not raise — emergency_stop swallows per-symbol errors
    await bot.emergency_stop(reason="test_emergency")

    calls = mock_exchange.place_market_order.call_args_list
    sold_symbols = {c.kwargs["symbol"] for c in calls}
    assert "XRPUSDC" in sold_symbols  # second symbol attempted despite first failure


@pytest.mark.asyncio
async def test_emergency_stop_with_liquidation_disabled(mock_exchange):
    """With emergency_liquidate_inventory=False, no market-sell is executed."""
    manager = _make_manager(mock_exchange, "BTCUSDC")
    manager._position_qty = 0.001

    mock_exchange.place_market_order = AsyncMock()
    mock_exchange.cancel_all_orders = AsyncMock()
    mock_exchange.stop_websockets = AsyncMock()

    bot = _make_minimal_bot(
        order_managers={"BTCUSDC": manager},
        exchange=mock_exchange,
        emergency_liquidate=False,  # disabled
    )

    await bot.emergency_stop(reason="test_emergency")

    mock_exchange.place_market_order.assert_not_called()


# ── Minimal bot stub for emergency_stop() testing ────────────────────────────


def _make_minimal_bot(order_managers, exchange, emergency_liquidate: bool):
    """Return a minimal object that implements the emergency_stop() logic under test."""
    import asyncio
    from data.models import BotStatus

    class _Settings:
        class risk:
            emergency_liquidate_inventory = emergency_liquidate

    class _DB:
        def update_bot_state(self, **kw): pass
        def stop_writer(self): pass

    class _MinimalBot:
        def __init__(self):
            self.order_managers = order_managers
            self.exchange = exchange
            self.settings = _Settings()
            self.db = _DB()
            self._shutdown_event = asyncio.Event()
            self.state = MagicMock()
            self.state.running = True

        async def _log_and_notify(self, *a, **kw): pass

        # Import the real emergency_stop implementation
        from main import TradingBot
        emergency_stop = TradingBot.emergency_stop

    return _MinimalBot()
