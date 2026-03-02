"""Phase G: Dynamic Profit Reinvestment Engine"""

import time
from loguru import logger
from config.settings import GridSettings


class ReinvestmentEngine:
    """
    Manages the dynamic trading capital baseline.
    Recalculates periodically based on realized equity growth,
    with protection against oversizing and drawdown spirals.
    """
    
    def __init__(self, initial_baseline: float, config: GridSettings):
        self._initial_baseline = initial_baseline
        self._current_baseline = initial_baseline
        self._last_recalc_time = time.monotonic()
        self._config = config
        self._floor = initial_baseline * config.reinvestment_min_baseline_floor_pct

        if config.enable_profit_reinvestment:
            logger.info(
                "[Reinvestment] Engine started | Initial Base: {:.2f} | Floor: {:.2f} | Alloc: {:.0f}%",
                self._initial_baseline, self._floor, config.reinvestment_equity_allocation_pct * 100
            )

    def maybe_recalculate(self, current_free_equity: float, is_bot_stopped: bool = False) -> float:
        """
        Called on every main loop iteration.
        Only recalculates if the recalc interval has elapsed.
        Returns the current effective baseline (updated or unchanged).
        """
        if not self._config.enable_profit_reinvestment:
            return self._initial_baseline
            
        if is_bot_stopped:
            self._last_recalc_time = time.monotonic()
            return self._current_baseline

        now = time.monotonic()
        if now - self._last_recalc_time >= self._config.reinvestment_recalc_interval_seconds:
            self._current_baseline = self._compute_new_baseline(current_free_equity)
            self._last_recalc_time = now

        return self._current_baseline

    def get_current_baseline(self) -> float:
        """Returns the current effective trading capital baseline."""
        if not self._config.enable_profit_reinvestment:
            return self._initial_baseline
        return self._current_baseline

    def reinitialize_after_stop(self, current_free_equity: float) -> None:
        """Accept the loss and re-initialize completely."""
        if not self._config.enable_profit_reinvestment:
            return
        logger.warning(
            "[Reinvestment] Re-initializing after stop state. Accepting new equity anchor: {:.2f}", 
            current_free_equity
        )
        self._initial_baseline = current_free_equity
        self._current_baseline = current_free_equity * self._config.reinvestment_equity_allocation_pct
        self._floor = current_free_equity * self._config.reinvestment_min_baseline_floor_pct
        self._last_recalc_time = time.monotonic()

    def _compute_new_baseline(self, current_free_equity: float) -> float:
        """
        Core recalculation logic.
        Applies allocation percentage, growth cap, and floor protection.
        """
        target = current_free_equity * self._config.reinvestment_equity_allocation_pct
        max_allowed = self._current_baseline * (1.0 + self._config.reinvestment_max_step_growth_pct)
        
        capped_target = min(target, max_allowed)
        
        if capped_target < self._floor:
            logger.warning(
                "[Reinvestment] Equity dropped below floor! Capped target {:.2f} clamped to floor {:.2f}", 
                capped_target, self._floor
            )
            floored_target = self._floor
        else:
            floored_target = max(capped_target, self._floor)

        new_baseline = floored_target
        
        # Calculate growth for logging
        from_initial = ((new_baseline / self._initial_baseline) - 1.0) * 100 if self._initial_baseline > 0 else 0.0
        step_growth = ((new_baseline / self._current_baseline) - 1.0) * 100 if self._current_baseline > 0 else 0.0
        
        logger.info(
            "[Reinvestment] Baseline recalculated | "
            "Previous: {:.2f} | New: {:.2f} | "
            "Free equity: {:.2f} | Allocation: {:.0f}% | "
            "Growth this step: {:+.2f}% (capped from {:.2f}) | "
            "Floor: {:.2f} | Next recalc in: {}s",
            self._current_baseline, new_baseline,
            current_free_equity, self._config.reinvestment_equity_allocation_pct * 100,
            step_growth, target,
            self._floor, self._config.reinvestment_recalc_interval_seconds
        )
        
        return new_baseline
