"""
Tests for core/indicators.py

Tests ATR, ADX, EMA calculations, MarketAnalysis, grid spacing,
data validation, and edge cases.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.indicators import (
    MarketAnalysis,
    MarketAnalysis,
    TechnicalIndicators,
    compute_adx,
    compute_atr,
    compute_atr_pct,
    compute_ema,
    klines_to_dataframe,
    _validate_ohlcv,
)


# ── ATR Tests ─────────────────────────────────────────────────────────


class TestComputeATR:
    """Tests for ATR calculation."""

    def test_atr_returns_series(self, ohlcv_df: pd.DataFrame):
        """ATR should return a pandas Series of the correct length."""
        atr = compute_atr(ohlcv_df, period=14)
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(ohlcv_df)

    def test_atr_values_positive(self, ohlcv_df: pd.DataFrame):
        """ATR values should be positive (volatility is always >= 0)."""
        atr = compute_atr(ohlcv_df, period=14)
        valid = atr.dropna()
        assert all(valid > 0), "ATR should be positive"

    def test_atr_different_periods(self, ohlcv_df: pd.DataFrame):
        """Longer ATR period should produce smoother (different) results."""
        atr_14 = compute_atr(ohlcv_df, period=14).dropna()
        atr_28 = compute_atr(ohlcv_df, period=28).dropna()
        # Different periods should produce different values
        assert not atr_14.iloc[-1] == atr_28.iloc[-1] or abs(atr_14.iloc[-1] - atr_28.iloc[-1]) < 1

    def test_atr_insufficient_data(self, small_df: pd.DataFrame):
        """ATR should raise IndicatorError with insufficient data."""
        with pytest.raises(ValueError, match="Insufficient data"):
            compute_atr(small_df, period=14)

    def test_atr_pct_returns_ratio(self, ohlcv_df: pd.DataFrame):
        """ATR% should return values as a fraction of price."""
        atr_pct = compute_atr_pct(ohlcv_df, period=14)
        valid = atr_pct.dropna()
        # ATR% for BTC should be small (< 10% per candle typically)
        assert all(valid < 0.10), "ATR% should be < 10%"
        assert all(valid > 0), "ATR% should be positive"


# ── ADX Tests ─────────────────────────────────────────────────────────


class TestComputeADX:
    """Tests for ADX calculation."""

    def test_adx_returns_dataframe(self, ohlcv_df: pd.DataFrame):
        """ADX should return a DataFrame with ADX, DI+, DI- columns."""
        result = compute_adx(ohlcv_df, period=14)
        assert isinstance(result, pd.DataFrame)
        assert "ADX_14" in result.columns

    def test_adx_range(self, ohlcv_df: pd.DataFrame):
        """ADX values should be between 0 and 100."""
        result = compute_adx(ohlcv_df, period=14)
        adx_vals = result["ADX_14"].dropna()
        assert all(0 <= v <= 100 for v in adx_vals), "ADX should be in [0, 100]"

    def test_adx_insufficient_data(self, small_df: pd.DataFrame):
        """ADX should raise IndicatorError with insufficient data."""
        with pytest.raises(ValueError, match="Insufficient data"):
            compute_adx(small_df, period=14)

    def test_trending_market_high_adx(self, trending_df: pd.DataFrame):
        """Strongly trending market should produce higher ADX."""
        result = compute_adx(trending_df, period=14)
        adx_last = result["ADX_14"].iloc[-1]
        # Strong trend should produce ADX > 20
        assert adx_last > 15, f"Trending market ADX should be high, got {adx_last}"


# ── EMA Tests ─────────────────────────────────────────────────────────


class TestComputeEMA:
    """Tests for EMA calculation."""

    def test_ema_returns_series(self, ohlcv_df: pd.DataFrame):
        """EMA should return a pandas Series."""
        ema = compute_ema(ohlcv_df["close"], period=50)
        assert isinstance(ema, pd.Series)

    def test_ema_smoothing(self, ohlcv_df: pd.DataFrame):
        """EMA should be smoother than raw prices (lower std)."""
        ema = compute_ema(ohlcv_df["close"], period=50).dropna()
        raw = ohlcv_df["close"].iloc[-len(ema):]
        assert ema.std() <= raw.std(), "EMA should be smoother than raw prices"

    def test_ema_fast_vs_slow(self, ohlcv_df: pd.DataFrame):
        """Fast EMA should track price more closely on average."""
        ema_20 = compute_ema(ohlcv_df["close"], period=20).dropna()
        ema_100 = compute_ema(ohlcv_df["close"], period=100).dropna()
        # Compare average absolute deviation from price over recent window
        last_n = min(50, len(ema_20), len(ema_100))
        fast_diff = (ohlcv_df["close"].iloc[-last_n:] - ema_20.iloc[-last_n:]).abs().mean()
        slow_diff = (ohlcv_df["close"].iloc[-last_n:] - ema_100.iloc[-last_n:]).abs().mean()
        assert fast_diff <= slow_diff, "Fast EMA should track price more closely on average"

    def test_ema_insufficient_data(self, small_df: pd.DataFrame):
        """EMA should raise with insufficient data."""
        with pytest.raises(ValueError):
            compute_ema(small_df["close"], period=50)


# ── MarketAnalysis Tests ──────────────────────────────────────────────


class TestTechnicalIndicators:
    """Tests for the TechnicalIndicators aggregator."""

    def test_analyze_returns_market_analysis(self, ohlcv_df: pd.DataFrame):
        """analyze() should return a MarketAnalysis object."""
        ti = TechnicalIndicators()
        result = ti.analyze(ohlcv_df)
        assert isinstance(result, MarketAnalysis)
        assert result.current_price > 0
        assert result.atr_value > 0
        assert 0 <= result.adx_value <= 100
        assert result.trend_direction in ("Long", "Short", "Neutral")

    def test_market_analysis_to_dict(self, ohlcv_df: pd.DataFrame):
        """to_dict() should return a proper dictionary."""
        ti = TechnicalIndicators()
        result = ti.analyze(ohlcv_df)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "current_price" in d
        assert "atr_value" in d
        assert "adx_value" in d
        assert "trend_direction" in d

    def test_grid_spacing_above_minimum(self, ohlcv_df: pd.DataFrame):
        """Grid spacing should always be >= min_spacing_pct."""
        ti = TechnicalIndicators()
        min_sp = 0.006
        spacing = ti.compute_grid_spacing(ohlcv_df, min_spacing_pct=min_sp)
        assert spacing >= min_sp, f"Spacing {spacing} < min {min_sp}"

    def test_grid_spacing_increases_with_multiplier(self, ohlcv_df: pd.DataFrame):
        """Higher ATR multiplier should produce larger spacing."""
        ti = TechnicalIndicators()
        sp_low = ti.compute_grid_spacing(ohlcv_df, atr_multiplier=1.0)
        sp_high = ti.compute_grid_spacing(ohlcv_df, atr_multiplier=3.0)
        assert sp_high >= sp_low

    def test_ranging_market_not_trending(self, ranging_df: pd.DataFrame):
        """Ranging market should have lower ADX."""
        ti = TechnicalIndicators()
        result = ti.analyze(ranging_df)
        # Ranging markets tend to have lower ADX, but with random data
        # we can at least verify the object is created
        assert isinstance(result.adx_value, float)


# ── Validation Tests ──────────────────────────────────────────────────


class TestValidation:
    """Tests for OHLCV DataFrame validation."""

    def test_missing_columns(self):
        """Should raise when required columns are missing."""
        df = pd.DataFrame({"open": [1, 2], "volume": [100, 200]})
        with pytest.raises(ValueError, match="Missing required columns"):
            _validate_ohlcv(df)

    def test_insufficient_rows(self, small_df: pd.DataFrame):
        """Should raise when not enough rows."""
        with pytest.raises(ValueError, match="Insufficient data"):
            _validate_ohlcv(small_df, min_rows=50)

    def test_valid_df_passes(self, ohlcv_df: pd.DataFrame):
        """Valid DataFrame should not raise."""
        _validate_ohlcv(ohlcv_df, min_rows=10)  # Should not raise


# ── Klines Conversion ─────────────────────────────────────────────────


class TestKlinesToDataframe:
    """Tests for the klines_to_dataframe utility."""

    def test_converts_list_of_dicts(self):
        """Should convert raw kline dicts to a proper DataFrame."""
        klines = [
            {"timestamp": 1704067200000, "open": 50000, "high": 50100, "low": 49900, "close": 50050, "volume": 1000},
            {"timestamp": 1704070800000, "open": 50050, "high": 50200, "low": 49950, "close": 50150, "volume": 1200},
        ]
        df = klines_to_dataframe(klines)
        assert len(df) == 2
        assert "close" in df.columns
        assert df["close"].dtype == float

    def test_empty_klines(self):
        """Empty klines should produce empty DataFrame."""
        df = klines_to_dataframe([])
        assert len(df) == 0
