"""Phase H — Unit tests for grid refresh safety gates and OR triggers."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.grid_refresh import (
    RefreshConfig,
    RefreshState,
    RefreshGateResult,
    StalenessResult,
    is_grid_stale,
    should_refresh,
    evaluate_safety_gates,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago(hours: float = 0, minutes: float = 0, seconds: float = 0) -> datetime:
    return _now() - timedelta(hours=hours, minutes=minutes, seconds=seconds)


# ── OR / AND Trigger tests ───────────────────────────────────────────


class TestTriggerModes:
    """Test Phase H OR trigger mode vs legacy AND mode."""

    def test_or_trigger_price_only(self):
        """Price deviation alone should trigger in OR mode."""
        result = is_grid_stale(
            current_price=105.0,
            anchor_price=100.0,  # 5% deviation
            grid_created_at=_ago(hours=1),  # Young grid — age NOT met
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="OR",
        )
        assert result.is_stale is True
        assert "dev" in result.reason.lower() or "stale" in result.reason.lower()

    def test_or_trigger_time_only(self):
        """Time expiry alone should trigger in OR mode."""
        result = is_grid_stale(
            current_price=100.5,
            anchor_price=100.0,  # 0.5% deviation — price NOT met
            grid_created_at=_ago(hours=8),  # Old grid — age met
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="OR",
        )
        assert result.is_stale is True

    def test_and_trigger_both_required(self):
        """AND mode requires BOTH conditions (legacy behavior)."""
        # Price met but age not met
        result = is_grid_stale(
            current_price=105.0,
            anchor_price=100.0,
            grid_created_at=_ago(hours=1),
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="AND",
        )
        assert result.is_stale is False

    def test_and_trigger_fires_when_both_met(self):
        """AND mode fires when both price AND age are met."""
        result = is_grid_stale(
            current_price=105.0,
            anchor_price=100.0,
            grid_created_at=_ago(hours=8),
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="AND",
        )
        assert result.is_stale is True

    def test_independent_trigger_disable_price(self):
        """Disabling price trigger should only allow time trigger."""
        result = is_grid_stale(
            current_price=200.0,  # Massive deviation
            anchor_price=100.0,
            grid_created_at=_ago(hours=1),  # Young grid
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="OR",
            enable_price_trigger=False,
            enable_time_trigger=True,
        )
        assert result.is_stale is False  # Price disabled, time not met

    def test_independent_trigger_disable_time(self):
        """Disabling time trigger should only allow price trigger."""
        result = is_grid_stale(
            current_price=100.0,
            anchor_price=100.0,  # No deviation
            grid_created_at=_ago(hours=100),  # Very old grid
            atr_pct=0.01,
            cfg=RefreshConfig(price_distance_pct=0.04, max_grid_age_hours=6.0),
            trigger_mode="OR",
            enable_price_trigger=True,
            enable_time_trigger=False,
        )
        assert result.is_stale is False  # Time disabled, price not met


# ── Safety Gate Tests ────────────────────────────────────────────────


class TestSafetyGates:
    """Test all 5 Phase H safety gates individually."""

    def _base_kwargs(self, **overrides) -> dict:
        """Baseline kwargs where all gates PASS."""
        defaults = dict(
            adx_value=20.0,
            trend_bias="long",
            inventory_ratio=0.20,
            open_buy_count=1,
            time_since_last_refresh_s=3600.0,
            price_move_since_last_pct=0.05,
            adx_block_threshold=35.0,
            max_inventory_ratio=0.40,
            cooldown_seconds=1800,
            min_move_pct=0.02,
            skip_if_orders_above=2,
        )
        defaults.update(overrides)
        return defaults

    # G1 — ADX Trend Filter

    def test_gate1_blocks_downtrend(self):
        """G1 blocks when ADX > threshold AND trend is SHORT."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(adx_value=40.0, trend_bias="short")
        )
        assert passed is False
        g1 = next(g for g in results if g.gate_name == "G1_ADX_TREND")
        assert g1.passed is False
        assert "BLOCKED" in g1.reason

    def test_gate1_allows_uptrend(self):
        """G1 allows when ADX > threshold but trend is LONG."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(adx_value=40.0, trend_bias="long")
        )
        g1 = next(g for g in results if g.gate_name == "G1_ADX_TREND")
        assert g1.passed is True

    def test_gate1_allows_ranging(self):
        """G1 allows when ADX is low regardless of trend."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(adx_value=15.0, trend_bias="short")
        )
        g1 = next(g for g in results if g.gate_name == "G1_ADX_TREND")
        assert g1.passed is True

    def test_gate1_allows_neutral_trend(self):
        """G1 allows neutral trend even with high ADX."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(adx_value=50.0, trend_bias="neutral")
        )
        g1 = next(g for g in results if g.gate_name == "G1_ADX_TREND")
        assert g1.passed is True

    # G2 — Inventory Cap

    def test_gate2_blocks_high_inventory(self):
        """G2 blocks when inventory ratio exceeds cap."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(inventory_ratio=0.55)
        )
        assert passed is False
        g2 = next(g for g in results if g.gate_name == "G2_INVENTORY")
        assert g2.passed is False

    def test_gate2_allows_low_inventory(self):
        """G2 allows when inventory ratio is below cap."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(inventory_ratio=0.20)
        )
        g2 = next(g for g in results if g.gate_name == "G2_INVENTORY")
        assert g2.passed is True

    def test_gate2_edge_at_exact_threshold(self):
        """G2 allows at exactly the threshold (<=)."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(inventory_ratio=0.40)
        )
        g2 = next(g for g in results if g.gate_name == "G2_INVENTORY")
        assert g2.passed is True

    # G3 — Cooldown

    def test_gate3_blocks_within_cooldown(self):
        """G3 blocks when not enough time has elapsed since last refresh."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(time_since_last_refresh_s=600.0, cooldown_seconds=1800)
        )
        assert passed is False
        g3 = next(g for g in results if g.gate_name == "G3_COOLDOWN")
        assert g3.passed is False

    def test_gate3_allows_after_cooldown(self):
        """G3 allows after cooldown has elapsed."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(time_since_last_refresh_s=2000.0, cooldown_seconds=1800)
        )
        g3 = next(g for g in results if g.gate_name == "G3_COOLDOWN")
        assert g3.passed is True

    # G4 — Minimum Price Movement

    def test_gate4_blocks_small_move(self):
        """G4 blocks when price has barely moved since last refresh."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(price_move_since_last_pct=0.005, min_move_pct=0.02)
        )
        assert passed is False
        g4 = next(g for g in results if g.gate_name == "G4_MIN_MOVE")
        assert g4.passed is False

    def test_gate4_allows_sufficient_move(self):
        """G4 allows when price has moved enough."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(price_move_since_last_pct=0.03, min_move_pct=0.02)
        )
        g4 = next(g for g in results if g.gate_name == "G4_MIN_MOVE")
        assert g4.passed is True

    # G5 — Open Orders Threshold

    def test_gate5_blocks_enough_orders(self):
        """G5 blocks when too many buy orders are still open (grid in range)."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(open_buy_count=4, skip_if_orders_above=2)
        )
        assert passed is False
        g5 = next(g for g in results if g.gate_name == "G5_ORDER_COUNT")
        assert g5.passed is False

    def test_gate5_allows_few_orders(self):
        """G5 allows when few buy orders remain (grid out of range)."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(open_buy_count=1, skip_if_orders_above=2)
        )
        g5 = next(g for g in results if g.gate_name == "G5_ORDER_COUNT")
        assert g5.passed is True

    # All gates combined

    def test_all_gates_pass(self):
        """When all conditions are favorable, all 5 gates pass."""
        passed, results = evaluate_safety_gates(**self._base_kwargs())
        assert passed is True
        assert len(results) == 5
        assert all(g.passed for g in results)

    def test_all_gates_logged(self):
        """Every gate produces a named result for audit."""
        _, results = evaluate_safety_gates(**self._base_kwargs())
        gate_names = [g.gate_name for g in results]
        assert "G1_ADX_TREND" in gate_names
        assert "G2_INVENTORY" in gate_names
        assert "G3_COOLDOWN" in gate_names
        assert "G4_MIN_MOVE" in gate_names
        assert "G5_ORDER_COUNT" in gate_names

    def test_multiple_gates_fail_first_reported(self):
        """When multiple gates fail, all are evaluated (not short-circuited)."""
        passed, results = evaluate_safety_gates(
            **self._base_kwargs(
                adx_value=45.0, trend_bias="short",  # G1 fails
                inventory_ratio=0.60,                  # G2 fails
                open_buy_count=5,                      # G5 fails
            )
        )
        assert passed is False
        failed = [g for g in results if not g.passed]
        assert len(failed) >= 3


