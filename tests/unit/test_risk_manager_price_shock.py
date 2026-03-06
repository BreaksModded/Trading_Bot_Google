"""
Phase J — Price Shock Circuit Breaker: cold-start guard + intelligent auto-recovery.

Test coverage:
  - Cold-start minimum samples guard
  - Three-stage pause / monitoring / auto-resume lifecycle
  - Escalation to emergency stop after sustained volatility
  - State isolation between independent pause episodes
  - Interaction with drawdown and daily-loss circuit breakers during warmup
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from core.risk_manager import RiskManager


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_rm(
    *,
    min_samples: int = 5,
    resume_cycles: int = 3,
    max_pause_secs: int = 600,
    hourly_move_pct: float = 0.08,
) -> RiskManager:
    """Create a RiskManager with Phase J defaults tuned for fast tests."""
    return RiskManager(
        max_drawdown_pct=0.25,
        max_daily_loss_pct=0.10,
        max_hourly_move_pct=hourly_move_pct,
        min_price_shock_samples=min_samples,
        price_shock_resume_cycles=resume_cycles,
        price_shock_max_pause_secs=max_pause_secs,
    )


def _register_prices(
    rm: RiskManager,
    *,
    count: int,
    start_price: float = 50_000.0,
    end_price: float | None = None,
) -> None:
    """Register `count` evenly-spaced prices spanning a 30-minute window."""
    now = datetime.now(UTC)
    end = end_price if end_price is not None else start_price
    for i in range(count):
        t = now - timedelta(minutes=30) + timedelta(minutes=30 * i / max(count - 1, 1))
        price = start_price + (end - start_price) * i / max(count - 1, 1)
        rm.register_price(now=t, price=price)


def _prime_equity(rm: RiskManager, equity: float = 1_000.0) -> None:
    """Call evaluate once to initialise peak_equity / day_start_equity."""
    with patch("time.monotonic", return_value=0.0):
        rm.evaluate(equity=equity)


# ── Cold-start guard ──────────────────────────────────────────────────────────

def test_price_shock_skipped_below_minimum_samples():
    """Price shock must not trigger when sample count < min_samples."""
    rm = _make_rm(min_samples=10, hourly_move_pct=0.05)
    _prime_equity(rm)

    # Register only 4 samples spanning a 20 % move (well above threshold)
    _register_prices(rm, count=4, start_price=50_000.0, end_price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=1_000.0)

    assert decision.price_shock_paused is False
    assert decision.block_new_grids is False
    assert decision.emergency_stop is False
    assert decision.allow_trading is True


def test_price_shock_skipped_does_not_produce_warning_reason():
    """During warmup, reason must be 'ok' (no price_shock text)."""
    rm = _make_rm(min_samples=10)
    _prime_equity(rm)
    _register_prices(rm, count=3, start_price=50_000.0, end_price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=1_000.0)

    assert "price_shock" not in decision.reason


def test_price_shock_activates_exactly_at_minimum_samples():
    """Price shock must trigger on the cycle where sample count first reaches min_samples."""
    rm = _make_rm(min_samples=5, hourly_move_pct=0.05)
    _prime_equity(rm)

    # Register exactly min_samples prices with a 20 % move
    _register_prices(rm, count=5, start_price=50_000.0, end_price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=1_000.0)

    assert decision.block_new_grids is True
    assert decision.price_shock_paused is True
    assert decision.emergency_stop is False


def test_warmup_info_logged_exactly_once():
    """The 'warmup mode' INFO message must appear exactly once, not on every cycle."""
    rm = _make_rm(min_samples=5)
    _prime_equity(rm)
    _register_prices(rm, count=2, start_price=50_000.0, end_price=60_000.0)

    # Advance monotonic by 5s each call to trigger lazy eval every time
    with patch("core.risk_manager.logger.info") as mock_info:
        for t in [10.0, 15.0, 20.0]:
            with patch("time.monotonic", return_value=t):
                rm.evaluate(equity=1_000.0)

    warmup_calls = [
        c for c in mock_info.call_args_list
        if "warmup mode" in str(c).lower()
    ]
    assert len(warmup_calls) == 1


def test_warmup_completion_info_logged_exactly_once():
    """The 'circuit breaker now active' INFO must appear exactly once."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.05)
    _prime_equity(rm)

    # First call: only 2 samples — still in warmup
    _register_prices(rm, count=2, start_price=50_000.0, end_price=50_000.0)

    with patch("core.risk_manager.logger.info") as mock_info:
        with patch("time.monotonic", return_value=10.0):
            rm.evaluate(equity=1_000.0)

        # Add one more sample to cross the threshold (3 total)
        rm.register_price(now=datetime.now(UTC), price=50_100.0)

        with patch("time.monotonic", return_value=16.0):
            rm.evaluate(equity=1_000.0)
        with patch("time.monotonic", return_value=22.0):
            rm.evaluate(equity=1_000.0)

    activation_calls = [
        c for c in mock_info.call_args_list
        if "now active" in str(c).lower()
    ]
    assert len(activation_calls) == 1


