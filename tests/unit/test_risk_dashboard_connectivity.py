"""Unit tests for Phase K — Risk Dashboard Connectivity.

Covers:
  - RiskManager.get_risk_status() correctness and field types
  - _last_drawdown_pct / _last_daily_loss_pct tracking
  - _handle_risk_decision() circuit breaker event logging (via db.log_circuit_breaker)
  - State-transition guard: duplicate writes are prevented
  - Risk status DB persistence (db.set_runtime_config called on every cycle)
"""

from __future__ import annotations

import pytest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from core.risk_manager import RiskManager, RiskDecision
from data.models import CircuitBreakerEvent
from main import TradingBot


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_rm(
    *,
    max_drawdown_pct: float = 0.25,
    max_daily_loss_pct: float = 0.10,
    max_hourly_move_pct: float = 0.05,
    min_price_shock_samples: int = 2,
) -> RiskManager:
    return RiskManager(
        max_drawdown_pct=max_drawdown_pct,
        max_daily_loss_pct=max_daily_loss_pct,
        max_hourly_move_pct=max_hourly_move_pct,
        min_price_shock_samples=min_price_shock_samples,
    )


@pytest.fixture
def mock_bot():
    """Minimal stand-in for TradingBot._handle_risk_decision self-parameter."""
    rm = _make_rm()
    rm.evaluate(equity=1000.0)  # initialise peak equity so it's not None

    bot = MagicMock()
    bot.db.log_circuit_breaker = MagicMock()
    bot.db.set_runtime_config = MagicMock()
    bot.risk_manager = rm
    bot.settings.risk.price_shock_notify_telegram = False
    bot._price_shock_was_paused = False
    bot._log_and_notify = AsyncMock()
    bot.emergency_stop = AsyncMock()
    return bot


# ── get_risk_status() tests ───────────────────────────────────────────────────

class TestGetRiskStatus:
    def test_available_is_true(self):
        rm = _make_rm()
        rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["available"] is True

    def test_all_required_fields_present(self):
        rm = _make_rm()
        rm.evaluate(equity=1000.0)
        status = rm.get_risk_status()
        required = {
            "available", "drawdown_pct", "daily_loss_pct",
            "drawdown_limit_pct", "daily_loss_limit_pct",
            "price_move_1h", "price_move_limit_pct",
            "price_shock_paused", "warmup_complete", "is_paused",
        }
        assert required.issubset(set(status.keys()))

    def test_drawdown_pct_is_percentage_scale(self):
        """10% drawdown stored as 10.0, not 0.10."""
        rm = _make_rm()
        rm.evaluate(equity=1000.0)
        rm.evaluate(equity=900.0)  # 10% drawdown
        status = rm.get_risk_status()
        assert 9.5 <= status["drawdown_pct"] <= 10.5

    def test_daily_loss_pct_is_percentage_scale(self):
        """5% daily loss stored as 5.0, not 0.05."""
        rm = _make_rm()
        rm.evaluate(equity=1000.0)
        rm.evaluate(equity=950.0)  # 5% day loss
        status = rm.get_risk_status()
        assert 4.5 <= status["daily_loss_pct"] <= 5.5

    def test_thresholds_match_constructor(self):
        rm = RiskManager(
            max_drawdown_pct=0.20,
            max_daily_loss_pct=0.05,
            max_hourly_move_pct=0.08,
            min_price_shock_samples=2,
        )
        status = rm.get_risk_status()
        assert status["drawdown_limit_pct"] == pytest.approx(20.0)
        assert status["daily_loss_limit_pct"] == pytest.approx(5.0)
        assert status["price_move_limit_pct"] == pytest.approx(8.0)

    def test_drawdown_pct_updates_after_evaluate(self):
        rm = _make_rm()
        rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["drawdown_pct"] == pytest.approx(0.0)
        rm.evaluate(equity=800.0)  # 20% drawdown
        assert rm.get_risk_status()["drawdown_pct"] == pytest.approx(20.0)

    def test_price_shock_paused_reflects_true_during_pause(self):
        rm = _make_rm()
        now = datetime.now(UTC)
        rm.register_price(now=now - timedelta(seconds=60), price=50000.0)
        rm.register_price(now=now, price=53000.0)  # 6% spike, threshold 5%
        with patch("time.monotonic", return_value=0.0):
            rm.evaluate(equity=1000.0)
        with patch("time.monotonic", return_value=6.0):
            rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["price_shock_paused"] is True
        assert rm.get_risk_status()["is_paused"] is True

    def test_price_shock_paused_false_when_stable(self):
        rm = _make_rm()
        now = datetime.now(UTC)
        rm.register_price(now=now - timedelta(seconds=60), price=50000.0)
        rm.register_price(now=now, price=50000.0)
        with patch("time.monotonic", return_value=0.0):
            rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["price_shock_paused"] is False

    def test_warmup_complete_false_before_sufficient_samples(self):
        rm = _make_rm(min_price_shock_samples=5)
        # Only 1 sample registered — warmup not complete
        rm.register_price(now=datetime.now(UTC), price=50000.0)
        with patch("time.monotonic", return_value=0.0):
            rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["warmup_complete"] is False

    def test_warmup_complete_true_after_sufficient_samples(self):
        rm = _make_rm(min_price_shock_samples=2)
        now = datetime.now(UTC)
        rm.register_price(now=now - timedelta(seconds=60), price=50000.0)
        rm.register_price(now=now, price=50000.0)
        # Use 1000.0 so that 1000.0 - 0.0 (init) >= 5.0 triggers the lazy eval
        with patch("time.monotonic", return_value=1000.0):
            rm.evaluate(equity=1000.0)
        assert rm.get_risk_status()["warmup_complete"] is True

    def test_price_move_1h_is_percentage_scale(self):
        rm = _make_rm()
        now = datetime.now(UTC)
        rm.register_price(now=now - timedelta(seconds=60), price=50000.0)
        rm.register_price(now=now, price=53000.0)  # 6% move
        # Use 1000.0 so the lazy eval fires (1000.0 - 0.0 >= 5.0)
        with patch("time.monotonic", return_value=1000.0):
            rm.evaluate(equity=1000.0)
        # After warmup completes, _cached_price_shock_result = 0.06
        status = rm.get_risk_status()
        # price_move_1h should be ~6.0 (percentage)
        assert status["price_move_1h"] == pytest.approx(6.0, abs=0.1)


