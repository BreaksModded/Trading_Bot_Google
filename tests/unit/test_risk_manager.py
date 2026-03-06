"""Unit tests for the RiskManager class and Phase F lazy evaluation logic."""

import pytest
import time
from unittest.mock import patch, MagicMock
from core.risk_manager import RiskManager
from datetime import UTC, datetime, timedelta

@pytest.fixture
def risk_manager():
    # min_price_shock_samples=2 so existing tests with 2 registered prices
    # still trigger price shock evaluation immediately (no warmup delay).
    return RiskManager(
        max_drawdown_pct=0.25,
        max_daily_loss_pct=0.10,
        max_hourly_move_pct=0.05,
        min_price_shock_samples=2,
    )

# ==========================================
# CORE RISK CONTROLS
# ==========================================

def test_max_drawdown_triggers_emergency_stop(risk_manager):
    """Set initial equity to 1000, call evaluate with equity=700 (30% drawdown). Assert emergency_stop=True."""
    # Peak equity becomes 1000 on first call
    res_1 = risk_manager.evaluate(equity=1000.0)
    assert not res_1.emergency_stop
    
    # Drop to 700 -> 30% drawdown (threshold 25%)
    res_2 = risk_manager.evaluate(equity=700.0)
    assert res_2.emergency_stop is True
    assert "drawdown" in res_2.reason.lower()

def test_max_drawdown_does_not_trigger_below_threshold(risk_manager):
    """Set initial equity to 1000, evaluate with equity=780 (22% drawdown). Assert emergency_stop=False."""
    risk_manager.evaluate(equity=1000.0)
    
    # Drop to 780 -> 22% drawdown (below 25%)
    res_2 = risk_manager.evaluate(equity=780.0)
    assert res_2.emergency_stop is False

def test_emergency_stop_respects_zero_equity_edge_case(risk_manager):
    """Simulate get_portfolio_equity returning 0 (sometimes passed inadvertently).
    We assert RiskDecision returns True for drawdown (100% drawdown) as it operates on raw math,
    leaving the validation to caller. This formalizes RiskManager behavior."""
    risk_manager.evaluate(equity=1000.0)
    
    res = risk_manager.evaluate(equity=0.0)
    assert res.emergency_stop is True

def test_daily_loss_triggers_emergency_stop(risk_manager):
    """Daily loss over limit halts trading."""
    risk_manager.evaluate(equity=1000.0) # >10% of 1000
    res = risk_manager.evaluate(equity=890.0)
    assert res.allow_trading is False # The daily loss triggers pause
    # Let's say it triggers pause, if the logic triggers emergency_stop we'll correct later
    
# ==========================================
# PHASE F — LAZY EVALUATION (PRICE SHOCK)
# ==========================================

def test_price_shock_check_runs_on_first_call(risk_manager):
    """First evaluate() call must run price_move_one_hour regardless of time."""
    # Register min_price_shock_samples=2 prices so the warmup guard passes
    now = datetime.now(UTC)
    risk_manager.register_price(now=now - timedelta(seconds=60), price=50000.0)
    risk_manager.register_price(now=now, price=50000.0)

    with patch.object(risk_manager, 'price_move_one_hour', return_value=0.0) as mock_price_move:
        risk_manager.evaluate(equity=1000.0)
        mock_price_move.assert_called_once()

def test_price_shock_check_is_cached_within_interval(risk_manager):
    """Call evaluate() twice within 5 seconds. Assert price_move_one_hour runs ONCE."""
    now = datetime.now(UTC)
    risk_manager.register_price(now=now - timedelta(seconds=60), price=50000.0)
    risk_manager.register_price(now=now, price=50000.0)

    with patch.object(risk_manager, 'price_move_one_hour', return_value=0.0) as mock_price_move:
        with patch('time.monotonic', return_value=1000.0):
            risk_manager.evaluate(equity=1000.0)

        # Advance slightly (2 seconds later)
        with patch('time.monotonic', return_value=1002.0):
            risk_manager.evaluate(equity=1000.0)

        # Due to 5-second lazy eval logic, price_move_one_hour should only have been called ONCE.
        mock_price_move.assert_called_once()


def test_price_shock_check_re_evaluates_after_interval(risk_manager):
    """Mock time.monotonic() to advance by 6 seconds. Assert runs on both calls."""
    now = datetime.now(UTC)
    risk_manager.register_price(now=now - timedelta(seconds=60), price=50000.0)
    risk_manager.register_price(now=now, price=50000.0)

    with patch.object(risk_manager, 'price_move_one_hour', return_value=0.0) as mock_price_move:
        with patch('time.monotonic', return_value=1000.0):
            risk_manager.evaluate(equity=1000.0)

        # Advance by 6 seconds, > 5.0s internal cache
        with patch('time.monotonic', return_value=1006.0):
            risk_manager.evaluate(equity=1000.0)

        assert mock_price_move.call_count == 2


def test_price_shock_detected_within_one_interval(risk_manager):
    """Inject a price shock. Assert caught within 5 seconds as a PAUSE (not emergency stop)."""
    # First call at t=0, price 50000
    with patch('time.monotonic', return_value=0.0):
        risk_manager.register_price(now=datetime.now(UTC), price=50000.0)
        risk_manager.evaluate(equity=1000.0)

    # Shock happens at t=2 (price jumps 6% to 53000, threshold is 5%)
    # Lazy eval cache interval hasn't expired yet — cached result is still 0.0
    with patch('time.monotonic', return_value=2.0):
        risk_manager.register_price(now=datetime.now(UTC), price=53000.0)
        safe_res = risk_manager.evaluate(equity=1000.0)
        assert safe_res.allow_trading is True

    # After 5 seconds expire (at t=6.0), the shock is detected.
    # Phase J: price shock triggers a PAUSE (block_new_grids), NOT emergency_stop.
    with patch('time.monotonic', return_value=6.0):
        shock_res = risk_manager.evaluate(equity=1000.0)
        assert shock_res.emergency_stop is False
        assert shock_res.block_new_grids is True
        assert shock_res.price_shock_paused is True
        assert shock_res.allow_trading is True
        assert "price_shock" in shock_res.reason.lower()


def test_cached_result_is_per_instance_not_class_variable():
    """Create two RiskManagers. Trigger a shock on one. Assert other is unaffected."""
    rm_1 = RiskManager(
        max_drawdown_pct=0.25, max_daily_loss_pct=0.10,
        max_hourly_move_pct=0.05, min_price_shock_samples=2,
    )
    rm_2 = RiskManager(
        max_drawdown_pct=0.25, max_daily_loss_pct=0.10,
        max_hourly_move_pct=0.05, min_price_shock_samples=2,
    )

    with patch('time.monotonic', return_value=0.0):
        rm_1.register_price(now=datetime.now(UTC), price=50000)
        rm_2.register_price(now=datetime.now(UTC), price=50000)
        rm_1.evaluate(equity=1000)
        rm_2.evaluate(equity=1000)

    with patch('time.monotonic', return_value=6.0):
        rm_1.register_price(now=datetime.now(UTC), price=53000)
        res_1 = rm_1.evaluate(equity=1000)  # 6% shock — Phase J: pause, not emergency
        res_2 = rm_2.evaluate(equity=1000)  # Flat — no pause

        assert res_1.block_new_grids is True
        assert res_1.emergency_stop is False
        assert res_2.emergency_stop is False
        assert res_2.block_new_grids is False
