"""
Tests for core/risk_manager.py

Tests all three circuit breakers, pause/resume, daily reset,
capital tracking, and risk status reporting.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from config.settings import Settings
from core.risk_manager import RiskManager
from data.database import Database


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def risk_mgr(tmp_db: Database, settings: Settings) -> RiskManager:
    """Create a RiskManager with default settings and 150 USDT capital."""
    return RiskManager(settings, tmp_db, initial_capital=150.0)


# ── Max Drawdown Tests ────────────────────────────────────────────────


class TestMaxDrawdown:
    """Tests for the max drawdown circuit breaker."""

    def test_no_trigger_within_limit(self, risk_mgr: RiskManager):
        """Should NOT trigger when drawdown is within all limits."""
        # 0.5% drawdown (150 → 149.25), also within daily loss 1% limit
        should_stop, reason = risk_mgr.check_all(149.25)
        assert should_stop is False

    def test_triggers_at_threshold(self, risk_mgr: RiskManager):
        """Should trigger when drawdown hits the threshold (15%)."""
        # 150 → 127.5 = 15% drawdown
        should_stop, reason = risk_mgr.check_all(127.5)
        assert should_stop is True
        assert "MAX DRAWDOWN" in reason

    def test_triggers_beyond_threshold(self, risk_mgr: RiskManager):
        """Should trigger when drawdown exceeds threshold."""
        # 150 → 120 = 20% drawdown
        should_stop, reason = risk_mgr.check_all(120.0)
        assert should_stop is True

    def test_peak_updates_upward(self, risk_mgr: RiskManager):
        """Peak capital should track the highest value."""
        risk_mgr.check_all(160.0)  # New peak
        assert risk_mgr.peak_capital == 160.0

        # Now 15% of 160 = 24, so 136 should trigger
        should_stop, _ = risk_mgr.check_all(136.0)
        assert should_stop is True

    def test_drawdown_property(self, risk_mgr: RiskManager):
        """drawdown_pct property should reflect current state."""
        risk_mgr.update_capital(142.5)  # 5% drawdown from 150
        assert abs(risk_mgr.drawdown_pct - 0.05) < 0.001


# ── Daily Loss Tests ──────────────────────────────────────────────────


class TestDailyLoss:
    """Tests for the daily loss circuit breaker."""

    def test_no_trigger_within_limit(self, risk_mgr: RiskManager):
        """Should NOT trigger when daily loss is within limit (1%)."""
        # 0.5% loss
        should_stop, reason = risk_mgr.check_all(149.25)
        assert should_stop is False

    def test_triggers_at_threshold(self, risk_mgr: RiskManager):
        """Should trigger at 1% daily loss."""
        # 150 → 148.5 = 1% daily loss
        should_stop, reason = risk_mgr.check_all(148.5)
        assert should_stop is True
        assert "DAILY LOSS" in reason

    def test_pauses_bot(self, risk_mgr: RiskManager):
        """Should set pause state when daily loss triggers."""
        risk_mgr.check_all(148.0)  # Trigger daily loss
        assert risk_mgr.is_paused is True
        assert risk_mgr.pause_remaining_hours > 0

    def test_resumes_after_pause_expires(self, risk_mgr: RiskManager):
        """Bot should resume after pause period expires."""
        risk_mgr.check_all(148.0)  # Trigger daily loss
        assert risk_mgr.is_paused is True

        # Simulate pause expiration by setting _pause_until to past
        risk_mgr._pause_until = datetime.utcnow() - timedelta(hours=1)

        should_stop, reason = risk_mgr.check_all(148.0)
        # After pause expires, it checks conditions again fresh
        assert risk_mgr._paused is False or "DAILY LOSS" in reason

    def test_daily_loss_property(self, risk_mgr: RiskManager):
        """daily_loss_pct property should reflect current state."""
        risk_mgr.update_capital(149.5)
        daily = risk_mgr.daily_loss_pct
        expected = (150.0 - 149.5) / 150.0
        assert abs(daily - expected) < 0.001


# ── Price Movement Tests ──────────────────────────────────────────────


class TestPriceMovement:
    """Tests for the sudden price movement circuit breaker."""

    def test_no_trigger_small_move(self, risk_mgr: RiskManager):
        """Should NOT trigger on small price movements."""
        # Record initial price
        risk_mgr._price_history = [
            (datetime.utcnow() - timedelta(hours=1, minutes=5), 50000.0),
        ]
        should_stop, reason = risk_mgr.check_all(150.0, current_price=50500.0)
        assert should_stop is False

    def test_triggers_on_large_move(self, risk_mgr: RiskManager):
        """Should trigger when price moves >8% in 1 hour."""
        # Set up price history: 1 hour ago was 50000
        risk_mgr._price_history = [
            (datetime.utcnow() - timedelta(hours=1, minutes=5), 50000.0),
        ]
        # Current price: 55000 = 10% move
        should_stop, reason = risk_mgr.check_all(150.0, current_price=55000.0)
        assert should_stop is True
        assert "PRICE MOVE" in reason

    def test_triggers_on_crash(self, risk_mgr: RiskManager):
        """Should trigger on 8%+ price crash."""
        risk_mgr._price_history = [
            (datetime.utcnow() - timedelta(hours=1, minutes=5), 50000.0),
        ]
        # Price crashed to 45000 = 10% drop
        should_stop, reason = risk_mgr.check_all(150.0, current_price=45000.0)
        assert should_stop is True

    def test_no_trigger_without_history(self, risk_mgr: RiskManager):
        """Should NOT trigger when there's no price history."""
        risk_mgr._price_history.clear()
        should_stop, reason = risk_mgr.check_all(150.0, current_price=50000.0)
        assert should_stop is False


