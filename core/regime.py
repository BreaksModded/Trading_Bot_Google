"""Market regime classifier for grid trading decisions.

Classifies the current market into one of four regimes using ADX + RSI:
- RANGING: Low directional movement, ideal for grid trading
- TRENDING_UP: Strong upward trend, grid with wider spacing
- TRENDING_DOWN: Strong downward trend, do NOT place new grids
- TRANSITIONAL: Ambiguous conditions, conservative grid (fewer levels)
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Optional

from loguru import logger


class MarketRegime(StrEnum):
    RANGING = "ranging"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    TRANSITIONAL = "transitional"


def classify_regime(
    adx: Optional[float],
    rsi: Optional[float],
    *,
    adx_ranging: float = 20.0,
    adx_trending: float = 30.0,
    rsi_upper: float = 65.0,
    rsi_lower: float = 35.0,
) -> MarketRegime:
    """Classify market regime from ADX + RSI values.

    Handles None/NaN gracefully with TRANSITIONAL fallback.
    """
    # Guard against invalid inputs
    if adx is None or rsi is None:
        return MarketRegime.TRANSITIONAL
    try:
        if math.isnan(adx) or math.isnan(rsi):
            return MarketRegime.TRANSITIONAL
    except TypeError:
        return MarketRegime.TRANSITIONAL

    if adx < adx_ranging and rsi_lower < rsi < rsi_upper:
        return MarketRegime.RANGING

    if adx > adx_trending and rsi > rsi_upper:
        return MarketRegime.TRENDING_UP

    if adx > adx_trending and rsi < rsi_lower:
        return MarketRegime.TRENDING_DOWN

    return MarketRegime.TRANSITIONAL


def classify_futures_regime(
    adx: Optional[float],
    ema_fast: Optional[float],
    ema_slow: Optional[float],
    *,
    adx_trend: float = 25.0,
    adx_range: float = 20.0,
) -> MarketRegime:
    """Classify regime for the futures bot using ADX (strength) + EMA (direction).

    - ADX >= adx_trend  -> TRENDING_UP / TRENDING_DOWN by EMA direction
    - ADX <  adx_range  -> RANGING (grid harvests chop)
    - in between          -> TRANSITIONAL (stand aside; no new entries)

    The transitional zone is deliberate: entering on weak/ambiguous strength is
    where trend systems get whipsawed, so the bot stays flat there.
    """
    if adx is None or ema_fast is None or ema_slow is None:
        return MarketRegime.TRANSITIONAL
    try:
        if math.isnan(adx) or math.isnan(ema_fast) or math.isnan(ema_slow):
            return MarketRegime.TRANSITIONAL
    except TypeError:
        return MarketRegime.TRANSITIONAL

    if adx >= adx_trend:
        return MarketRegime.TRENDING_UP if ema_fast > ema_slow else MarketRegime.TRENDING_DOWN
    if adx < adx_range:
        return MarketRegime.RANGING
    return MarketRegime.TRANSITIONAL


def get_grid_params_for_regime(
    regime: MarketRegime,
    base_spacing: float,
    base_levels: int,
) -> tuple[float, int, bool]:
    """Return (spacing_adjusted, levels_adjusted, allow_placement) for the given regime.

    - RANGING: full grid, normal spacing
    - TRENDING_UP: wider spacing (+40%), one fewer level, still allowed
    - TRENDING_DOWN: blocked — no new grids
    - TRANSITIONAL: fewer levels (-2), normal spacing, still allowed
    """
    if regime == MarketRegime.RANGING:
        return base_spacing, base_levels, True

    if regime == MarketRegime.TRENDING_UP:
        return base_spacing * 1.4, max(3, base_levels - 1), True

    if regime == MarketRegime.TRENDING_DOWN:
        return base_spacing, base_levels, False

    # TRANSITIONAL
    return base_spacing, max(3, base_levels - 2), True
