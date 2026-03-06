"""Phase I: Dynamic Capital-Proportional Order Sizing.

Pure function for computing effective order size based on available
capital percentage, with min/max enforcement and backward-compatible
fallback to fixed GRID_ORDER_SIZE_USDT.
"""

from loguru import logger


def compute_dynamic_order_size(
    available_capital_usdt: float,
    order_size_pct: float,
    reinvestment_multiplier: float,
    min_order_usdt: float,
    max_order_usdt: float,
    fallback_fixed_size: float,
    enabled: bool,
) -> float:
    """Compute the effective order size for a single grid level.

    When ``enabled=False``, returns ``fallback_fixed_size × reinvestment_multiplier``
    (identical to current behavior — zero change).

    When ``enabled=True``:
      1. base_size = available_capital × order_size_pct
      2. smoothed  = base_size × reinvestment_multiplier
      3. clamp to [min_order_usdt, max_order_usdt]
      4. validate result is positive

    This function is **pure** — no side effects, no API calls.
    It never returns 0 or negative and never raises exceptions.
    """
    try:
        # ── Disabled mode: exact current behavior ──────────────────
        if not enabled:
            result = (fallback_fixed_size or 25.0) * (reinvestment_multiplier or 1.0)
            return max(result, 1.0)

        # ── Guard against invalid inputs ───────────────────────────
        capital = max(available_capital_usdt or 0.0, 0.0)
        pct = max(order_size_pct or 0.05, 0.001)
        multiplier = reinvestment_multiplier if reinvestment_multiplier and reinvestment_multiplier > 0 else 1.0
        floor = max(min_order_usdt or 10.0, 1.0)
        cap = max(max_order_usdt or 0.0, 0.0)
        fallback = max(fallback_fixed_size or 25.0, 5.0)

        # ── Step 1: Base size from capital percentage ──────────────
        base_size = capital * pct

        # ── Step 2: Apply reinvestment smoothing multiplier ────────
        smoothed_size = base_size * multiplier

        # ── Step 3: Enforce minimum floor ──────────────────────────
        if smoothed_size < floor:
            if capital > 0:
                logger.warning(
                    "[DynamicSizing] Computed size {:.2f} USDT below minimum {:.2f}. "
                    "Capital: {:.2f}, pct: {:.3f}. Using minimum floor.",
                    smoothed_size, floor, capital, pct,
                )
            return floor

        # ── Step 4: Enforce maximum cap (if configured) ────────────
        if cap > 0 and smoothed_size > cap:
            logger.info(
                "[DynamicSizing] Computed size {:.2f} USDT exceeds cap {:.2f}. Capping.",
                smoothed_size, cap,
            )
            return cap

        return smoothed_size

    except Exception as exc:
        # Absolute safety net — never crash, fall back to fixed size
        logger.error("[DynamicSizing] Unexpected error: {}. Using fallback.", exc)
        return max(fallback_fixed_size or 25.0, 5.0)
