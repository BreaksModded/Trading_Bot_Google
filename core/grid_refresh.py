"""Pure, stateless module for grid staleness detection."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class RefreshConfig:
    """All tuneable parameters in one place."""
    price_distance_pct: float = 0.040        # 4.0% base threshold
    atr_multiplier: float = 2.5              # adaptive: max(base, N*ATR%)
    max_grid_age_hours: float = 6.0          # TTL for grid without fills
    cooldown_minutes: float = 90.0           # per-symbol cooldown after refresh
    stale_confirm_cycles: int = 2            # consecutive stale cycles before action
    max_refreshes_per_day: int = 4           # hard cap per symbol per calendar day

@dataclass  
class RefreshState:
    """Mutable per-symbol state — store in bot's symbol dict."""
    stale_cycle_count: int = 0
    last_refresh_at: Optional[datetime] = None
    refresh_count_today: int = 0
    refresh_count_date: Optional[str] = None  # 'YYYY-MM-DD'
    refresh_in_progress: bool = False
    refresh_fail_count: int = 0

@dataclass
class StalenessResult:
    is_stale: bool
    reason: str                              # human-readable for logging
    price_deviation_pct: float
    grid_age_hours: float
    threshold_used_pct: float

def compute_effective_threshold(
    base_pct: float,
    atr_multiplier: float,
    atr_pct: Optional[float],          # ATR expressed as % of price, or None
) -> float:
    """Return the higher of base threshold or ATR-adaptive threshold."""
    if atr_pct and atr_pct > 0:
        adaptive = atr_multiplier * atr_pct
        return max(base_pct, adaptive)
    return base_pct

def is_grid_stale(
    current_price: float,
    anchor_price: float,
    grid_created_at: datetime,
    atr_pct: Optional[float],
    cfg: RefreshConfig,
) -> StalenessResult:
    """
    Core detection logic. Returns StalenessResult.
    Grid is stale only when BOTH conditions are true (AND logic):
      1. Price has deviated >= threshold from anchor
      2. Grid age >= max_grid_age_hours
    This AND gate prevents reacting to short-lived spikes.
    """
    now = datetime.now(timezone.utc)
    grid_age_hours = (now - grid_created_at).total_seconds() / 3600.0
    
    if anchor_price <= 0:
        price_deviation_pct = 0.0
    else:
        price_deviation_pct = abs(current_price - anchor_price) / anchor_price
        
    threshold_used_pct = compute_effective_threshold(
        cfg.price_distance_pct, cfg.atr_multiplier, atr_pct
    )
    
    is_time_stale = grid_age_hours >= cfg.max_grid_age_hours
    is_price_stale = price_deviation_pct >= threshold_used_pct
    
    is_stale = is_time_stale and is_price_stale
    
    if is_stale:
        reason = f"Stale: age {grid_age_hours:.1f}h >= {cfg.max_grid_age_hours}h AND dev {price_deviation_pct:.2%} >= {threshold_used_pct:.2%}"
        logger.debug(reason)
    else:
        reason = "Active"
        
    return StalenessResult(
        is_stale=is_stale,
        reason=reason,
        price_deviation_pct=price_deviation_pct,
        grid_age_hours=grid_age_hours,
        threshold_used_pct=threshold_used_pct
    )

def should_refresh(
    state: RefreshState,
    stale_result: StalenessResult,
    cfg: RefreshConfig,
    now: Optional[datetime] = None,
) -> tuple[bool, str]:
    """
    Apply cooldown, daily cap, in-progress guard, and consecutive-cycle 
    confirmation on top of raw staleness. Returns (go, reason).
    """
    if now is None:
        now = datetime.now(timezone.utc)
        
    today_str = now.strftime('%Y-%m-%d')
    if state.refresh_count_date != today_str:
        state.refresh_count_today = 0
        state.refresh_count_date = today_str

    if state.refresh_in_progress:
        return False, "refresh_in_progress"
        
    if state.refresh_count_today >= cfg.max_refreshes_per_day:
        return False, f"daily_cap_reached ({cfg.max_refreshes_per_day})"
        
    if state.last_refresh_at:
        cooldown_elapsed = (now - state.last_refresh_at).total_seconds() / 60.0
        if cooldown_elapsed < cfg.cooldown_minutes:
            return False, f"in_cooldown ({cooldown_elapsed:.1f}m < {cfg.cooldown_minutes}m)"
            
    if not stale_result.is_stale:
        state.stale_cycle_count = 0
        return False, "not_stale"
        
    state.stale_cycle_count += 1
    if state.stale_cycle_count < cfg.stale_confirm_cycles:
        return False, f"confirming_stale ({state.stale_cycle_count}/{cfg.stale_confirm_cycles})"
        
    return True, stale_result.reason
