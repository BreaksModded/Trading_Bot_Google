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


def classify_futures_regime(
    adx: Optional[float],
    ema_fast: Optional[float],
    ema_slow: Optional[float],
    *,
    adx_trend: float = 25.0,
    adx_range: float = 20.0,
    current: Optional[MarketRegime] = None,
    hysteresis: float = 3.0,
) -> MarketRegime:
    """Classify regime for the futures bot using ADX (strength) + EMA (direction).

    - ADX >= adx_trend  -> TRENDING_UP / TRENDING_DOWN by EMA direction
    - ADX <  adx_range  -> RANGING (grid harvests chop)
    - in between          -> TRANSITIONAL (stand aside; no new entries)

    Hysteresis (audit H4): once in a regime, HOLD it through a buffer band so the bot
    does not flip-flop when ADX hovers right on a threshold (which churned orders). Pass
    the previous regime as ``current``: a trend holds until ADX < adx_trend - hysteresis,
    a range holds until ADX > adx_range + hysteresis. ``current`` may be a MarketRegime or
    its string value (StrEnum compares equal to the string).
    """
    if adx is None or ema_fast is None or ema_slow is None:
        return MarketRegime.TRANSITIONAL
    try:
        if math.isnan(adx) or math.isnan(ema_fast) or math.isnan(ema_slow):
            return MarketRegime.TRANSITIONAL
    except TypeError:
        return MarketRegime.TRANSITIONAL

    trend_now = MarketRegime.TRENDING_UP if ema_fast > ema_slow else MarketRegime.TRENDING_DOWN
    # Sticky: hold the current regime through the buffer band unless ADX clearly leaves it.
    if current in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN) and adx >= adx_trend - hysteresis:
        return trend_now
    if current == MarketRegime.RANGING and adx <= adx_range + hysteresis:
        return MarketRegime.RANGING
    # Entering a regime: the hard thresholds.
    if adx >= adx_trend:
        return trend_now
    if adx < adx_range:
        return MarketRegime.RANGING
    return MarketRegime.TRANSITIONAL
