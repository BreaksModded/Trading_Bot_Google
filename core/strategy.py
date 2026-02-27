"""Dynamic grid strategy with ATR spacing and ADX/EMA filters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from core.indicators import enrich_indicators
from data.models import GridLevel, OrderSide, TrendBias


@dataclass(slots=True)
class StrategyConfig:
    """Configurable strategy parameters."""

    symbol: str
    num_levels: int
    min_spacing_pct: float
    atr_multiplier: float
    order_size_usdt: float
    adx_threshold: float
    ema_fast: int
    ema_slow: int
    sizing_baseline_atr: float = 0.05
    max_order_size_usdt: float = 100.0
    enable_adx_filter: bool = True
    enable_ema_filter: bool = True
    min_volume_ratio: float = 1.0


@dataclass(slots=True)
class StrategySignal:
    """Output generated each loop from strategy analysis."""

    generated_at: datetime
    current_price: float
    spacing_pct: float
    trend_bias: TrendBias
    adx_value: float
    atr_pct: float
    volume_ratio: float
    pause_new_grid: bool
    target_notional: float
    levels: list[GridLevel]
    reason: str
    close_history: pd.Series | None = None


class GridStrategy:
    """Implements the dynamic grid strategy with volatility and trend filters."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.validate_config(config)

    @staticmethod
    def validate_config(config: StrategyConfig) -> None:
        """Validate static constraints for the strategy settings."""
        if config.num_levels <= 0:
            raise ValueError("num_levels must be positive.")
        if config.min_spacing_pct <= 0:
            raise ValueError("min_spacing_pct must be positive.")
        if config.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive.")
        if config.order_size_usdt <= 0:
            raise ValueError("order_size_usdt must be positive.")
        if config.ema_fast <= 0 or config.ema_slow <= 0:
            raise ValueError("EMA periods must be positive.")

    def compute_spacing_pct(self, atr_pct: float) -> float:
        """Compute current spacing using max(min_spacing, atr_multiplier * ATR%)."""
        return max(self.config.min_spacing_pct, self.config.atr_multiplier * atr_pct)

    def determine_trend(self, ema_fast_value: float, ema_slow_value: float) -> TrendBias:
        """Determine directional bias from EMA fast/slow relationship."""
        if not self.config.enable_ema_filter:
            return TrendBias.NEUTRAL
        if ema_fast_value > ema_slow_value:
            return TrendBias.LONG
        if ema_fast_value < ema_slow_value:
            return TrendBias.SHORT
        return TrendBias.NEUTRAL

    def _build_levels(
        self, current_price: float, spacing_pct: float, trend: TrendBias, target_size: float
    ) -> list[GridLevel]:
        """Create limit orders around spot depending on trend filter."""
        levels: list[GridLevel] = []
        qty = target_size / current_price

        for idx in range(1, self.config.num_levels + 1):
            lower_price = current_price * (1 - spacing_pct * idx)
            upper_price = current_price * (1 + spacing_pct * idx)

            if trend != TrendBias.SHORT:
                levels.append(
                    GridLevel(
                        level_id=f"buy-{idx}-{int(lower_price)}",
                        price=lower_price,
                        side=OrderSide.BUY,
                        qty=qty,
                    )
                )

            if trend != TrendBias.LONG:
                levels.append(
                    GridLevel(
                        level_id=f"sell-{idx}-{int(upper_price)}",
                        price=upper_price,
                        side=OrderSide.SELL,
                        qty=qty,
                    )
                )

        return levels

    def compute_signal(self, market_df: pd.DataFrame) -> StrategySignal:
        """Compute indicators and return actionable grid signal for the cycle."""
        if market_df.empty:
            raise ValueError("market_df cannot be empty.")

        enriched = enrich_indicators(
            market_df,
            ema_fast=self.config.ema_fast,
            ema_slow=self.config.ema_slow,
            atr_period=14,
            adx_period=14,
        )
        latest = enriched.iloc[-1]
        current_price = float(latest["close"])
        atr_pct = float(latest["atr_pct"])
        adx_value = float(latest["adx"])
        trend = self.determine_trend(float(latest["ema_fast"]), float(latest["ema_slow"]))
        spacing = self.compute_spacing_pct(atr_pct)

        pause_grid = self.config.enable_adx_filter and adx_value > self.config.adx_threshold
        reason = "grid_active"
        
        # APS-2: Calculate volatile-adjusted sizing (volatility targeting)
        base_size = self.config.order_size_usdt
        target_size = (base_size * self.config.sizing_baseline_atr) / atr_pct if atr_pct > 0 else base_size
        target_size = min(target_size, self.config.max_order_size_usdt)

        # Keep levels available even when ADX pauses new grids;
        # runtime may use controlled fallback paths. (matches PROYECTO2)
        levels = self._build_levels(
            current_price=current_price, spacing_pct=spacing, trend=trend, target_size=target_size
        )
        if pause_grid:
            reason = f"adx_above_threshold:{adx_value:.2f}"

        volume_ratio = float(latest["volume_ratio"])

        return StrategySignal(
            generated_at=datetime.now(UTC),
            current_price=current_price,
            spacing_pct=spacing,
            trend_bias=trend,
            adx_value=adx_value,
            atr_pct=atr_pct,
            volume_ratio=volume_ratio,
            pause_new_grid=pause_grid,
            target_notional=target_size,
            levels=levels,
            reason=reason,
            close_history=market_df["close"].tail(50).copy(),
        )

    def estimate_required_capital(self, signal: StrategySignal) -> float:
        """Estimate USDT required for BUY side levels in the signal."""
        buy_levels = [level for level in signal.levels if level.side == OrderSide.BUY]
        return float(len(buy_levels) * signal.target_notional)
