"""Tests for the dynamic daily loss circuit breaker."""

import pytest
from datetime import UTC, datetime, timedelta

from core.risk_manager import RiskManager


@pytest.fixture
def manager():
    """Create a RiskManager with standard settings."""
    return RiskManager(
        max_drawdown_pct=0.15,
        max_daily_loss_pct=0.05,
        max_hourly_move_pct=0.08,
        daily_loss_pause_hours=1.0,  # 1 hour minimum
    )


class TestDynamicDailyLossCB:
    """Validate equity-recovery based pause clearing."""

    def test_daily_loss_cb_clears_when_equity_recovers(self, manager):
        """Trigger CB -> >1h passes -> equity rises -> pause clears."""
        t0 = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
        
        # Initial equity: 1000
        manager.evaluate(equity=1000.0, now=t0)
        
        # Drop to 900 (10% drop, triggers 5% CB)
        t1 = t0 + timedelta(minutes=5)
        decision1 = manager.evaluate(equity=900.0, now=t1)
        
        assert decision1.allow_trading is False
        assert "max_daily_loss_triggered:0.10" in decision1.reason
        assert manager.paused_until == t1 + timedelta(hours=1.0)
        assert manager._loss_trigger_equity == 900.0
        
        # Fast forward >1 hour, equity recovers to 910
        t2 = t1 + timedelta(minutes=65)
        decision2 = manager.evaluate(equity=910.0, now=t2)
        
        # Should clear the CB and allow trading again
        assert decision2.allow_trading is True
        assert manager.paused_until is None
        assert manager._loss_trigger_equity is None

    def test_daily_loss_cb_stays_paused_before_minimum_hours(self, manager):
        """Even if equity recovers, keeps pause until minimum hours pass."""
        t0 = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
        manager.evaluate(equity=1000.0, now=t0)
        
        # Trigger CB at 900
        t1 = t0 + timedelta(minutes=5)
        manager.evaluate(equity=900.0, now=t1)
        
        # Fast forward 30 minutes (less than 1 hour), equity recovers to 910
        t2 = t1 + timedelta(minutes=30)
        decision2 = manager.evaluate(equity=910.0, now=t2)
        
        # Still paused!
        assert decision2.allow_trading is False
        assert decision2.reason == "daily_loss_pause"
        assert manager.paused_until is not None

    def test_daily_loss_cb_stays_paused_if_equity_not_recovered(self, manager):
        """Even after minimum hours, keeps pause if equity hasn't recovered."""
        t0 = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
        manager.evaluate(equity=1000.0, now=t0)
        
        # Trigger CB at 900
        t1 = t0 + timedelta(minutes=5)
        manager.evaluate(equity=900.0, now=t1)
        
        # Fast forward 2 hours, equity drops further to 890
        t2 = t1 + timedelta(hours=2)
        decision2 = manager.evaluate(equity=890.0, now=t2)
        
        # Still paused!
        assert decision2.allow_trading is False
        assert decision2.reason == "daily_loss_pause"
        assert manager.paused_until is not None

    def test_daily_loss_cb_24h_fallback(self, manager):
        """If 24h pass and equity hasn't recovered, force clear the pause."""
        t0 = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
        manager.evaluate(equity=1000.0, now=t0)
        
        # Trigger CB at 900
        t1 = t0 + timedelta(minutes=5)
        manager.evaluate(equity=900.0, now=t1)
        
        # Fast forward 24h + 1m, equity is still 890
        t2 = t1 + timedelta(hours=24, minutes=1)
        decision2 = manager.evaluate(equity=890.0, now=t2)
        
        # Fallback clears the pause!
        # (day_loss remains 0.11 against a new day_start_equity if rolled, but the block is lifted)
        assert decision2.allow_trading is True
        assert manager.paused_until is None

    def test_daily_loss_pause_hours_configurable(self):
        """Verify the configuration parameter alters the minimum wait."""
        mgr = RiskManager(
            max_drawdown_pct=0.15,
            max_daily_loss_pct=0.05,
            max_hourly_move_pct=0.08,
            daily_loss_pause_hours=2.0,  # 2 hours minimum
        )
        
        t0 = datetime(2026, 3, 6, 10, 0, tzinfo=UTC)
        mgr.evaluate(equity=1000.0, now=t0)
        
        # Trigger CB at 900
        t1 = t0 + timedelta(minutes=5)
        mgr.evaluate(equity=900.0, now=t1)
        
        # 1.5 hours later, equity recovers to 910
        t2 = t1 + timedelta(hours=1, minutes=30)
        decision2 = mgr.evaluate(equity=910.0, now=t2)
        
        # Still paused (need 2h)
        assert decision2.allow_trading is False
        
        # 2.5 hours later, equity recovers to 910
        t3 = t1 + timedelta(hours=2, minutes=30)
        decision3 = mgr.evaluate(equity=910.0, now=t3)
        
        # Wait passed, unpaused
        assert decision3.allow_trading is True
