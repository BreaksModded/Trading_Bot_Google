"""Tests for the trend engine: entry, fixed-fractional sizing, Chandelier stop."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.regime import MarketRegime
from core.trend import (
    TrendStop, compute_fixed_fractional_qty, decide_trend, evaluate_trend_entry,
    initial_trend_stop,
)


def _decide(**kw):
    base = dict(
        regime=MarketRegime.TRENDING_UP, regime_htf=MarketRegime.TRENDING_UP,
        position_side="flat", position_flat=True, chandelier_long=1950.0,
        chandelier_short=2050.0, live_price=2000.0, equity=150.0, available_margin=150.0,
        trend_stop=None, risk_pct=0.015, leverage=5, require_htf=True,
        qty_step=Decimal("0.001"), min_qty=Decimal("0.001"),
    )
    base.update(kw)
    return decide_trend(**base)


def test_decide_enters_long_in_uptrend():
    d = _decide()
    assert d.action == "enter" and d.side == "Buy" and d.qty > 0 and d.stop is not None


def test_decide_none_on_htf_conflict():
    assert _decide(regime_htf=MarketRegime.TRENDING_DOWN).action == "none"


def test_decide_close_on_reversal():
    d = _decide(position_flat=False, position_side="long",
                regime=MarketRegime.TRENDING_DOWN, regime_htf=MarketRegime.TRENDING_DOWN)
    assert d.action == "close" and d.reason == "trend_reversal"


def test_decide_close_on_stop_hit():
    d = _decide(position_flat=False, position_side="long", live_price=1940.0)
    assert d.action == "close" and d.reason == "chandelier_stop"


def test_decide_hold_above_stop():
    d = _decide(position_flat=False, position_side="long", live_price=2000.0)
    assert d.action == "hold" and d.stop.stop_price == 1950.0


# ── Entry ─────────────────────────────────────────────────────────────


def test_uptrend_enters_long():
    e = evaluate_trend_entry(regime=MarketRegime.TRENDING_UP, higher_tf_regime=MarketRegime.TRENDING_UP, require_htf=True)
    assert e.side == "Buy"


def test_downtrend_enters_short():
    e = evaluate_trend_entry(regime=MarketRegime.TRENDING_DOWN, higher_tf_regime=MarketRegime.TRENDING_DOWN, require_htf=True)
    assert e.side == "Sell"


def test_higher_tf_conflict_blocks_entry():
    e = evaluate_trend_entry(regime=MarketRegime.TRENDING_UP, higher_tf_regime=MarketRegime.TRENDING_DOWN, require_htf=True)
    assert e.side is None and e.reason == "htf_conflict"


def test_range_regime_no_entry():
    e = evaluate_trend_entry(regime=MarketRegime.RANGING, higher_tf_regime=None, require_htf=False)
    assert e.side is None


def test_htf_ignored_when_not_required():
    e = evaluate_trend_entry(regime=MarketRegime.TRENDING_UP, higher_tf_regime=MarketRegime.TRENDING_DOWN, require_htf=False)
    assert e.side == "Buy"


# ── Fixed-fractional sizing (the positive-skew core) ──────────────────


def test_qty_risks_exactly_the_configured_fraction():
    # equity 150, risk 2% = $3 risk; stop 6% away on a $2000 entry = $120 distance.
    qty = compute_fixed_fractional_qty(
        equity=150, risk_pct=0.02, entry_price=2000, stop_price=1880,
        qty_step=Decimal("0.0001"), min_qty=Decimal("0.001"),
    )
    # qty = 3 / 120 = 0.025; loss at stop = 0.025 * 120 = $3 = 2% of equity.
    assert qty == pytest.approx(0.025, rel=1e-3)
    assert qty * (2000 - 1880) == pytest.approx(3.0, rel=1e-3)


def test_qty_capped_by_available_margin():
    # Tiny margin forces a smaller position than risk alone would allow.
    qty = compute_fixed_fractional_qty(
        equity=10_000, risk_pct=0.02, entry_price=2000, stop_price=1990,
        qty_step=Decimal("0.0001"), min_qty=Decimal("0.001"),
        available_margin=20, leverage=2,
    )
    # margin cap: notional <= 40 -> qty <= 0.02
    assert qty <= 0.02 + 1e-9


def test_qty_below_min_returns_zero():
    qty = compute_fixed_fractional_qty(
        equity=10, risk_pct=0.01, entry_price=2000, stop_price=1900,
        qty_step=Decimal("0.001"), min_qty=Decimal("0.01"),
    )
    assert qty == 0.0


def test_zero_stop_distance_is_safe():
    assert compute_fixed_fractional_qty(
        equity=150, risk_pct=0.02, entry_price=2000, stop_price=2000,
        qty_step=Decimal("0.001"), min_qty=Decimal("0.001"),
    ) == 0.0


# ── Chandelier trailing stop ──────────────────────────────────────────


def test_long_stop_only_ratchets_up():
    stop = initial_trend_stop("Buy", chandelier_long=1900, chandelier_short=2100)
    stop.update(chandelier_long=1950, chandelier_short=2050)  # price rose
    assert stop.stop_price == 1950
    stop.update(chandelier_long=1920, chandelier_short=2080)  # pullback: stop must NOT drop
    assert stop.stop_price == 1950


def test_short_stop_only_ratchets_down():
    stop = initial_trend_stop("Sell", chandelier_long=1900, chandelier_short=2100)
    stop.update(chandelier_long=1850, chandelier_short=2050)  # price fell
    assert stop.stop_price == 2050
    stop.update(chandelier_long=1880, chandelier_short=2080)  # bounce: stop must NOT rise
    assert stop.stop_price == 2050


def test_stop_hit_detection():
    long_stop = TrendStop("Buy", 1950)
    assert long_stop.is_hit(1949) and not long_stop.is_hit(1951)
    short_stop = TrendStop("Sell", 2050)
    assert short_stop.is_hit(2051) and not short_stop.is_hit(2049)
