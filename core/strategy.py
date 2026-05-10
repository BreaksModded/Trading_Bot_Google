"""Dynamic grid strategy with ATR spacing and ADX/EMA filters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from core.indicators import enrich_indicators
from core.regime import MarketRegime, classify_regime, get_grid_params_for_regime
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
    # Phase G: Asymmetric Grid Bias
    enable_asymmetric_grid: bool = False
    asymmetric_bearish_buy_factor: float = 1.35
    asymmetric_bearish_sell_factor: float = 0.70
    asymmetric_bullish_buy_factor: float = 0.80
    asymmetric_bullish_sell_factor: float = 1.25
    asymmetric_min_profit_multiple: float = 1.15
    # C.2 DCA: each deeper BUY level has qty × (1 + dca_qty_increment × (idx-1))
    dca_qty_increment: float = 0.0
    # C.3 Trend-rider: when regime=TRENDING_UP and ADX>threshold, replace
    # symmetric grid with a tight BUY-only ladder
    enable_trend_rider: bool = False
    trend_rider_adx_threshold: float = 50.0
    trend_rider_buy_spacing_pct: float = 0.004
    trend_rider_levels: int = 3


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
    reason: str = ""
    close_history: list[float] | None = None
    ema_fast_val: float = 0.0
    ema_slow_val: float = 0.0
    buy_spacing_pct: float = 0.0
    sell_spacing_pct: float = 0.0
    rsi_value: float = 50.0
    regime: str = "transitional"

    def scale(self, scale_factor: float) -> StrategySignal:
        """Return a new signal instance with proportionally scaled quantities and notional."""
        from copy import deepcopy
        levels_copy = [deepcopy(lvl) for lvl in self.levels]
        for lvl in levels_copy:
            lvl.qty = lvl.qty * scale_factor
        return StrategySignal(
            generated_at=self.generated_at,
            current_price=self.current_price,
            spacing_pct=self.spacing_pct,
            trend_bias=self.trend_bias,
            adx_value=self.adx_value,
            atr_pct=self.atr_pct,
            volume_ratio=self.volume_ratio,
            pause_new_grid=self.pause_new_grid,
            target_notional=self.target_notional * scale_factor,
            levels=levels_copy,
            reason=self.reason,
            close_history=self.close_history,
            ema_fast_val=self.ema_fast_val,
            ema_slow_val=self.ema_slow_val,
            buy_spacing_pct=self.buy_spacing_pct,
            sell_spacing_pct=self.sell_spacing_pct,
            rsi_value=self.rsi_value,
            regime=self.regime,
        )

    @property
    def level_count(self) -> int:
        return len(self.levels)
    
    @property
    def grid_spread_pct(self) -> float:
        """Returns the percentage spread between lowest and highest limit orders."""
        if len(self.levels) < 2:
            return 0.0
        prices = [lvl.price for lvl in self.levels]
        return (max(prices) - min(prices)) / self.current_price


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

    def compute_asymmetric_spacing(
        self,
        base_spacing_pct: float,
        trend_bias: TrendBias,
        adx_value: float,
    ) -> tuple[float, float]:
        """
        Applies asymmetric scaling to the base ATR spacing based on trend.
        
        Returns: (buy_spacing_pct, sell_spacing_pct)
        """
        if not self.config.enable_asymmetric_grid:
            return base_spacing_pct, base_spacing_pct
            
        if trend_bias == TrendBias.NEUTRAL:
            return base_spacing_pct, base_spacing_pct
            
        # Scale strength linearly from ADX 15 (0%) to ADX 40 (100%)
        adx_strength = max(0.0, min(1.0, (adx_value - 15.0) / 25.0))
        if adx_strength == 0.0:
            return base_spacing_pct, base_spacing_pct
            
        if trend_bias == TrendBias.SHORT:
            cf_buy = self.config.asymmetric_bearish_buy_factor
            cf_sell = self.config.asymmetric_bearish_sell_factor
        else: # TrendBias.LONG
            cf_buy = self.config.asymmetric_bullish_buy_factor
            cf_sell = self.config.asymmetric_bullish_sell_factor
            
        buy_factor = 1.0 + (cf_buy - 1.0) * adx_strength
        sell_factor = 1.0 + (cf_sell - 1.0) * adx_strength
        
        buy_spacing = base_spacing_pct * buy_factor
        sell_spacing = base_spacing_pct * sell_factor
        
        # Minimum profitability guard (Taker fee isolated to 0.1% hardcoded for safety)
        taker_fee = 0.001
        min_spacing_allowed = (2 * taker_fee) * self.config.asymmetric_min_profit_multiple
        
        if sell_spacing < min_spacing_allowed:
            from loguru import logger
            logger.debug(
                "[AsymmetricGuard] {} Sell spacing {:.3f}% clamped to minimum {:.3f}%",
                self.config.symbol,
                sell_spacing * 100,
                min_spacing_allowed * 100
            )
            sell_spacing = min_spacing_allowed
            
        from loguru import logger
        logger.info(
            "[AsymmetricGrid] {} | Bias: {} | ADX: {:.1f} | "
            "Buy spacing: {:.2f}% (base: {:.2f}% × {:.2f}) | "
            "Sell spacing: {:.2f}% (base: {:.2f}% × {:.2f}) | "
            "ADX strength: {:.2f}",
            self.config.symbol, trend_bias.name, adx_value,
            buy_spacing * 100, base_spacing_pct * 100, buy_factor,
            sell_spacing * 100, base_spacing_pct * 100, sell_factor,
            adx_strength
        )
            
        return buy_spacing, sell_spacing

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
        self, current_price: float, buy_spacing_pct: float, sell_spacing_pct: float, trend: TrendBias, target_size: float
    ) -> list[GridLevel]:
        """Create limit orders around spot depending on trend filter.

        C.2: BUY level qty scales by (1 + dca_qty_increment × (idx-1)) so deeper
        levels carry more weight, accelerating avg_cost reduction on adverse moves.
        SELL levels keep flat qty.
        """
        levels: list[GridLevel] = []
        base_qty = target_size / current_price
        dca_inc = max(0.0, self.config.dca_qty_increment)

        for idx in range(1, self.config.num_levels + 1):
            lower_price = current_price * (1 - buy_spacing_pct * idx)
            upper_price = current_price * (1 + sell_spacing_pct * idx)
            buy_qty = base_qty * (1.0 + dca_inc * (idx - 1))

            if trend != TrendBias.SHORT:
                levels.append(
                    GridLevel(
                        level_id=f"buy-{idx}-{int(lower_price)}",
                        price=lower_price,
                        side=OrderSide.BUY,
                        qty=buy_qty,
                    )
                )

            if trend != TrendBias.LONG:
                levels.append(
                    GridLevel(
                        level_id=f"sell-{idx}-{int(upper_price)}",
                        price=upper_price,
                        side=OrderSide.SELL,
                        qty=base_qty,
                    )
                )

        return levels

    def _build_trend_rider_levels(
        self, current_price: float, target_size: float
    ) -> list[GridLevel]:
        """C.3: BUY-only tight ladder for strong-uptrend regimes.

        Places N rungs below the current price at trend_rider_buy_spacing_pct
        intervals. SELLs are not placed at grid time — the inverse-SELL flow
        in handle_fill() places exits after each BUY fill, and trailing TP
        (when enabled) ratchets them upward as the trend continues.
        """
        levels: list[GridLevel] = []
        base_qty = target_size / current_price
        rungs = max(2, int(self.config.trend_rider_levels))
        spacing = max(0.001, float(self.config.trend_rider_buy_spacing_pct))
        dca_inc = max(0.0, self.config.dca_qty_increment)
        for idx in range(1, rungs + 1):
            price = current_price * (1 - spacing * idx)
            qty = base_qty * (1.0 + dca_inc * (idx - 1))
            levels.append(
                GridLevel(
                    level_id=f"trider-{idx}-{int(price)}",
                    price=price,
                    side=OrderSide.BUY,
                    qty=qty,
                )
            )
        return levels

    def compute_signal(self, market_df: pd.DataFrame, *, grid_settings=None) -> StrategySignal:
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
        ema_fast_val = float(latest["ema_fast"])
        ema_slow_val = float(latest["ema_slow"])
        rsi_value = float(latest.get("rsi", 50.0))
        volume_ratio = float(latest["volume_ratio"])
        trend = self.determine_trend(ema_fast_val, ema_slow_val)
        base_spacing = self.compute_spacing_pct(atr_pct)
        buy_spacing, sell_spacing = self.compute_asymmetric_spacing(base_spacing, trend, adx_value)

        # ADX filter is directional: only block when high ADX coincides with a SHORT
        # trend (falling-knife risk). High ADX with LONG/NEUTRAL bias means strong
        # upside momentum — exactly what the asymmetric grid is designed to capture.
        pause_grid = (
            self.config.enable_adx_filter
            and adx_value > self.config.adx_threshold
            and trend == TrendBias.SHORT
        )
        reason = "grid_active"

        # Step 5: Regime classification (ADX + RSI)
        regime_params = {}
        if grid_settings is not None:
            regime_params = {
                "adx_ranging": grid_settings.regime_adx_ranging,
                "adx_trending": grid_settings.regime_adx_trending,
                "rsi_upper": grid_settings.regime_rsi_upper,
                "rsi_lower": grid_settings.regime_rsi_lower,
            }
        regime = classify_regime(adx_value, rsi_value, **regime_params)

        # Step 7: Dynamic num_levels based on ATR
        effective_levels = self.config.num_levels
        if grid_settings is not None and grid_settings.dynamic_levels_enabled:
            if atr_pct < grid_settings.levels_low_vol_atr:
                effective_levels = max(3, self.config.num_levels - 2)
            elif atr_pct > grid_settings.levels_high_vol_atr:
                effective_levels = min(grid_settings.levels_max, self.config.num_levels + 2)

        # MEJORA-006: Apply regime-specific spacing and level adjustments
        adj_spacing, effective_levels, _ = get_grid_params_for_regime(
            regime, base_spacing, effective_levels
        )
        if adj_spacing != base_spacing:
            buy_spacing, sell_spacing = self.compute_asymmetric_spacing(adj_spacing, trend, adx_value)

        # APS-2: Calculate volatile-adjusted sizing (volatility targeting)
        base_size = self.config.order_size_usdt
        target_size = (base_size * self.config.sizing_baseline_atr) / atr_pct if atr_pct > 0 else base_size
        target_size = min(target_size, self.config.max_order_size_usdt)

        # C.3: Trend-rider mode short-circuits the symmetric grid when the regime
        # is decisively bullish. We build a tight BUY-only ladder and let the
        # inverse-SELL + trailing TP flow handle exits.
        trend_rider_active = (
            self.config.enable_trend_rider
            and regime == MarketRegime.TRENDING_UP
            and adx_value > self.config.trend_rider_adx_threshold
        )

        if trend_rider_active:
            from loguru import logger
            logger.info(
                "[TrendRider] {} | regime={} ADX={:.1f} > {} | building {} BUY ladder at {:.2%} spacing",
                self.config.symbol, regime.value, adx_value, self.config.trend_rider_adx_threshold,
                self.config.trend_rider_levels, self.config.trend_rider_buy_spacing_pct,
            )
            levels = self._build_trend_rider_levels(current_price=current_price, target_size=target_size)
        else:
            # Override num_levels temporarily for level building; use try/finally so
            # the original value is always restored even if _build_levels raises.
            original_levels = self.config.num_levels
            self.config.num_levels = effective_levels
            try:
                levels = self._build_levels(
                    current_price=current_price, buy_spacing_pct=buy_spacing, sell_spacing_pct=sell_spacing, trend=trend, target_size=target_size
                )
            finally:
                self.config.num_levels = original_levels

        if pause_grid:
            reason = f"adx_above_threshold:{adx_value:.2f}"

        return StrategySignal(
            generated_at=datetime.now(UTC),
            current_price=current_price,
            spacing_pct=base_spacing,
            trend_bias=trend,
            adx_value=adx_value,
            atr_pct=atr_pct,
            volume_ratio=volume_ratio,
            pause_new_grid=pause_grid,
            target_notional=target_size,
            levels=levels,
            reason=reason,
            close_history=market_df["close"].tail(50).copy().tolist(),
            ema_fast_val=ema_fast_val,
            ema_slow_val=ema_slow_val,
            buy_spacing_pct=buy_spacing,
            sell_spacing_pct=sell_spacing,
            rsi_value=rsi_value,
            regime=regime.value,
        )

    def estimate_required_capital(self, signal: StrategySignal) -> float:
        """Estimate USDT required for BUY side levels in the signal.

        Sums actual qty × price per BUY level (handles DCA non-uniform sizing
        and trend-rider ladders where qty/price varies per rung).
        """
        return float(sum(lvl.qty * lvl.price for lvl in signal.levels if lvl.side == OrderSide.BUY))

