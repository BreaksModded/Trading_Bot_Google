"""Unit tests for Phase I: Dynamic Capital-Proportional Order Sizing."""

import pytest
from unittest.mock import patch

from core.dynamic_sizing import compute_dynamic_order_size


# ─── Settings field — pct configurability ────────────────────────────


class TestOrderSizePctSettings:
    """order_size_pct_per_level is configurable; ge=0.01, le=0.30."""

    def test_configured_pct_14_produces_24_usdt_on_173_capital(self):
        """pct=0.14, capital=173 → 173×0.14=24.22 USDT per level (not 10€ floor)."""
        result = compute_dynamic_order_size(
            available_capital_usdt=173.0,
            order_size_pct=0.14,
            reinvestment_multiplier=1.0,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=25.0,
            enabled=True,
        )
        assert result == pytest.approx(173.0 * 0.14)  # 24.22

    def test_default_pct_005_is_preserved(self):
        """Field code default is 0.05 — env can override, but code fallback stays 5%."""
        from config.settings import GridSettings
        # Inspect Field.default directly — avoids .env interference
        code_default = GridSettings.model_fields["order_size_pct_per_level"].default
        assert code_default == pytest.approx(0.05)

    def test_pct_below_minimum_rejected(self):
        """Values below ge=0.01 (e.g. 0.0, 0.005) raise ValidationError."""
        import pydantic
        from config.settings import GridSettings
        with pytest.raises(pydantic.ValidationError):
            GridSettings(order_size_pct_per_level=0.005)

    def test_pct_above_maximum_rejected(self):
        """Values above le=0.30 (e.g. 0.31, 0.50) raise ValidationError."""
        import pydantic
        from config.settings import GridSettings
        with pytest.raises(pydantic.ValidationError):
            GridSettings(order_size_pct_per_level=0.31)


# ─── Disabled mode (backward compat) ──────────────────────────────────

class TestDisabledMode:
    """When enabled=False, function must produce exact current behavior."""

    def test_disabled_returns_fixed_size_times_multiplier(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=5000.0,
            order_size_pct=0.05,
            reinvestment_multiplier=1.10,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=False,
        )
        assert result == pytest.approx(300.0 * 1.10, rel=1e-6)

    def test_disabled_ignores_capital_and_pct(self):
        """Capital and percentage must be irrelevant when disabled."""
        r1 = compute_dynamic_order_size(1000, 0.05, 1.0, 10, 0, 300, enabled=False)
        r2 = compute_dynamic_order_size(99999, 0.50, 1.0, 10, 0, 300, enabled=False)
        assert r1 == r2 == pytest.approx(300.0)

    def test_disabled_zero_behavioral_change(self):
        """Exact reproduction of legacy behavior: fixed_size × multiplier."""
        result = compute_dynamic_order_size(0, 0, 1.05, 10, 0, 25.0, enabled=False)
        assert result == pytest.approx(25.0 * 1.05)


# ─── Basic percentage calculation ─────────────────────────────────────

class TestBasicCalculation:
    def test_basic_percentage_correct(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=2000.0,
            order_size_pct=0.05,
            reinvestment_multiplier=1.0,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=True,
        )
        assert result == pytest.approx(100.0)  # 2000 × 0.05

    def test_multiplier_applied_correctly(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=2000.0,
            order_size_pct=0.05,
            reinvestment_multiplier=0.95,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=True,
        )
        assert result == pytest.approx(95.0)  # 2000 × 0.05 × 0.95

    def test_multiplier_of_one_produces_pure_percentage(self):
        result = compute_dynamic_order_size(1000, 0.06, 1.0, 10, 0, 300, enabled=True)
        assert result == pytest.approx(60.0)  # 1000 × 0.06 × 1.0


# ─── Min/Max enforcement ──────────────────────────────────────────────

