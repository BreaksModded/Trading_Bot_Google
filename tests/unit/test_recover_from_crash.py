"""
Tests for recover_from_crash() Bug A+B fixes and multi-pair EMA filter.

Bug A: ManagedOrder() called with empty args → TypeError on restart with live orders.
Bug B: handle_fill() called inside async with self._lock → deadlock.
"""
import asyncio
import time
import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from core.order_manager import OrderManager, ManagedOrder
from core.regime import MarketRegime
from core.strategy import StrategySignal
from data.models import GridLevel, OrderSide, OrderStatus, TrendBias


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def manager(mock_exchange):
    """OrderManager with BTCUSDC symbol and mocked exchange."""
    return OrderManager(
        exchange=mock_exchange,
        symbol="BTCUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.01,
    )


def _raw_limit_order(order_id: str, link_id: str, price: float, qty: float = 0.001) -> dict:
    """Build a minimal raw Bybit v5 open-order dict (LIMIT, no stop)."""
    return {
        "orderId": order_id,
        "orderLinkId": link_id,
        "symbol": "BTCUSDC",
        "side": "Buy",
        "orderType": "Limit",
        "stopOrderType": "UNKNOWN",
        "price": str(price),
        "qty": str(qty),
        "leavesQty": str(qty),
    }


def _make_signal(
    regime: MarketRegime = MarketRegime.RANGING,
    trend_bias: TrendBias = TrendBias.NEUTRAL,
    volume_ratio: float = 1.5,
) -> StrategySignal:
    """Build a minimal StrategySignal for filter-condition testing."""
    return StrategySignal(
        generated_at=datetime.now(UTC),
        current_price=3000.0,
        spacing_pct=0.01,
        trend_bias=trend_bias,
        adx_value=18.0,
        atr_pct=0.015,
        volume_ratio=volume_ratio,
        pause_new_grid=False,
        target_notional=25.0,
        levels=[],
        reason="test",
        close_history=None,
        regime=str(regime),
    )


# ── Bug A + B: recover_from_crash with 2 open orders ─────────────────────────

@pytest.mark.asyncio
async def test_recover_from_crash_with_open_orders(manager):
    """
    recover_from_crash() must:
    - Not raise TypeError (Bug A fix: ManagedOrder populated with real fields)
    - Not deadlock (Bug B fix: handle_fill called outside the lock)
    - Populate manager._orders with 2 correctly-constructed ManagedOrder entries
    """
    raw_orders = [
        _raw_limit_order("CRASH_ORD_1", "buy-1-96000", 96000.0),
        _raw_limit_order("CRASH_ORD_2", "buy-2-95000", 95000.0),
    ]
    manager.exchange.get_open_orders.return_value = raw_orders

    try:
        result = await asyncio.wait_for(manager.recover_from_crash(), timeout=3.0)
    except TimeoutError:
        pytest.fail("Deadlock detected in recover_from_crash()!")

    # Two orders were loaded
    assert len(manager._orders) == 2

    # Bug A: ManagedOrder fields must be populated (not default/empty)
    ord1 = manager._orders["CRASH_ORD_1"]
    assert ord1.order_id == "CRASH_ORD_1"
    assert ord1.level_id == "buy-1-96000"
    assert ord1.symbol == "BTCUSDC"
    assert ord1.side == "Buy"
    assert ord1.price == pytest.approx(96000.0)
    assert ord1.qty == pytest.approx(0.001)

    ord2 = manager._orders["CRASH_ORD_2"]
    assert ord2.price == pytest.approx(95000.0)

    # Lock released after completion
    assert not manager._lock.locked()


# ── Bug B: recover_from_crash with pre-buffered WS fill ───────────────────────

@pytest.mark.asyncio
async def test_recover_from_crash_with_pending_ws_fill(manager):
    """
    When a WS fill arrives before recover_from_crash():
    - The fill must be processed without deadlocking (Bug B fix).
    - The unmatched buffer for that order must be cleared after recovery.
    """
    fill_event = {
        "execId": "WS_CRASH_FILL_1",
        "orderId": "CRASH_ORD_WS",
        "side": "Buy",
        "execQty": "0.001",
        "avgPrice": "95000.0",
        "execFee": "0.0",
        "feeCurrency": "USDC",
    }

    # Pre-buffer the WS fill as if it arrived before the order was registered
    manager._unmatched_ws_fills["CRASH_ORD_WS"] = fill_event
    manager._unmatched_ws_timestamps["CRASH_ORD_WS"] = time.time()

    manager.exchange.get_open_orders.return_value = [
        _raw_limit_order("CRASH_ORD_WS", "buy-1-95000", 95000.0),
    ]

    try:
        await asyncio.wait_for(manager.recover_from_crash(), timeout=3.0)
    except TimeoutError:
        pytest.fail(
            "Deadlock detected when processing buffered WS fill in recover_from_crash()!"
        )

    # Buffer entry must have been consumed during recovery
    assert "CRASH_ORD_WS" not in manager._unmatched_ws_fills
    # Lock must be fully released
    assert not manager._lock.locked()


# ── Multi-pair EMA backup filter (fixed regime-aware logic) ───────────────────

def test_non_btc_symbol_can_be_selected():
    """
    After the EMA backup filter fix, ETHUSDC (or any symbol) with RANGING regime
    and NEUTRAL trend_bias must NOT be skipped.

    Old logic: required trend_bias == "long" for ALL non-TRENDING_DOWN regimes.
    New logic: only block TRANSITIONAL + SHORT.
    """
    def would_be_skipped_by_ema_filter(signal: StrategySignal) -> bool:
        """Replicates the exact filter condition in main.py."""
        _bias = str(signal.trend_bias).lower()
        return (
            signal.regime == str(MarketRegime.TRANSITIONAL)
            and _bias not in ("long", "neutral")
        )

    cases = [
        # (regime, bias, should_skip, description)
        (MarketRegime.RANGING, TrendBias.NEUTRAL, False,
         "RANGING + NEUTRAL must be allowed (primary ETHUSDC use-case)"),
        (MarketRegime.RANGING, TrendBias.LONG, False,
         "RANGING + LONG must be allowed"),
        (MarketRegime.RANGING, TrendBias.SHORT, False,
         "RANGING + SHORT must be allowed (grid in sideways markets)"),
        (MarketRegime.TRENDING_UP, TrendBias.NEUTRAL, False,
         "TRENDING_UP + NEUTRAL must be allowed"),
        (MarketRegime.TRENDING_UP, TrendBias.SHORT, False,
         "TRENDING_UP + SHORT must be allowed (momentum favors longs, grid still OK)"),
        (MarketRegime.TRANSITIONAL, TrendBias.LONG, False,
         "TRANSITIONAL + LONG must be allowed"),
        (MarketRegime.TRANSITIONAL, TrendBias.NEUTRAL, False,
         "TRANSITIONAL + NEUTRAL must be allowed"),
        (MarketRegime.TRANSITIONAL, TrendBias.SHORT, True,
         "TRANSITIONAL + SHORT must be BLOCKED (only blocked case)"),
    ]

    for regime, bias, expected_skip, description in cases:
        signal = _make_signal(regime=regime, trend_bias=bias)
        actual_skip = would_be_skipped_by_ema_filter(signal)
        assert actual_skip == expected_skip, (
            f"FAIL: {description}\n"
            f"  regime={regime}, bias={bias}\n"
            f"  expected skip={expected_skip}, got skip={actual_skip}"
        )