def test_drawdown_circuit_breaker_unaffected_during_warmup():
    """Max drawdown must still fire even when price shock warmup is active."""
    rm = _make_rm(min_samples=20)
    # Only 2 samples — well below min_samples=20
    _register_prices(rm, count=2, start_price=50_000.0, end_price=50_000.0)

    with patch("time.monotonic", return_value=0.0):
        rm.evaluate(equity=1_000.0)  # sets peak = 1000

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=700.0)  # 30 % drawdown > 25 % threshold

    assert decision.emergency_stop is True
    assert "drawdown" in decision.reason.lower()


def test_daily_loss_circuit_breaker_unaffected_during_warmup():
    """Daily loss must still block trading even when price shock warmup is active."""
    rm = _make_rm(min_samples=20)
    _register_prices(rm, count=2)

    with patch("time.monotonic", return_value=0.0):
        rm.evaluate(equity=1_000.0)

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=880.0)  # 12 % daily loss > 10 % threshold

    assert decision.allow_trading is False
    assert "daily_loss" in decision.reason.lower()
    assert decision.emergency_stop is False


# ── Auto-recovery pause tests ─────────────────────────────────────────────────

def test_price_shock_triggers_pause_not_emergency_stop():
    """A price shock above threshold must produce a pause, not an emergency stop."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08)
    _prime_equity(rm)
    _register_prices(rm, count=5, start_price=50_000.0, end_price=60_000.0)  # 20 % move

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=1_000.0)

    assert decision.emergency_stop is False
    assert decision.block_new_grids is True
    assert decision.price_shock_paused is True
    assert decision.allow_trading is True
    assert "price_shock_pause" in decision.reason


def test_pause_keeps_allow_trading_true():
    """allow_trading must be True during a price shock pause so sync/fills continue."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.05)
    _prime_equity(rm)
    _register_prices(rm, count=5, start_price=50_000.0, end_price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        decision = rm.evaluate(equity=1_000.0)

    assert decision.allow_trading is True


def test_clean_cycles_counter_increments_correctly():
    """
    After price normalises, each lazy-eval cycle must increment
    the clean-cycles counter (we verify via the decision fields
    rather than private state).
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=3)
    _prime_equity(rm)

    now = datetime.now(UTC)

    # Register shock prices
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)  # 20 % shock

    # t=10: lazy eval fires → pause starts
    with patch("time.monotonic", return_value=10.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # Register normalised prices (move back to ~0 %)
    rm.register_price(now=now + timedelta(minutes=1), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=2), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=3), price=50_500.0)

    # t=16: lazy eval fires — 1 clean cycle, still paused
    with patch("time.monotonic", return_value=16.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True  # not yet resumed

    # t=22: 2nd clean cycle — still paused
    with patch("time.monotonic", return_value=22.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # t=28: 3rd clean cycle — auto-resume triggers
    with patch("time.monotonic", return_value=28.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is False
    assert d.block_new_grids is False
    assert d.allow_trading is True


def test_auto_resume_fires_after_consecutive_clean_cycles():
    """Trading must fully resume after N consecutive clean evaluation cycles."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=2)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)  # shock detected

    # Register flat prices
    rm.register_price(now=now + timedelta(minutes=1), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=2), price=50_500.0)

    with patch("time.monotonic", return_value=16.0):
        rm.evaluate(equity=1_000.0)  # clean cycle 1

    with patch("time.monotonic", return_value=22.0):
        d = rm.evaluate(equity=1_000.0)  # clean cycle 2 → auto-resume

    assert d.price_shock_paused is False
    assert d.block_new_grids is False
    assert d.emergency_stop is False