# ── _handle_risk_decision circuit breaker logging ─────────────────────────────

@pytest.mark.asyncio
async def test_risk_status_persisted_on_every_call(mock_bot):
    """set_runtime_config("risk_status", ...) is called on every evaluate cycle."""
    decision = RiskDecision(allow_trading=True, emergency_stop=False, reason="ok")
    await TradingBot._handle_risk_decision(mock_bot, decision)
    # Called: "risk_status", "positions", and "peak_equity"
    assert mock_bot.db.set_runtime_config.call_count >= 2
    first_call = mock_bot.db.set_runtime_config.call_args_list[0][0]
    assert first_call[0] == "risk_status"
    assert first_call[1]["available"] is True


@pytest.mark.asyncio
async def test_emergency_stop_logs_circuit_breaker_max_drawdown(mock_bot):
    decision = RiskDecision(
        allow_trading=False,
        emergency_stop=True,
        reason="max_drawdown_triggered:0.3000",
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_called_once()
    ev: CircuitBreakerEvent = mock_bot.db.log_circuit_breaker.call_args[0][0]
    assert ev.breaker_type == "max_drawdown"
    assert ev.action_taken == "emergency_stop"
    assert ev.trigger_value == pytest.approx(0.3)
    # emergency_stop() called
    mock_bot.emergency_stop.assert_called_once_with("max_drawdown_triggered:0.3000")


@pytest.mark.asyncio
async def test_emergency_stop_logs_circuit_breaker_price_shock_escalation(mock_bot):
    decision = RiskDecision(
        allow_trading=False,
        emergency_stop=True,
        reason="price_shock_sustained:0.0650:7300s",
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_called_once()
    ev: CircuitBreakerEvent = mock_bot.db.log_circuit_breaker.call_args[0][0]
    assert ev.breaker_type == "price_shock_escalation"
    assert ev.action_taken == "emergency_stop"


@pytest.mark.asyncio
async def test_daily_loss_logs_circuit_breaker_on_first_trigger(mock_bot):
    decision = RiskDecision(
        allow_trading=False,
        emergency_stop=False,
        reason="max_daily_loss_triggered:0.1050",
        paused_until=datetime.now(UTC),
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_called_once()
    ev: CircuitBreakerEvent = mock_bot.db.log_circuit_breaker.call_args[0][0]
    assert ev.breaker_type == "daily_loss"
    assert ev.action_taken == "pause_24h"
    assert ev.trigger_value == pytest.approx(0.105)


@pytest.mark.asyncio
async def test_daily_loss_no_circuit_breaker_log_during_ongoing_pause(mock_bot):
    """On subsequent cycles (reason='daily_loss_pause'), no duplicate DB write."""
    decision = RiskDecision(
        allow_trading=False,
        emergency_stop=False,
        reason="daily_loss_pause",
        paused_until=datetime.now(UTC),
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_not_called()


@pytest.mark.asyncio
async def test_price_shock_pause_logs_circuit_breaker_on_activation(mock_bot):
    mock_bot._price_shock_was_paused = False
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="price_shock_pause_active:0.0600",
        block_new_grids=True,
        price_shock_paused=True,
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_called_once()
    ev: CircuitBreakerEvent = mock_bot.db.log_circuit_breaker.call_args[0][0]
    assert ev.breaker_type == "price_shock_pause"
    assert ev.action_taken == "block_new_grids"
    assert ev.trigger_value == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_price_shock_pause_no_duplicate_log_when_already_active(mock_bot):
    """State guard: no DB write when pause was already active last cycle."""
    mock_bot._price_shock_was_paused = True
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="price_shock_pause_active:0.0600",
        block_new_grids=True,
        price_shock_paused=True,
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_not_called()


@pytest.mark.asyncio
async def test_price_shock_resume_logs_circuit_breaker(mock_bot):
    mock_bot._price_shock_was_paused = True
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="ok",
        block_new_grids=False,
        price_shock_paused=False,
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_called_once()
    ev: CircuitBreakerEvent = mock_bot.db.log_circuit_breaker.call_args[0][0]
    assert ev.breaker_type == "price_shock_resume"
    assert ev.action_taken == "resume_grids"


@pytest.mark.asyncio
async def test_price_shock_resume_no_log_when_was_not_paused(mock_bot):
    """No resume event logged when there was no active pause."""
    mock_bot._price_shock_was_paused = False
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="ok",
        block_new_grids=False,
        price_shock_paused=False,
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_not_called()


@pytest.mark.asyncio
async def test_price_shock_was_paused_updated_after_call(mock_bot):
    """Verify _price_shock_was_paused tracks the decision's price_shock_paused."""
    mock_bot._price_shock_was_paused = False
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="price_shock_pause_active:0.0600",
        block_new_grids=True,
        price_shock_paused=True,
    )
    await TradingBot._handle_risk_decision(mock_bot, decision)
    assert mock_bot._price_shock_was_paused is True


@pytest.mark.asyncio
async def test_no_log_on_normal_ok_cycle(mock_bot):
    """Normal 'ok' cycle: no circuit breaker event, but risk status IS persisted."""
    decision = RiskDecision(allow_trading=True, emergency_stop=False, reason="ok")
    await TradingBot._handle_risk_decision(mock_bot, decision)
    mock_bot.db.log_circuit_breaker.assert_not_called()
    # set_runtime_config called: "risk_status" + "positions" + "peak_equity"
    assert mock_bot.db.set_runtime_config.call_count >= 2


@pytest.mark.asyncio
async def test_db_error_in_circuit_breaker_does_not_raise(mock_bot):
    """DB failure for CB logging is caught and swallowed — bot continues."""
    mock_bot.db.log_circuit_breaker.side_effect = RuntimeError("DB locked")
    decision = RiskDecision(
        allow_trading=True,
        emergency_stop=False,
        reason="price_shock_pause_active:0.0600",
        block_new_grids=True,
        price_shock_paused=True,
    )
    # Should not raise
    await TradingBot._handle_risk_decision(mock_bot, decision)


@pytest.mark.asyncio
async def test_db_error_in_risk_status_persist_does_not_raise(mock_bot):
    """DB failure in set_runtime_config is caught and swallowed."""
    mock_bot.db.set_runtime_config.side_effect = RuntimeError("DB error")
    decision = RiskDecision(allow_trading=True, emergency_stop=False, reason="ok")
    await TradingBot._handle_risk_decision(mock_bot, decision)  # Must not raise
