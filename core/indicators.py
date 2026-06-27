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
    atr = true_range.ewm(alpha=1 / period, adjust=False).mean().replace(0, float('nan'))

    plus_di = (100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr).fillna(0.0)
    minus_di = (100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr).fillna(0.0)
    denominator = (plus_di + minus_di).replace(0, float('nan'))
    dx = (100 * (plus_di - minus_di).abs() / denominator).fillna(0.0)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx


def compute_volume_ratio(series: pd.Series, period: int = 20) -> pd.Series:
    """Return current volume / median(volume, period) ratio.

    Uses median instead of mean (SMA) to resist volume spikes.
    A single extreme-volume candle (e.g. during a trend event) would inflate
    the SMA for ~20 periods, making normal subsequent volume appear low and
    blocking grid placement.  Median is unaffected by a single outlier.
    """
    if period <= 0:
        raise ValueError("Volume ratio period must be positive.")
    median_vol = series.rolling(window=period, min_periods=1).median().replace(0, float('nan'))
    # FIX-B: fillna(1.0) instead of fillna(0.0).
    # 1.0 = volume equals median = neutral; prevents blocking grid placement
    # when exchange returns NaN due to timeout or empty candle data.
    # Real low-volume conditions produce ratio < 1.0 (not NaN), unaffected.
    return (series / median_vol).fillna(1.0)


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI using Wilder smoothing (consistent with ATR/ADX)."""
    if period <= 0:
        raise ValueError("RSI period must be positive.")
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float('nan'))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)  # Neutral when insufficient data


def compute_chandelier_exit(
    df: pd.DataFrame, *, period: int = 22, atr_mult: float = 3.0, atr_period: int | None = None,
) -> pd.DataFrame:
    """Chandelier Exit trailing-stop levels (Chuck LeBeau).

    long_stop  = highest_high(period) - ATR(atr_period) * atr_mult
    short_stop = lowest_low(period)   + ATR(atr_period) * atr_mult

    A long position trails its stop at ``long_stop`` (exit if close drops below);
    a short trails at ``short_stop`` (exit if close rises above). The ratchet
    (stop only moves in the favourable direction) is applied statefully by the
    trend module — this function returns the raw per-bar levels.

    Validated default for crypto trend-following: period=22, atr_mult≈2.5-3.0.
    """
    ap = atr_period if atr_period is not None else period
    atr = compute_atr(df, period=ap)
    highest_high = df["high"].rolling(window=period, min_periods=1).max()
    lowest_low = df["low"].rolling(window=period, min_periods=1).min()
    out = pd.DataFrame(index=df.index)
    out["chandelier_long"] = highest_high - atr * atr_mult
    out["chandelier_short"] = lowest_low + atr * atr_mult
    return out


def enrich_indicators(
    df: pd.DataFrame,
    *,
    ema_fast: int,
    ema_slow: int,
    atr_period: int = 14,
    adx_period: int = 14,
    volume_ratio_period: int = 20,
) -> pd.DataFrame:
    """Return a copy with EMA, ATR, ADX, RSI, and volume_ratio columns appended."""
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
    out["rsi"] = compute_rsi(out["close"], period=atr_period)
    return out