def test_clean_cycles_reset_on_new_spike_during_pause():
    """If price spikes again while clean cycles are accumulating, counter resets to 0."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=3)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)  # shock

    # One clean cycle
    rm.register_price(now=now + timedelta(minutes=1), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=2), price=50_500.0)
    with patch("time.monotonic", return_value=16.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True  # 1/3 clean — not yet resumed

    # Spike again — should reset clean_cycles
    rm.register_price(now=now + timedelta(minutes=3), price=60_000.0)
    with patch("time.monotonic", return_value=22.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # Now resume from scratch: need 3 more clean cycles
    rm.register_price(now=now + timedelta(minutes=4), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=5), price=50_500.0)
    with patch("time.monotonic", return_value=28.0):
        d = rm.evaluate(equity=1_000.0)  # clean 1
    with patch("time.monotonic", return_value=34.0):
        d = rm.evaluate(equity=1_000.0)  # clean 2
    assert d.price_shock_paused is True  # still 2/3

    with patch("time.monotonic", return_value=40.0):
        d = rm.evaluate(equity=1_000.0)  # clean 3 → auto-resume
    assert d.price_shock_paused is False


def test_pause_timer_resets_on_new_episode():
    """
    After a complete pause→resume cycle, a new spike must start a fresh
    escalation timer (not inherit the previous episode's start time).
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=2, max_pause_secs=300)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    # Episode 1: spike at t=10, normalise, resume at t=22
    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)

    rm.register_price(now=now + timedelta(minutes=1), price=50_500.0)
    rm.register_price(now=now + timedelta(minutes=2), price=50_500.0)
    with patch("time.monotonic", return_value=16.0):
        rm.evaluate(equity=1_000.0)
    with patch("time.monotonic", return_value=22.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is False  # resumed

    # Episode 2: new spike at t=200 — escalation clock must start from t=200,
    # not t=10. So at t=490 (290 s after t=200 < 300 s limit) must NOT escalate.
    rm.register_price(now=now + timedelta(minutes=3), price=60_000.0)
    rm.register_price(now=now + timedelta(minutes=4), price=60_000.0)
    with patch("time.monotonic", return_value=200.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    with patch("time.monotonic", return_value=490.0):
        d = rm.evaluate(equity=1_000.0)  # 490-200=290 < 300 → not escalated
    assert d.emergency_stop is False
    assert d.price_shock_paused is True


def test_escalation_to_emergency_stop_after_max_duration():
    """Pause that exceeds max_pause_secs must escalate to emergency stop."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, max_pause_secs=100, resume_cycles=3)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    # t=10: pause starts; pause_start = 10
    with patch("time.monotonic", return_value=10.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # t=115: pause_duration = 115 - 10 = 105 > 100 → escalate
    with patch("time.monotonic", return_value=115.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.emergency_stop is True
    assert "price_shock_sustained" in d.reason


def test_escalation_does_not_fire_before_max_duration():
    """Pause below max_pause_secs must NOT escalate."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, max_pause_secs=100, resume_cycles=3)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)

    # t=90: pause_duration = 80 < 100 → no escalation
    with patch("time.monotonic", return_value=90.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.emergency_stop is False
    assert d.price_shock_paused is True


# ── State integrity tests ─────────────────────────────────────────────────────

def test_multiple_pause_resume_cycles_no_state_bleed():
    """Three independent spike→resume cycles must each start with clean state."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=2, max_pause_secs=9999)
    _prime_equity(rm)

    now = datetime.now(UTC)
    base_price = 50_000.0
    t = 10.0

    for episode in range(3):
        # Spike
        rm.register_price(now=now - timedelta(minutes=30), price=base_price)
        rm.register_price(now=now - timedelta(minutes=15), price=base_price)
        rm.register_price(now=now, price=base_price * 1.20)

        with patch("time.monotonic", return_value=t):
            d = rm.evaluate(equity=1_000.0)
        assert d.price_shock_paused is True, f"Episode {episode}: shock not detected"

        # Normalise
        rm.register_price(now=now + timedelta(minutes=1), price=base_price * 1.01)
        rm.register_price(now=now + timedelta(minutes=2), price=base_price * 1.01)
        t += 6.0
        with patch("time.monotonic", return_value=t):
            rm.evaluate(equity=1_000.0)  # clean cycle 1
        t += 6.0
        with patch("time.monotonic", return_value=t):
            d = rm.evaluate(equity=1_000.0)  # clean cycle 2 → resume
        assert d.price_shock_paused is False, f"Episode {episode}: did not resume"
        assert rm._price_shock_clean_cycles == 0
        assert rm._price_shock_pause_start is None

        t += 100.0  # large gap between episodes


def test_price_shock_paused_field_correct_in_all_stages():
    """Verify price_shock_paused=False in warmup, True during pause, False after resume."""
    rm = _make_rm(min_samples=5, hourly_move_pct=0.08, resume_cycles=2)
    _prime_equity(rm)

    # Stage 0 — warmup: fewer than min_samples
    _register_prices(rm, count=2)
    with patch("time.monotonic", return_value=10.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is False

    # Stage 1 — shock detected after reaching min_samples
    _register_prices(rm, count=5, start_price=50_000.0, end_price=60_000.0)
    with patch("time.monotonic", return_value=16.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # Stage 2 — still paused, within lazy-eval interval (cached result)
    with patch("time.monotonic", return_value=18.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # Stage 3 — clean cycles → resume
    _register_prices(rm, count=5, start_price=50_000.0, end_price=50_100.0)
    with patch("time.monotonic", return_value=22.0):
        rm.evaluate(equity=1_000.0)  # clean 1
    with patch("time.monotonic", return_value=28.0):
        d = rm.evaluate(equity=1_000.0)  # clean 2 → resume
    assert d.price_shock_paused is False


def test_block_new_grids_true_during_pause():
    """block_new_grids must be True at every evaluation cycle during a pause."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.05, resume_cycles=5)
    _prime_equity(rm)
    _register_prices(rm, count=5, start_price=50_000.0, end_price=60_000.0)

    for t in [10.0, 13.0, 16.0]:
        with patch("time.monotonic", return_value=t):
            d = rm.evaluate(equity=1_000.0)
        assert d.block_new_grids is True, f"block_new_grids should be True at t={t}"


def test_block_new_grids_false_after_auto_resume():
    """block_new_grids must be False immediately after auto-resume."""
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=2)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)

    rm.register_price(now=now + timedelta(minutes=1), price=50_300.0)
    rm.register_price(now=now + timedelta(minutes=2), price=50_300.0)
    with patch("time.monotonic", return_value=16.0):
        rm.evaluate(equity=1_000.0)
    with patch("time.monotonic", return_value=22.0):
        d = rm.evaluate(equity=1_000.0)  # resumes

    assert d.block_new_grids is False
    assert d.allow_trading is True
    assert d.emergency_stop is False


# ── Interaction with existing circuit breakers ────────────────────────────────

def test_drawdown_takes_priority_over_price_shock_pause():
    """
    If max drawdown triggers while price shock is paused, emergency_stop must
    fire (drawdown check runs before price shock section in evaluate()).
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=5)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=0.0):
        rm.evaluate(equity=1_000.0)  # peak = 1000

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)  # pause activates

    # Equity collapses 30 % (> 25 % drawdown threshold)
    with patch("time.monotonic", return_value=16.0):
        d = rm.evaluate(equity=700.0)

    assert d.emergency_stop is True
    assert "drawdown" in d.reason.lower()


def test_price_shock_pause_does_not_interfere_with_daily_loss():
    """
    Daily loss must still fire correctly even when price shock pause
    state is populated (drawdown/daily-loss checks come first).
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=5)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    with patch("time.monotonic", return_value=0.0):
        rm.evaluate(equity=1_000.0)

    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)  # pause activates

    # Daily loss: 12 % > 10 % threshold
    with patch("time.monotonic", return_value=16.0):
        d = rm.evaluate(equity=880.0)

    assert d.allow_trading is False
    assert d.emergency_stop is False
    assert "daily_loss" in d.reason.lower()


