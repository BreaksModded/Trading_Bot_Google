"""FIX-B tests: Volume ratio NaN fallback changed from 0.0 to 1.0.

Validates that NaN data returns neutral 1.0 (not blocking 0.0),
real data still returns correct ratios, and zero-volume candles
return 0.0 (real low volume, not a NaN fallback).
"""

import math
import pandas as pd
import pytest

from core.indicators import compute_volume_ratio


class TestVolumeRatioNaNFallback:
    """FIX-B Test 1: NaN data returns 1.0 (neutral), not 0.0 (block)."""

    def test_volume_ratio_returns_1_on_nan_data(self):
        """A DataFrame with NaN volume values should produce ratio 1.0
        via fillna(1.0), not the old fillna(0.0) which blocked trading."""
        # 10 normal values, then NaN values to simulate missing data
        data = [100.0] * 10 + [float('nan')] * 5
        series = pd.Series(data)

        ratios = compute_volume_ratio(series, period=10)

        # The NaN values should be filled with 1.0 (neutral)
        for i in range(10, len(ratios)):
            assert ratios.iloc[i] == 1.0, (
                f"NaN value at index {i} produced ratio {ratios.iloc[i]}, "
                f"expected 1.0 (neutral fallback)"
            )


class TestVolumeRatioRealData:
    """FIX-B Test 2: Real data still returns correct ratios."""

    def test_volume_ratio_returns_correct_on_real_data(self):
        """Real low-volume data returns ratio < 1.0, high-volume > 1.0.
        These are NOT NaN, so fillna doesn't affect them."""
        # 20 bars of normal volume
        normal = [100.0] * 20

        # Low volume bar: 30 / median(100) = 0.3
        low_vol = [30.0]
        series_low = pd.Series(normal + low_vol)
        ratios_low = compute_volume_ratio(series_low, period=20)
        last_low = ratios_low.iloc[-1]
        assert last_low < 1.0, f"Low volume ratio was {last_low}, expected < 1.0"
        assert last_low > 0.0, f"Low volume ratio was {last_low}, should NOT be 0.0"

        # High volume bar: 300 / median(100) = 3.0
        high_vol = [300.0]
        series_high = pd.Series(normal + high_vol)
        ratios_high = compute_volume_ratio(series_high, period=20)
        last_high = ratios_high.iloc[-1]
        assert last_high > 1.0, f"High volume ratio was {last_high}, expected > 1.0"


class TestVolumeRatioZeroVsCandleNaN:
    """FIX-B Test 3: Zero-volume candle vs NaN candle distinguished."""

    def test_volume_ratio_zero_volume_candle_not_nan(self):
        """A candle with actual volume=0 returns 0.0 (real data).
        Only NaN (missing data) returns the 1.0 fallback."""
        base = [100.0] * 20

        # Zero-volume candle: 0 / median(100) = 0.0 (real computation)
        zero_vol = [0.0]
        series_zero = pd.Series(base + zero_vol)
        ratios_zero = compute_volume_ratio(series_zero, period=20)
        last_zero = ratios_zero.iloc[-1]
        assert last_zero == 0.0, (
            f"Zero-volume candle returned {last_zero}, expected 0.0 (real low volume)"
        )

        # NaN candle: NaN / median(100) = NaN → fillna(1.0) → 1.0
        nan_vol = [float('nan')]
        series_nan = pd.Series(base + nan_vol)
        ratios_nan = compute_volume_ratio(series_nan, period=20)
        last_nan = ratios_nan.iloc[-1]
        assert last_nan == 1.0, (
            f"NaN-volume candle returned {last_nan}, expected 1.0 (fallback for missing data)"
        )
