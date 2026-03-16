"""FIX-C tests: G4 and G5 removed, G1/G2/G3 preserved.

Validates that:
1. Time-stale grids refresh even without price movement (G4 gone)
2. Grids with many open buys can still refresh (G5 gone)
3. G1 (ADX downtrend) still blocks
4. G2 (inventory cap) still blocks
5. G3 (cooldown) still blocks
"""

import pytest

from core.grid_refresh import evaluate_safety_gates


def _base_kwargs(**overrides) -> dict:
    """Baseline kwargs where all remaining gates PASS."""
    defaults = dict(
        adx_value=20.0,
        trend_bias="long",
        inventory_ratio=0.20,
        open_buy_count=5,  # Previously would block G5; now irrelevant
        time_since_last_refresh_s=3600.0,
        price_move_since_last_pct=0.001,  # Previously would block G4; now irrelevant
        adx_block_threshold=35.0,
        max_inventory_ratio=0.40,
        cooldown_seconds=1800,
        min_move_pct=0.02,
        skip_if_orders_above=2,
    )
    defaults.update(overrides)
    return defaults


class TestG4Removed:
    """FIX-C Test 1: Time-stale refresh executes without price movement."""

    def test_time_stale_refresh_executes_without_price_move(self):
        """With G4 removed, a grid that's time-stale but hasn't moved
        price-wise should still pass all safety gates. This was the
        primary deadlock scenario in lateral markets."""
        passed, results = evaluate_safety_gates(
            **_base_kwargs(
                price_move_since_last_pct=0.001,  # Tiny move, would fail old G4
                time_since_last_refresh_s=7200.0,  # Past cooldown
            )
        )
        assert passed is True, (
            f"Expected all gates to pass (G4 removed), but got: "
            f"{[(g.gate_name, g.passed, g.reason) for g in results]}"
        )
        # Verify G4 is NOT in results
        gate_names = [g.gate_name for g in results]
        assert "G4_MIN_MOVE" not in gate_names


class TestG5Removed:
    """FIX-C Test 2: Refresh executes with multiple open buy orders."""

    def test_refresh_executes_with_multiple_open_buys(self):
        """With G5 removed, many open buy orders (even old ones at $87)
        should NOT block the refresh. This was the Order Count deadlock."""
        passed, results = evaluate_safety_gates(
            **_base_kwargs(
                open_buy_count=10,  # Way above old threshold; would fail G5
                time_since_last_refresh_s=3600.0,
            )
        )
        assert passed is True, (
            f"Expected all gates to pass (G5 removed), but got: "
            f"{[(g.gate_name, g.passed, g.reason) for g in results]}"
        )
        gate_names = [g.gate_name for g in results]
        assert "G5_ORDER_COUNT" not in gate_names


class TestG1StillBlocks:
    """FIX-C Test 3: G1 (ADX downtrend filter) still blocks correctly."""

    def test_g1_still_blocks_on_strong_downtrend(self):
        """Strong bearish trend with high ADX should still block refresh
        to prevent buying into a falling knife."""
        passed, results = evaluate_safety_gates(
            **_base_kwargs(
                adx_value=45.0,  # Strong trend
                trend_bias="short",  # Bearish
            )
        )
        assert passed is False
        g1 = next(g for g in results if g.gate_name == "G1_ADX_TREND")
        assert g1.passed is False
        assert "BLOCKED" in g1.reason
        assert "falling knife" in g1.reason


class TestG2StillBlocks:
    """FIX-C Test 4: G2 (inventory cap) still blocks correctly."""

    def test_g2_still_blocks_on_inventory_cap(self):
        """Inventory ratio above the cap should still block refresh
        to prevent over-allocating capital to one pair."""
        passed, results = evaluate_safety_gates(
            **_base_kwargs(
                inventory_ratio=0.60,  # Above 0.40 cap
            )
        )
        assert passed is False
        g2 = next(g for g in results if g.gate_name == "G2_INVENTORY")
        assert g2.passed is False
        assert "BLOCKED" in g2.reason


class TestG3StillBlocks:
    """FIX-C Test 5: G3 (cooldown) still blocks correctly."""

    def test_g3_still_blocks_on_cooldown(self):
        """Recently refreshed grids should still be blocked by cooldown
        to prevent API spam."""
        passed, results = evaluate_safety_gates(
            **_base_kwargs(
                time_since_last_refresh_s=600.0,  # 10 min, below 1800s cooldown
                cooldown_seconds=1800,
            )
        )
        assert passed is False
        g3 = next(g for g in results if g.gate_name == "G3_COOLDOWN")
        assert g3.passed is False
        assert "BLOCKED" in g3.reason