# ── Phase G lazy-eval timing interaction ──────────────────────────────────────

def test_phase_g_lazy_eval_timer_resets_on_warmup_guard_fire():
    """
    The lazy-eval timer must reset even when the cold-start guard fires,
    so that the guard does not permanently suppress evaluation.
    """
    rm = _make_rm(min_samples=10)
    _prime_equity(rm)
    _register_prices(rm, count=3)

    # First lazy-eval cycle: warmup guard fires, but timer still resets
    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)
    assert rm._last_price_shock_eval == 10.0  # timer was reset

    # Next call within 5s: lazy eval skipped (timer respected)
    with patch("time.monotonic", return_value=12.0):
        rm.evaluate(equity=1_000.0)
    assert rm._last_price_shock_eval == 10.0  # unchanged


def test_pause_state_maintained_between_lazy_eval_intervals():
    """
    Between lazy-eval intervals, the pause state must persist so that
    block_new_grids=True is returned on every main-loop iteration.
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, resume_cycles=5)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    # t=10: lazy eval fires, pause starts
    with patch("time.monotonic", return_value=10.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.price_shock_paused is True

    # t=12, t=13, t=14: within 5s interval — lazy eval skipped, pause persists
    for t in [12.0, 13.0, 14.0]:
        with patch("time.monotonic", return_value=t):
            d = rm.evaluate(equity=1_000.0)
        assert d.price_shock_paused is True, f"Pause not maintained at t={t}"
        assert d.block_new_grids is True


def test_escalation_detected_between_lazy_eval_intervals():
    """
    Escalation (pause_duration > max_pause_secs) must be detectable on every
    evaluate() call, not just at lazy-eval boundaries.
    """
    rm = _make_rm(min_samples=3, hourly_move_pct=0.08, max_pause_secs=50, resume_cycles=5)
    _prime_equity(rm)

    now = datetime.now(UTC)
    rm.register_price(now=now - timedelta(minutes=30), price=50_000.0)
    rm.register_price(now=now - timedelta(minutes=15), price=50_000.0)
    rm.register_price(now=now, price=60_000.0)

    # t=10: lazy eval, pause_start = 10; last_eval = 10
    with patch("time.monotonic", return_value=10.0):
        rm.evaluate(equity=1_000.0)

    # t=12: within 5s interval (lazy eval skipped), but pause_duration = 2 < 50 → ok
    with patch("time.monotonic", return_value=12.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.emergency_stop is False

    # t=62: pause_duration = 52 > 50, but still within lazy eval (62-10=52 >= 5 → eval fires)
    # Lazy eval runs (confirms still above threshold), then escalation check fires
    with patch("time.monotonic", return_value=62.0):
        d = rm.evaluate(equity=1_000.0)
    assert d.emergency_stop is True
    assert "price_shock_sustained" in d.reason
