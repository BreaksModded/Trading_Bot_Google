"""Neutral grid geometry for linear-perpetual futures.

A neutral grid ladders Buy limit orders below the mid price and Sell limit
orders above it. In one-way position mode the net position oscillates: it
accumulates long as price falls through the buy rungs and flips toward short
as price rises through the sell rungs — so the bot profits from oscillation in
BOTH directions, which is the whole reason we moved off spot.

Each filled rung places a partner order one spacing step away on the opposite
side (a Buy fill -> a Sell one step up; a Sell fill -> a Buy one step down),
capturing ``spacing`` per round trip. Risk is bounded by a hard stop-loss a
fixed distance beyond the grid edges and by conservative leverage that keeps
the liquidation price well outside that stop.

This module is pure geometry/sizing — no I/O. It is trivially unit-testable
and is consumed by the position manager and the main loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal


@dataclass(slots=True)
class GridLevelSpec:
    """A single rung: a limit order the bot should maintain."""

    level_id: str
    price: float
    side: str  # "Buy" | "Sell"
    qty: float


@dataclass(slots=True)
class GridPlan:
    """The full neutral grid: entry ladder, range bounds and hard stops."""

    symbol: str
    mid: float
    spacing_pct: float
    qty_per_level: float
    lower_bound: float
    upper_bound: float
    stop_loss_lower: float
    stop_loss_upper: float
    worst_case_loss: float = 0.0  # one side fully filled + price at the stop band
    levels: list[GridLevelSpec] = field(default_factory=list)

    @property
    def n_per_side(self) -> int:
        return len(self.levels) // 2

    @property
    def notional_per_level(self) -> float:
        return self.qty_per_level * self.mid


# ── helpers ───────────────────────────────────────────────────────────


def _round_down(value: float, step: Decimal) -> float:
    """Round *value* down to a multiple of *step* (price tick or qty step)."""
    if step <= 0:
        return value
    units = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * step)


# ── grid construction ─────────────────────────────────────────────────


def build_grid_plan(
    *,
    symbol: str,
    mid: float,
    atr_pct: float,
    settings,  # config.settings.FuturesSettings
    available_usdt: float,
    qty_step: Decimal,
    min_qty: Decimal,
    tick_size: Decimal,
) -> GridPlan:
    """Compute the neutral grid plan around *mid*.

    Sizing deploys ``capital_fraction`` of available margin at the configured
    leverage, split across the levels of one side, so that a full one-sided
    fill (the worst directional case) consumes at most that fraction of margin
    and leaves headroom before liquidation.
    """
    if mid <= 0:
        raise ValueError("mid price must be positive")

    n = int(settings.grid_levels)

    # Half-range as a fraction of price: ATR-derived or fixed.
    if settings.use_atr_range and atr_pct > 0:
        half_range = atr_pct * settings.grid_range_atr_multiple
    else:
        half_range = settings.grid_range_pct
    half_range = max(half_range, settings.min_spacing_pct * n)
    spacing = max(settings.min_spacing_pct, half_range / n)

    # Sizing.
    if settings.order_size_usdt > 0:
        per_level_notional = float(settings.order_size_usdt)
    else:
        max_notional = available_usdt * settings.capital_fraction * settings.leverage
        per_level_notional = (max_notional / n) if n else 0.0

    # Risk cap: bound the worst-case loss (one side fully filled, price at the stop
    # band) to grid_risk_pct of capital — the same risk philosophy as the trend
    # engine, so leverage cannot silently oversize the grid.
    worst_loss_frac = settings.stop_loss_pct + spacing * (n - 1) / 2.0
    grid_risk_pct = getattr(settings, "grid_risk_pct", 0.0)
    if grid_risk_pct > 0 and worst_loss_frac > 0 and n > 0:
        max_per_level_risk = (grid_risk_pct * available_usdt) / (n * worst_loss_frac)
        per_level_notional = min(per_level_notional, max_per_level_risk)
    per_level_notional = max(per_level_notional, settings.min_order_usdt)

    qty = _round_down(per_level_notional / mid, qty_step)
    if qty < float(min_qty):
        qty = float(min_qty)  # floor to exchange minimum; viability checked by caller
    worst_case_loss = qty * mid * n * worst_loss_frac

    levels: list[GridLevelSpec] = []
    for i in range(1, n + 1):
        long_price = _round_down(mid * (1 - spacing * i), tick_size)
        short_price = _round_down(mid * (1 + spacing * i), tick_size)
        levels.append(GridLevelSpec(f"L{i}", long_price, "Buy", qty))
        levels.append(GridLevelSpec(f"S{i}", short_price, "Sell", qty))

    lower_bound = _round_down(mid * (1 - spacing * n), tick_size)
    upper_bound = _round_down(mid * (1 + spacing * n), tick_size)
    sl_lower = _round_down(mid * (1 - spacing * n - settings.stop_loss_pct), tick_size)
    sl_upper = _round_down(mid * (1 + spacing * n + settings.stop_loss_pct), tick_size)

    return GridPlan(
        symbol=symbol,
        mid=mid,
        spacing_pct=spacing,
        qty_per_level=qty,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        stop_loss_lower=sl_lower,
        stop_loss_upper=sl_upper,
        worst_case_loss=worst_case_loss,
        levels=levels,
    )


def partner_order(
    *, filled_side: str, filled_price: float, spacing_pct: float, qty: float, tick_size: Decimal,
) -> GridLevelSpec:
    """The opposite-side order placed one spacing step away after a fill.

    A Buy fill -> a Sell one step up (books profit on the long / seeds a short).
    A Sell fill -> a Buy one step down (books profit on the short / seeds a long).
    """
    if filled_side == "Buy":
        price = _round_down(filled_price * (1 + spacing_pct), tick_size)
        return GridLevelSpec("tp", price, "Sell", qty)
    price = _round_down(filled_price * (1 - spacing_pct), tick_size)
    return GridLevelSpec("tp", price, "Buy", qty)


def validate_grid(
    plan: GridPlan, *, min_order_usdt: float, capital: float = 0.0,
    grid_risk_pct: float = 0.0, round_trip_fee_pct: float = 0.0011,
) -> tuple[bool, str]:
    """Check the grid is economically viable AND within the risk budget.

    - Per-level notional must clear the exchange minimum.
    - Spacing must capture at least 3x the round-trip fee, otherwise the grid
      churns fees without an edge (the exact failure mode the audit found).
    - Worst-case loss must fit the risk budget; if the exchange minimum forced a
      size above budget, the grid is too risky at this capital and is skipped.
    """
    if plan.notional_per_level < min_order_usdt:
        return False, f"per-level notional {plan.notional_per_level:.2f} < min {min_order_usdt:.2f}"
    if plan.spacing_pct < 3 * round_trip_fee_pct:
        return False, (
            f"spacing {plan.spacing_pct * 100:.3f}% < 3x round-trip fee "
            f"({3 * round_trip_fee_pct * 100:.3f}%)"
        )
    if grid_risk_pct > 0 and capital > 0 and plan.worst_case_loss > capital * grid_risk_pct * 1.15:
        return False, (
            f"worst-case loss {plan.worst_case_loss:.2f} exceeds risk budget "
            f"{capital * grid_risk_pct:.2f} ({grid_risk_pct:.0%} of {capital:.0f})"
        )
    return True, "ok"