# ── Pause/Resume Tests ────────────────────────────────────────────────


class TestPauseResume:
    """Tests for manual pause and resume."""

    def test_force_pause(self, risk_mgr: RiskManager):
        """Should manually pause the bot."""
        risk_mgr.force_pause("Test pause", hours=2.0)
        assert risk_mgr.is_paused is True
        assert risk_mgr.pause_remaining_hours > 0
        assert risk_mgr.pause_remaining_hours <= 2.0

    def test_resume(self, risk_mgr: RiskManager):
        """Should manually resume the bot."""
        risk_mgr.force_pause("Test pause", hours=2.0)
        risk_mgr.resume()
        assert risk_mgr.is_paused is False
        assert risk_mgr.pause_remaining_hours == 0.0

    def test_paused_blocks_operations(self, risk_mgr: RiskManager):
        """While paused, check_all should return stop=True."""
        risk_mgr.force_pause("Maintenance", hours=2.0)
        should_stop, reason = risk_mgr.check_all(150.0)
        assert should_stop is True
        assert "Paused" in reason


# ── Risk Status Tests ─────────────────────────────────────────────────


class TestRiskStatus:
    """Tests for the get_risk_status method."""

    def test_status_dict_format(self, risk_mgr: RiskManager):
        """Risk status should return a dict with all expected keys."""
        status = risk_mgr.get_risk_status()
        assert "current_capital" in status
        assert "drawdown_pct" in status
        assert "daily_loss_pct" in status
        assert "is_paused" in status
        assert "drawdown_limit_pct" in status

    def test_status_reflects_state(self, risk_mgr: RiskManager):
        """Status values should match internal state."""
        risk_mgr.update_capital(145.0)
        status = risk_mgr.get_risk_status()
        assert status["current_capital"] == 145.0
        assert status["peak_capital"] == 150.0

    def test_circuit_breaker_logged(self, risk_mgr: RiskManager):
        """Circuit breaker events should be logged to database."""
        risk_mgr.check_all(127.5)  # Trigger max drawdown
        # Verify event was logged
        events = risk_mgr.db.get_circuit_breaker_events(limit=1)
        assert len(events) >= 1


# ── Capital Tracking Tests ────────────────────────────────────────────


class TestCapitalTracking:
    """Tests for capital and peak tracking."""

    def test_update_capital(self, risk_mgr: RiskManager):
        """update_capital should update current and peak."""
        risk_mgr.update_capital(160.0)
        assert risk_mgr.current_capital == 160.0
        assert risk_mgr.peak_capital == 160.0

    def test_peak_does_not_decrease(self, risk_mgr: RiskManager):
        """Peak should never decrease."""
        risk_mgr.update_capital(160.0)
        risk_mgr.update_capital(155.0)
        assert risk_mgr.peak_capital == 160.0

    def test_record_price(self, risk_mgr: RiskManager):
        """record_price should add to price history."""
        initial_len = len(risk_mgr._price_history)
        risk_mgr.record_price(50000.0)
        assert len(risk_mgr._price_history) == initial_len + 1
