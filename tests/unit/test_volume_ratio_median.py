"""Tests for spike-resistant volume ratio (median vs SMA).

Validates that switching from SMA to median in compute_volume_ratio
prevents a single volume spike from suppressing subsequent normal bars.
"""

import pandas as pd
import pytest

from core.indicators import compute_volume_ratio


class TestVolumeRatioSpikeResistance:
    """Verify that median-based volume ratio resists outlier spikes."""

    def test_volume_ratio_spike_resistant(self) -> None:
        """A 10× spike should NOT drag down normal bars that follow.

        Scenario: 19 normal bars (volume=100), 1 spike (volume=1000),
        then 1 normal bar (volume=100).

        With SMA: the normal bar after spike would read ~100/145 ≈ 0.69 → blocked at 0.70.
        With median: the normal bar reads ~100/100 = 1.0 → passed.
        """
        normal = [100.0] * 19
        spike = [1000.0]
        post_spike = [100.0]
        series = pd.Series(normal + spike + post_spike)

        ratios = compute_volume_ratio(series, period=20)
        last_ratio = ratios.iloc[-1]

        # With median, normal volume after spike ≈ 1.0 (100/100)
        assert last_ratio > 0.90, (
            f"Post-spike ratio was {last_ratio:.4f}; expected > 0.90 (median resistant). "
            "If this is ~0.69, the SMA bug has regressed."
        )

        # The spike bar itself should still show as high
        spike_ratio = ratios.iloc[-2]
        assert spike_ratio > 5.0, (
            f"Spike ratio was {spike_ratio:.4f}; expected > 5.0"
        )

    def test_volume_ratio_normal_conditions(self) -> None:
        """Under uniform volume, median ≈ mean → ratio ≈ 1.0.

        Verifies that the switch to median doesn't break normal behavior.
        """
        uniform = [100.0] * 30
        series = pd.Series(uniform)

        ratios = compute_volume_ratio(series, period=20)

        # All ratios after warmup should be ~1.0
        for i in range(20, len(ratios)):
            assert abs(ratios.iloc[i] - 1.0) < 0.01, (
                f"Ratio at index {i} was {ratios.iloc[i]:.4f}; expected ~1.0 under uniform volume"
            )

    def test_volume_ratio_gradual_increase(self) -> None:
        """Gradually increasing volume should produce ratios > 1.0.

        Confirms median tracks underlying trend, not just spikes.
        """
        # 20 bars of volume 100, then 5 bars increasing to 200
        base = [100.0] * 20
        increasing = [120.0, 140.0, 160.0, 180.0, 200.0]
        series = pd.Series(base + increasing)

        ratios = compute_volume_ratio(series, period=20)
        last_ratio = ratios.iloc[-1]

        # 200 / median(mix of 100s and a few >100) should be > 1
        assert last_ratio > 1.5, (
            f"Ratio at end was {last_ratio:.4f}; expected > 1.5 for 200 vs mostly-100 median"
        )

    def test_volume_ratio_zero_handling(self) -> None:
        """Zero volume bars should not cause division errors."""
        data = [100.0] * 10 + [0.0] * 5 + [100.0] * 10
        series = pd.Series(data)

        ratios = compute_volume_ratio(series, period=20)

        # No NaN or inf values
        assert not ratios.isna().any(), "NaN values found in volume ratio"
        assert not (ratios == float('inf')).any(), "Inf values found in volume ratio"

    def test_volume_ratio_period_validation(self) -> None:
        """Period <= 0 should raise ValueError."""
        series = pd.Series([100.0] * 10)

        with pytest.raises(ValueError, match="positive"):
            compute_volume_ratio(series, period=0)

        with pytest.raises(ValueError, match="positive"):
            compute_volume_ratio(series, period=-5)
