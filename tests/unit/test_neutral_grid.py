"""Unit tests for the neutral futures grid geometry (core/grid.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from config.settings import FuturesSettings
from core.grid import build_grid_plan, partner_order, validate_grid


def _settings(**overrides) -> FuturesSettings:
    base = dict(
        symbol="ETHUSDT", leverage=2, grid_levels=8, use_atr_range=False,
        grid_range_pct=0.10, min_spacing_pct=0.004, capital_fraction=0.8,
        order_size_usdt=0.0, min_order_usdt=5.0, stop_loss_pct=0.12,
        grid_risk_pct=0.0,  # raw sizing for the base tests; cap tested separately
    )
    base.update(overrides)
    return FuturesSettings(**base)


def _plan(qty_step="0.01", min_qty="0.01", **overrides):
    return build_grid_plan(
        symbol="ETHUSDT", mid=2000.0, atr_pct=0.0, settings=_settings(**overrides),
        available_usdt=150.0, qty_step=Decimal(qty_step),
        min_qty=Decimal(min_qty), tick_size=Decimal("0.01"),
    )


def test_grid_is_symmetric_long_below_short_above():
    plan = _plan()
    buys = [lv for lv in plan.levels if lv.side == "Buy"]
    sells = [lv for lv in plan.levels if lv.side == "Sell"]
    assert len(buys) == len(sells) == 8
    # Every buy is below mid, every sell above mid.
    assert all(lv.price < plan.mid for lv in buys)
    assert all(lv.price > plan.mid for lv in sells)


def test_spacing_respects_minimum():
    # A tiny range would force spacing below min; it must be clamped.
    plan = _plan(grid_range_pct=0.02, grid_levels=8, min_spacing_pct=0.004)
    assert plan.spacing_pct >= 0.004 - 1e-9


def test_stops_sit_outside_the_grid_range():
    plan = _plan()
    assert plan.stop_loss_lower < plan.lower_bound < plan.mid
    assert plan.stop_loss_upper > plan.upper_bound > plan.mid


def test_partner_of_buy_is_sell_one_step_up():
    p = partner_order(filled_side="Buy", filled_price=1980.0,
                      spacing_pct=0.01, qty=0.05, tick_size=Decimal("0.01"))
    assert p.side == "Sell"
    assert p.price == pytest.approx(1980.0 * 1.01, rel=1e-4)


def test_partner_of_sell_is_buy_one_step_down():
    p = partner_order(filled_side="Sell", filled_price=2020.0,
                      spacing_pct=0.01, qty=0.05, tick_size=Decimal("0.01"))
    assert p.side == "Buy"
    assert p.price == pytest.approx(2020.0 * 0.99, rel=1e-4)


def test_sizing_uses_leverage_and_capital_fraction():
    # 150 USDT * 0.8 fraction * 2x leverage / 8 levels = 30 USDT notional/level.
    # Use a fine qty_step so rounding doesn't mask the sizing math.
    plan = _plan(qty_step="0.0001")
    assert plan.notional_per_level == pytest.approx(30.0, rel=0.05)


def test_coarse_qty_step_rounds_notional_down():
    # Realistic ETH step 0.01: target 0.015 -> floored to 0.01 -> notional 20, not 30.
    plan = _plan(qty_step="0.01")
    assert plan.qty_per_level == pytest.approx(0.01)
    assert plan.notional_per_level == pytest.approx(20.0)


def test_validate_rejects_subfee_spacing():
    # Valid settings that still yield spacing below 3x round-trip fee:
    # range 2% / 8 levels = 0.25% spacing < 0.33% fee threshold.
    plan = _plan(min_spacing_pct=0.001, grid_range_pct=0.02, grid_levels=8)
    ok, reason = validate_grid(plan, min_order_usdt=5.0)
    assert not ok and "fee" in reason


def test_validate_accepts_healthy_grid():
    ok, reason = validate_grid(_plan(), min_order_usdt=5.0)
    assert ok, reason


def test_risk_cap_shrinks_oversized_grid():
    # With a risk budget (and a fine min_qty so the cap can bite), the grid is sized
    # smaller than the raw leverage sizing and stays within budget.
    raw = _plan(qty_step="0.0001", min_qty="0.0001", grid_risk_pct=0.0)
    capped = _plan(qty_step="0.0001", min_qty="0.0001", grid_risk_pct=0.04)
    assert capped.notional_per_level < raw.notional_per_level
    assert capped.worst_case_loss <= 150 * 0.04 * 1.20


def test_validate_rejects_grid_over_risk_budget():
    # A high-leverage grid that the exchange minimum forces above budget is rejected.
    plan = _plan(qty_step="0.0001", grid_risk_pct=0.0, leverage=10)
    ok, reason = validate_grid(plan, min_order_usdt=5.0, capital=150.0, grid_risk_pct=0.01)
    assert not ok and "risk budget" in reason