class TestMinMaxEnforcement:
    def test_minimum_floor_enforced(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=100.0,
            order_size_pct=0.05,
            reinvestment_multiplier=1.0,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=True,
        )
        # 100 × 0.05 = 5.0 < 10.0 → should clamp to 10.0
        assert result == pytest.approx(10.0)

    @patch("core.dynamic_sizing.logger")
    def test_minimum_floor_logs_warning(self, mock_logger):
        compute_dynamic_order_size(100, 0.05, 1.0, 10, 0, 300, enabled=True)
        mock_logger.warning.assert_called_once()
        assert "below minimum" in mock_logger.warning.call_args[0][0]

    def test_maximum_cap_enforced(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=10000.0,
            order_size_pct=0.10,
            reinvestment_multiplier=1.0,
            min_order_usdt=10.0,
            max_order_usdt=500.0,
            fallback_fixed_size=300.0,
            enabled=True,
        )
        # 10000 × 0.10 = 1000 > 500 → should cap to 500
        assert result == pytest.approx(500.0)

    def test_zero_cap_means_no_limit(self):
        result = compute_dynamic_order_size(
            available_capital_usdt=100000.0,
            order_size_pct=0.10,
            reinvestment_multiplier=1.0,
            min_order_usdt=10.0,
            max_order_usdt=0.0,  # no cap
            fallback_fixed_size=300.0,
            enabled=True,
        )
        assert result == pytest.approx(10000.0)  # 100000 × 0.10


# ─── Edge cases / safety ──────────────────────────────────────────────

class TestEdgeCases:
    def test_never_returns_zero(self):
        result = compute_dynamic_order_size(0, 0.05, 1.0, 10, 0, 300, enabled=True)
        assert result > 0

    def test_never_returns_negative(self):
        result = compute_dynamic_order_size(-500, 0.05, 1.0, 10, 0, 300, enabled=True)
        assert result > 0

    def test_never_raises_on_zero_capital(self):
        # Must not crash, should return minimum floor
        result = compute_dynamic_order_size(0, 0.05, 1.0, 10.0, 0, 300, enabled=True)
        assert result == pytest.approx(10.0)

    def test_never_raises_on_none_inputs(self):
        # None values should be handled gracefully
        result = compute_dynamic_order_size(None, None, None, None, None, None, enabled=True)
        assert result > 0

    def test_negative_multiplier_treated_as_one(self):
        result = compute_dynamic_order_size(2000, 0.05, -1.0, 10, 0, 300, enabled=True)
        assert result == pytest.approx(100.0)  # uses multiplier=1.0


# ─── Growth & drawdown scenarios ──────────────────────────────────────

class TestScalingScenarios:
    def test_scales_proportionally_with_capital(self):
        r1 = compute_dynamic_order_size(1000, 0.05, 1.0, 10, 0, 300, enabled=True)
        r2 = compute_dynamic_order_size(2000, 0.05, 1.0, 10, 0, 300, enabled=True)
        r3 = compute_dynamic_order_size(5000, 0.05, 1.0, 10, 0, 300, enabled=True)
        assert r1 == pytest.approx(50.0)
        assert r2 == pytest.approx(100.0)
        assert r3 == pytest.approx(250.0)
        # Linear proportionality
        assert r2 / r1 == pytest.approx(2.0)
        assert r3 / r1 == pytest.approx(5.0)

    def test_reduces_proportionally_in_drawdown(self):
        r_initial = compute_dynamic_order_size(1000, 0.05, 1.0, 10, 0, 300, enabled=True)
        r_drawdown = compute_dynamic_order_size(650, 0.05, 1.0, 10, 0, 300, enabled=True)
        assert r_drawdown == pytest.approx(32.50)
        assert r_drawdown < r_initial  # Automatically smaller in drawdown

    def test_very_small_account_hits_floor(self):
        """Account with €150, 5% = €7.50 → below €10 min → uses floor."""
        result = compute_dynamic_order_size(150, 0.05, 1.0, 10, 0, 300, enabled=True)
        assert result == pytest.approx(10.0)


# ─── Double-counting proof ───────────────────────────────────────────

class TestNoDoubleCounting:
    def test_growth_not_double_counted(self):
        """
        Capital grows €1,000 → €1,200.
        With dynamic sizing, order should be €60 (1200 × 5%), NOT €72.
        The multiplier is forced to 1.0 in dynamic mode.
        """
        result = compute_dynamic_order_size(
            available_capital_usdt=1200.0,
            order_size_pct=0.05,
            reinvestment_multiplier=1.0,  # forced by main.py
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=True,
        )
        assert result == pytest.approx(60.0)
        assert result != pytest.approx(72.0)  # NOT double-counted

    def test_legacy_mode_uses_multiplier_correctly(self):
        """When disabled, the old multiplier behavior is preserved."""
        result = compute_dynamic_order_size(
            available_capital_usdt=1200.0,
            order_size_pct=0.05,
            reinvestment_multiplier=1.20,
            min_order_usdt=10.0,
            max_order_usdt=0.0,
            fallback_fixed_size=300.0,
            enabled=False,
        )
        assert result == pytest.approx(300.0 * 1.20)  # 360 — old behavior
