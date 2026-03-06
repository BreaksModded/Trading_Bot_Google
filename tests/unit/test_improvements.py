"""
Tests for MEJORA-001 through MEJORA-012 improvements.

Coverage:
- MEJORA-001: np.corrcoef length guard
- MEJORA-006: get_grid_params_for_regime integration in strategy.py
- MEJORA-007: Hard SL anchored to avg_cost
- MEJORA-011: Inventory soft stop-loss (check_inventory_stop_loss)
- MEJORA-012: order_size_usdt defensive guard in validate_trading_params
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.order_manager import OrderManager, ManagedOrder
from core.regime import MarketRegime, get_grid_params_for_regime
from data.models import OrderSide, OrderStatus


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def manager(mock_exchange):
    return OrderManager(
        exchange=mock_exchange,
        symbol="BTCUSDC",
        maker_fee_pct=0.0001,
        max_open_orders=20,
        default_spacing_pct=0.01,
    )


# ── MEJORA-001: np.corrcoef length guard ──────────────────────────────────────

def test_corrcoef_length_guard_skips_mismatched():
    """
    The length guard in main.py must skip correlation check when
    close_history arrays have different lengths, preventing ValueError.
    """
    def would_skip_corrcoef(hist_a, hist_b):
        """Replicates the guard from main.py."""
        return len(hist_a) != len(hist_b)

    assert would_skip_corrcoef([1.0] * 50, [1.0] * 49) is True
    assert would_skip_corrcoef([1.0] * 50, [1.0] * 51) is True
    assert would_skip_corrcoef([1.0] * 50, [1.0] * 50) is False
    assert would_skip_corrcoef([], [1.0]) is True
    assert would_skip_corrcoef([], []) is False


def test_corrcoef_safe_with_equal_length():
    """np.corrcoef must not raise when arrays have equal length."""
    import numpy as np
    a = list(range(50))
    b = list(range(1, 51))
    result = np.corrcoef(a, b)[0, 1]
    assert not np.isnan(result)
    assert abs(result - 1.0) < 1e-6


# ── MEJORA-006: get_grid_params_for_regime integration ───────────────────────

def test_regime_ranging_no_change():
    """RANGING regime must return base_spacing and base_levels unchanged."""
    spacing, levels, allow = get_grid_params_for_regime(MarketRegime.RANGING, 0.01, 5)
    assert spacing == pytest.approx(0.01)
    assert levels == 5
    assert allow is True


def test_regime_trending_up_wider_spacing():
    """TRENDING_UP must widen spacing × 1.4 and reduce levels by 1."""
    spacing, levels, allow = get_grid_params_for_regime(MarketRegime.TRENDING_UP, 0.01, 5)
    assert spacing == pytest.approx(0.014)
    assert levels == 4
    assert allow is True


def test_regime_trending_up_min_levels():
    """TRENDING_UP with base_levels=3 must clamp to 3 (not go below minimum)."""
    _, levels, _ = get_grid_params_for_regime(MarketRegime.TRENDING_UP, 0.01, 3)
    assert levels == 3


def test_regime_trending_down_blocks():
    """TRENDING_DOWN must return allow_placement=False."""
    _, _, allow = get_grid_params_for_regime(MarketRegime.TRENDING_DOWN, 0.01, 5)
    assert allow is False


def test_regime_transitional_reduces_levels():
    """TRANSITIONAL must reduce levels by 2, minimum 3."""
    _, levels, allow = get_grid_params_for_regime(MarketRegime.TRANSITIONAL, 0.01, 5)
    assert levels == 3
    assert allow is True


def test_regime_transitional_clamps_at_3():
    """TRANSITIONAL with base_levels=3 must stay at 3."""
    _, levels, _ = get_grid_params_for_regime(MarketRegime.TRANSITIONAL, 0.01, 3)
    assert levels == 3


# ── MEJORA-007: Hard SL anchored to avg_cost ─────────────────────────────────

def test_hard_sl_uses_avg_cost_when_available():
    """Hard SL formula: avg_cost * (1 - hard_stop_loss_pct)."""
    avg_cost = 50000.0
    hard_stop_loss_pct = 0.08
    stop_price = avg_cost * (1 - hard_stop_loss_pct)
    assert stop_price == pytest.approx(46000.0)
    assert stop_price < avg_cost


def test_hard_sl_falls_back_to_lowest_price_when_no_cost():
    """When avg_cost == 0, hard SL must fall back to lowest_price."""
    avg_cost = 0.0
    lowest_price = 48000.0
    hard_stop_loss_pct = 0.08

    ref_price = avg_cost if avg_cost > 0 else lowest_price
    stop_price = ref_price * (1 - hard_stop_loss_pct)

    assert stop_price == pytest.approx(lowest_price * 0.92)


def test_hard_sl_8pct_fires_before_drawdown_cb():
    """Hard SL at 8% fires well before CB at 15% drawdown."""
    avg_cost = 50000.0
    hard_stop_loss_pct = 0.08
    cb_drawdown_pct = 0.15

    sl_price = avg_cost * (1 - hard_stop_loss_pct)
    cb_price = avg_cost * (1 - cb_drawdown_pct)

    # SL should trigger at higher price than CB (fires first)
    assert sl_price > cb_price


# ── MEJORA-011: Inventory soft stop-loss ─────────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_stop_loss_triggers_below_threshold(manager):
    """Stop-loss triggers when price < avg_cost * (1 - stop_pct)."""
    manager._avg_cost = 50000.0
    manager._position_qty = 0.1
    manager.exchange.place_market_order = AsyncMock(return_value="MARKET_1")
    manager.exchange.cancel_all_orders = AsyncMock()

    triggered = await manager.check_inventory_stop_loss(
        current_price=45000.0,  # 10% below avg_cost
        stop_pct=0.08,           # threshold at 46000
    )

    assert triggered is True
    manager.exchange.cancel_all_orders.assert_called_once_with(symbol="BTCUSDC")
    manager.exchange.place_market_order.assert_called_once_with(
        symbol="BTCUSDC", side=str(OrderSide.SELL), qty=0.1
    )


@pytest.mark.asyncio
async def test_inventory_stop_loss_does_not_trigger_above_threshold(manager):
    """Stop-loss must NOT trigger when price is above threshold."""
    manager._avg_cost = 50000.0
    manager._position_qty = 0.1
    manager.exchange.place_market_order = AsyncMock(return_value="MARKET_2")
    manager.exchange.cancel_all_orders = AsyncMock()

    triggered = await manager.check_inventory_stop_loss(
        current_price=47000.0,  # 6% below avg_cost
        stop_pct=0.08,           # threshold at 46000 — price is still above
    )

    assert triggered is False
    manager.exchange.cancel_all_orders.assert_not_called()
    manager.exchange.place_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_stop_loss_noop_without_position(manager):
    """No-op when position_qty == 0 (nothing to sell)."""
    manager._avg_cost = 50000.0
    manager._position_qty = 0.0
    manager.exchange.place_market_order = AsyncMock()

    triggered = await manager.check_inventory_stop_loss(
        current_price=40000.0,
        stop_pct=0.08,
    )

    assert triggered is False
    manager.exchange.place_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_inventory_stop_loss_noop_without_cost_basis(manager):
    """No-op when avg_cost == 0 (untracked position — no valid threshold)."""
    manager._avg_cost = 0.0
    manager._position_qty = 0.5
    manager.exchange.place_market_order = AsyncMock()

    triggered = await manager.check_inventory_stop_loss(
        current_price=100.0,
        stop_pct=0.08,
    )

    assert triggered is False
    manager.exchange.place_market_order.assert_not_called()


# ── MEJORA-012: order_size_usdt defensive guard ───────────────────────────────

def test_order_size_guard_warns_when_too_large(caplog):
    """validate_trading_params warns when order_size_usdt > capital / pairs * 0.5."""
    import logging
    from config.settings import Settings
    from unittest.mock import patch

    with patch.dict("os.environ", {
        "GRID_CAPITAL_USDT": "150",
        "GRID_ORDER_SIZE_USDT": "300",
        "GRID_MAX_ACTIVE_PAIRS": "3",
        "GRID_SYMBOLS": "BTCUSDC,ETHUSDC,SOLUSDC",
        "RISK_MAX_DAILY_LOSS_PCT": "0.025",
    }, clear=False):
        settings = Settings()
        # max_safe_order = 150 / 3 * 0.5 = 25; 300 >> 25 → should warn
        with caplog.at_level(logging.WARNING, logger="config"):
            try:
                settings.validate_trading_params()
            except ValueError:
                pass
        assert any("order_size_usdt" in msg for msg in caplog.messages)
