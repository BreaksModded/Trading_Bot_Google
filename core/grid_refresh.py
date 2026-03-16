"""Pure, stateless module for grid staleness detection and Phase H safety gates."""

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
    last_refresh_price: Optional[float] = None
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


@dataclass
class RefreshGateResult:
    """Result of a single safety gate evaluation — used for audit logging."""

    gate_name: str       # "G1_ADX", "G2_INVENTORY", etc.
    passed: bool
    reason: str
    value: float         # the measured value
    threshold: float     # the configured threshold


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
    *,
    trigger_mode: str = "AND",
    enable_price_trigger: bool = True,
    enable_time_trigger: bool = True,
) -> StalenessResult:
    """
    Core detection logic. Returns StalenessResult.

    trigger_mode="AND" (legacy): stale only when BOTH conditions fire.
    trigger_mode="OR" (Phase H): stale when EITHER condition fires.

    Individual triggers can be disabled independently.
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

    is_price_stale = enable_price_trigger and (price_deviation_pct >= threshold_used_pct)
    is_time_stale = enable_time_trigger and (grid_age_hours >= cfg.max_grid_age_hours)

    if trigger_mode == "OR":
        is_stale = is_price_stale or is_time_stale
    else:
        # AND (legacy default)
        is_stale = is_price_stale and is_time_stale

    if is_stale:
        parts = []
        if is_price_stale:
            parts.append(f"dev {price_deviation_pct:.2%} >= {threshold_used_pct:.2%}")
        if is_time_stale:
            parts.append(f"age {grid_age_hours:.1f}h >= {cfg.max_grid_age_hours}h")
        trigger_word = "OR" if trigger_mode == "OR" else "AND"
        reason = f"Stale ({trigger_word}): {' | '.join(parts)}"
        logger.debug(reason)
    else:
        reason = "Active"

    return StalenessResult(
        is_stale=is_stale,
        reason=reason,
        price_deviation_pct=price_deviation_pct,
        grid_age_hours=grid_age_hours,
        threshold_used_pct=threshold_used_pct,
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


# ── Phase H: Safety Gate Evaluation ──────────────────────────────────


def evaluate_safety_gates(
    *,
    adx_value: float,
    trend_bias: str,
    inventory_ratio: float,
    open_buy_count: int,
    time_since_last_refresh_s: float,
    price_move_since_last_pct: float,
    adx_block_threshold: float = 35.0,
    max_inventory_ratio: float = 0.40,
    cooldown_seconds: int = 1800,
    min_move_pct: float = 0.02,
    skip_if_orders_above: int = 2,
) -> tuple[bool, list[RefreshGateResult]]:
    """
    Evaluate Phase H safety gates for grid refresh.

    FIX-C: Reduced from 5 gates to 3.  G4 (min_move) and G5 (order_count)
    were removed:
    - G4 (min_move) was redundant with StalenessResult which already evaluates
      price deviation. It blocked Time-Refresh in legitimate lateral markets
      where recalibrating ATR spacing is the whole point of the age trigger.
    - G5 (order_count) caused deadlocks: old buy orders at stale prices
      counted as "grid still in range" preventing their own cancellation.
      StalenessResult already evaluates order age and price deviation.

    Remaining gates:
    - G1 (ADX trend): Do not refresh into a strong downtrend (falling knife)
    - G2 (inventory cap): Do not over-allocate capital to one pair
    - G3 (cooldown): Prevent API spam from rapid refresh cycles

    Returns:
        (all_passed, list of gate results)
    """
    results: list[RefreshGateResult] = []

    # G3 — Cooldown (cheapest check first)
    g3_passed = time_since_last_refresh_s >= cooldown_seconds
    results.append(RefreshGateResult(
        gate_name="G3_COOLDOWN",
        passed=g3_passed,
        reason=(
            f"elapsed={time_since_last_refresh_s:.0f}s >= cooldown={cooldown_seconds}s"
            if g3_passed
            else f"BLOCKED: elapsed={time_since_last_refresh_s:.0f}s < cooldown={cooldown_seconds}s"
        ),
        value=time_since_last_refresh_s,
        threshold=float(cooldown_seconds),
    ))

    # G1 — ADX Trend Filter
    bias_lower = str(trend_bias).lower()
    is_downtrend = bias_lower in ("short", "bearish")
    adx_above = adx_value > adx_block_threshold
    g1_blocked = is_downtrend and adx_above
    g1_passed = not g1_blocked
    results.append(RefreshGateResult(
        gate_name="G1_ADX_TREND",
        passed=g1_passed,
        reason=(
            f"OK: ADX={adx_value:.1f} trend={trend_bias}"
            if g1_passed
            else f"BLOCKED: ADX={adx_value:.1f} > {adx_block_threshold:.1f} in {trend_bias} trend (falling knife)"
        ),
        value=adx_value,
        threshold=adx_block_threshold,
    ))

    # G2 — Inventory Cap
    g2_passed = inventory_ratio <= max_inventory_ratio
    results.append(RefreshGateResult(
        gate_name="G2_INVENTORY",
        passed=g2_passed,
        reason=(
            f"ratio={inventory_ratio:.2%} <= cap={max_inventory_ratio:.2%}"
            if g2_passed
            else f"BLOCKED: ratio={inventory_ratio:.2%} > cap={max_inventory_ratio:.2%} (capital trapped as inventory)"
        ),
        value=inventory_ratio,
        threshold=max_inventory_ratio,
    ))

    all_passed = all(g.passed for g in results)
    return all_passed, results