# ── should_refresh guardrails ────────────────────────────────────────


class TestShouldRefresh:
    """Test existing guardrails still work with Phase H."""

    def test_daily_cap_blocks(self):
        state = RefreshState(refresh_count_today=4, refresh_count_date="2026-03-03")
        stale = StalenessResult(is_stale=True, reason="stale", price_deviation_pct=0.05, grid_age_hours=8.0, threshold_used_pct=0.04)
        cfg = RefreshConfig(max_refreshes_per_day=4)
        go, reason = should_refresh(state, stale, cfg, now=datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc))
        assert go is False
        assert "daily_cap" in reason

    def test_confirmation_cycle_required(self):
        state = RefreshState(stale_cycle_count=0)
        stale = StalenessResult(is_stale=True, reason="stale", price_deviation_pct=0.05, grid_age_hours=8.0, threshold_used_pct=0.04)
        cfg = RefreshConfig(stale_confirm_cycles=2)
        go, _ = should_refresh(state, stale, cfg)
        assert go is False
        assert state.stale_cycle_count == 1

    def test_refresh_disabled_zero_change(self):
        """When is_stale is False, should_refresh returns False (defensive mode)."""
        state = RefreshState()
        stale = StalenessResult(is_stale=False, reason="Active", price_deviation_pct=0.01, grid_age_hours=1.0, threshold_used_pct=0.04)
        cfg = RefreshConfig()
        go, reason = should_refresh(state, stale, cfg)
        assert go is False
        assert reason == "not_stale"
