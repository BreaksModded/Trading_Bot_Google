"""
Tests for core/strategy.py

Tests grid level computation, order placement logic,
market condition filtering, and fill handling.
Uses mock exchange to avoid real API calls.
"""

from __future__ import annotations

import pandas as pd
import pytest

from core.strategy import GridStrategy, StrategyConfig, StrategySignal
from data.models import GridLevel, OrderSide, TrendBias
from config.settings import Settings

@pytest.fixture
def strategy() -> GridStrategy:
    """Create a GridStrategy with default dummy config."""
    config = StrategyConfig(
        symbol="BTCUSDT",
        num_levels=5,
        min_spacing_pct=0.006,
        atr_multiplier=1.5,
        order_size_usdt=25.0,
        adx_threshold=25.0,
        ema_fast=50,
        ema_slow=200,
    )
    return GridStrategy(config)


class TestStrategyConfig:
    def test_invalid_num_levels(self):
        with pytest.raises(ValueError):
            config = StrategyConfig(symbol="BTCUSDT", num_levels=0, min_spacing_pct=0.01, atr_multiplier=1.0, order_size_usdt=10, adx_threshold=25.0, ema_fast=50, ema_slow=200)
            GridStrategy(config)

    def test_valid_config(self, strategy: GridStrategy):
        assert strategy.config.num_levels == 5


class TestStrategyLogic:
    def test_compute_spacing(self, strategy: GridStrategy):
        # min is 0.006, atr_multiplier is 1.5
        assert strategy.compute_spacing_pct(0.003) == 0.006  # min bound
        assert strategy.compute_spacing_pct(0.01) == 0.015   # 0.01 * 1.5

    def test_determine_trend(self, strategy: GridStrategy):
        assert strategy.determine_trend(ema_fast_value=51000, ema_slow_value=50000) == TrendBias.LONG
        assert strategy.determine_trend(ema_fast_value=49000, ema_slow_value=50000) == TrendBias.SHORT
        assert strategy.determine_trend(ema_fast_value=50000, ema_slow_value=50000) == TrendBias.NEUTRAL

    def test_build_levels_neutral(self, strategy: GridStrategy):
        levels = strategy._build_levels(50000.0, 0.01, TrendBias.NEUTRAL, 25.0)
        assert len(levels) == 10  # 5 buy, 5 sell
        buys = [l for l in levels if l.side == OrderSide.BUY]
        sells = [l for l in levels if l.side == OrderSide.SELL]
        assert len(buys) == 5
        assert len(sells) == 5
        assert all(l.price < 50000 for l in buys)
        assert all(l.price > 50000 for l in sells)

    def test_build_levels_long(self, strategy: GridStrategy):
        levels = strategy._build_levels(50000.0, 0.01, TrendBias.LONG, 25.0)
        assert len(levels) == 5
        assert all(l.side == OrderSide.BUY for l in levels)

    def test_build_levels_short(self, strategy: GridStrategy):
        levels = strategy._build_levels(50000.0, 0.01, TrendBias.SHORT, 25.0)
        assert len(levels) == 5
        assert all(l.side == OrderSide.SELL for l in levels)

    def test_estimate_capital(self, strategy: GridStrategy):
        levels = strategy._build_levels(50000.0, 0.01, TrendBias.NEUTRAL, 25.0)
        signal = StrategySignal(
            generated_at=pd.Timestamp.utcnow(),
            current_price=50000.0,
            spacing_pct=0.01,
            trend_bias=TrendBias.NEUTRAL,
            adx_value=10.0,
            atr_pct=0.01,
            volume_ratio=1.0,
            pause_new_grid=False,
            target_notional=25.0,
            levels=levels,
            reason="test"
        )
        assert strategy.estimate_required_capital(signal) == 125.0  # 5 buys * 25.0
