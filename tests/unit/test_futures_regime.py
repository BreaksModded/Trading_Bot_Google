"""Tests for the futures regime classifier and Chandelier Exit (Phase A)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from core.indicators import compute_chandelier_exit
from core.regime import MarketRegime, classify_futures_regime


# ── Regime classifier ─────────────────────────────────────────────────


def test_strong_adx_up_is_trending_up():
    r = classify_futures_regime(adx=30, ema_fast=110, ema_slow=100)
    assert r == MarketRegime.TRENDING_UP


def test_strong_adx_down_is_trending_down():
    r = classify_futures_regime(adx=30, ema_fast=90, ema_slow=100)
    assert r == MarketRegime.TRENDING_DOWN


def test_low_adx_is_ranging():
    r = classify_futures_regime(adx=15, ema_fast=110, ema_slow=100)
    assert r == MarketRegime.RANGING


def test_transitional_zone_stands_aside():
    # ADX between range (20) and trend (25) -> transitional regardless of EMA.
    r = classify_futures_regime(adx=22, ema_fast=110, ema_slow=100)
    assert r == MarketRegime.TRANSITIONAL


def test_nan_inputs_are_transitional():
    assert classify_futures_regime(adx=float("nan"), ema_fast=1, ema_slow=1) == MarketRegime.TRANSITIONAL
    assert classify_futures_regime(adx=None, ema_fast=1, ema_slow=1) == MarketRegime.TRANSITIONAL


def test_custom_thresholds_respected():
    # With trend threshold lowered to 18, adx=20 now counts as trending.
    r = classify_futures_regime(adx=20, ema_fast=110, ema_slow=100, adx_trend=18, adx_range=15)
    assert r == MarketRegime.TRENDING_UP


# ── Chandelier Exit ───────────────────────────────────────────────────


def _trend_df(n: int = 40, *, up: bool = True) -> pd.DataFrame:
    base = [100 + (i if up else -i) for i in range(n)]
    return pd.DataFrame({
        "open": base, "high": [b + 1 for b in base],
        "low": [b - 1 for b in base], "close": base, "volume": [10] * n,
    })


def _rising_df(n: int = 40) -> pd.DataFrame:
    return _trend_df(n, up=True)


def test_chandelier_long_trails_below_in_uptrend():
    # The long's trailing stop sits below price so a long is allowed to run.
    df = _trend_df(up=True)
    ce = compute_chandelier_exit(df, period=22, atr_mult=3.0)
    assert ce["chandelier_long"].iloc[-1] < df["close"].iloc[-1]
    assert not math.isnan(ce["chandelier_long"].iloc[-1])


def test_chandelier_short_trails_above_in_downtrend():
    # The short's trailing stop sits above price so a short is allowed to run.
    df = _trend_df(up=False)
    ce = compute_chandelier_exit(df, period=22, atr_mult=3.0)
    assert ce["chandelier_short"].iloc[-1] > df["close"].iloc[-1]


def test_chandelier_multiplier_widens_stop():
    df = _rising_df()
    tight = compute_chandelier_exit(df, period=22, atr_mult=2.0)["chandelier_long"].iloc[-1]
    wide = compute_chandelier_exit(df, period=22, atr_mult=4.0)["chandelier_long"].iloc[-1]
    assert wide < tight  # bigger multiple -> stop further from price (lower for a long)
