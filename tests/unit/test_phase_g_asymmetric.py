import pytest
from core.strategy import GridStrategy, StrategyConfig
from data.models import TrendBias

@pytest.fixture
def strategy():
    c = StrategyConfig(
        symbol="BTCUSDC",
        num_levels=5,
        min_spacing_pct=0.005,
        atr_multiplier=1.5,
        order_size_usdt=25.0,
        adx_threshold=30.0,
        ema_fast=50,
        ema_slow=200,
        enable_asymmetric_grid=True,
        asymmetric_bearish_buy_factor=1.30,
        asymmetric_bearish_sell_factor=0.70,
        asymmetric_bullish_buy_factor=0.85,
        asymmetric_bullish_sell_factor=1.20,
        asymmetric_min_profit_multiple=1.15
    )
    return GridStrategy(c)

def test_asymmetric_disabled(strategy):
    """1. Feature completely disabled produces symmetric outputs."""
    strategy.config.enable_asymmetric_grid = False
    
    # Passing 1.0% base spacing
    buy, sell = strategy.compute_asymmetric_spacing(0.01, TrendBias.SHORT, 40.0)
    assert buy == 0.01
    assert sell == 0.01


def test_asymmetric_bearish_bias(strategy):
    """2. Bearish bias produces widened buys and tightened sells."""
    # Strong ADX (40.0) -> full interpolation (adx_strength = 1.0)
    buy, sell = strategy.compute_asymmetric_spacing(0.01, TrendBias.SHORT, 40.0)
    
    assert buy == 0.01 * 1.30
    assert sell == 0.01 * 0.70


def test_asymmetric_low_adx(strategy):
    """3. Low ADX reduces asymmetry toward nearest integer base."""
    # ADX below 15.0 -> adx_strength = 0.0
    buy, sell = strategy.compute_asymmetric_spacing(0.01, TrendBias.LONG, 12.0)
    
    assert buy == 0.01
    assert sell == 0.01
    
    # ADX = 27.5 -> adx_strength = 0.5
    buy2, sell2 = strategy.compute_asymmetric_spacing(0.01, TrendBias.SHORT, 27.5)
    # Factor buy = 1.0 + 0.3 * 0.5 = 1.15
    # Factor sell = 1.0 + (-0.3) * 0.5 = 0.85
    assert buy2 == pytest.approx(0.0115)
    assert sell2 == pytest.approx(0.0085)


def test_asymmetric_profitability_guard(strategy):
    """4. Extreme tight spacing triggers the floor profitability guard."""
    strategy.config.asymmetric_bearish_sell_factor = 0.01 # Absurdly low
    
    # Base = 0.01, ADX = 40 (full strength)
    buy, sell = strategy.compute_asymmetric_spacing(0.01, TrendBias.SHORT, 40.0)
    
    # Taker fee 0.001 * 2 * 1.15 = 0.0023
    assert sell == pytest.approx(0.0023)
    assert buy == 0.01 * 1.30


def test_asymmetric_neutral_bias(strategy):
    """5. Neutral bias forces symmetry regardless of ADX."""
    buy, sell = strategy.compute_asymmetric_spacing(0.015, TrendBias.NEUTRAL, 50.0)
    
    assert buy == 0.015
    assert sell == 0.015
