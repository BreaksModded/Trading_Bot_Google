"""Technical indicators used by the strategy and backtesting engines."""

from __future__ import annotations

import pandas as pd


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Return exponential moving average."""
    if period <= 0:
        raise ValueError("EMA period must be positive.")
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average True Range using Wilder smoothing."""
    if period <= 0:
        raise ValueError("ATR period must be positive.")
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr_components = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX with a Wilder style smoothing approach."""
    if period <= 0:
        raise ValueError("ADX period must be positive.")

    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = low.diff() * -1

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = close.shift(1)
    tr_components = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    atr = true_range.ewm(alpha=1 / period, adjust=False).mean().replace(0, pd.NA)

    plus_di = (100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr).fillna(0.0)
    minus_di = (100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr).fillna(0.0)
    denominator = (plus_di + minus_di).replace(0, pd.NA)
    dx = (100 * (plus_di - minus_di).abs() / denominator).fillna(0.0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx


def compute_volume_ratio(series: pd.Series, period: int = 20) -> pd.Series:
    """Return current volume / SMA(volume, period) ratio."""
    if period <= 0:
        raise ValueError("Volume ratio period must be positive.")
    sma_vol = series.rolling(window=period, min_periods=1).mean().replace(0, pd.NA)
    return (series / sma_vol).fillna(0.0)


def enrich_indicators(
    df: pd.DataFrame,
    *,
    ema_fast: int,
    ema_slow: int,
    atr_period: int = 14,
    adx_period: int = 14,
    volume_ratio_period: int = 20,
) -> pd.DataFrame:
    """Return a copy with EMA, ATR, ADX, and volume_ratio columns appended."""
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing))}")

    out = df.copy()
    out["ema_fast"] = compute_ema(out["close"], ema_fast)
    out["ema_slow"] = compute_ema(out["close"], ema_slow)
    out["atr"] = compute_atr(out, period=atr_period)
    out["adx"] = compute_adx(out, period=adx_period)
    out["atr_pct"] = out["atr"] / out["close"]
    out["volume_ratio"] = compute_volume_ratio(out["volume"], period=volume_ratio_period)
    return out
