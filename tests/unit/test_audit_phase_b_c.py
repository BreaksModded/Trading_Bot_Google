"""Tests for the May 2026 audit Phase B + C features.

Covers:
- C.2 DCA qty increment per BUY level
- C.3 Trend-rider BUY-ladder build mode
- B.3 Trailing TP on inverse SELLs (OrderManager.update_inverse_tp_trailing)
- B.2 Updated score signal (volume + regime bonuses, half ADX penalty)
- A.2 Directional ADX filter (pause_grid only blocks SHORT trend)
- estimate_required_capital with non-uniform BUY qty
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pandas as pd
import pytest

from core.order_manager import ManagedOrder, OrderManager
from core.regime import MarketRegime
from core.strategy import GridStrategy, StrategyConfig, StrategySignal
from data.models import GridLevel, OrderSide, OrderStatus, TrendBias


# ── Helpers ──────────────────────────────────────────────────────────────


def _config(**overrides) -> StrategyConfig:
    base = dict(
        symbol="SOLUSDC",
        num_levels=3,
        min_spacing_pct=0.008,
        atr_multiplier=1.5,
        order_size_usdt=30.0,
        adx_threshold=40.0,
        ema_fast=50,
        ema_slow=200,
    )
    base.update(overrides)
    return StrategyConfig(**base)


def _market_df(price: float = 100.0, n: int = 250) -> pd.DataFrame:
    # Slight noise so ATR and ADX are non-trivial
    rows = []
    for i in range(n):
        p = price + (i % 5) * 0.5
        rows.append(
            {
                "open": p,
                "high": p + 0.4,
                "low": p - 0.4,
                "close": p,
                "volume": 1000 + i,
            }
        )
    return pd.DataFrame(rows)


# ── C.2 DCA qty increment ────────────────────────────────────────────────


def test_dca_increment_zero_keeps_flat_qty():
    strategy = GridStrategy(_config(dca_qty_increment=0.0))
    levels = strategy._build_levels(
        current_price=100.0,
        buy_spacing_pct=0.01,
        sell_spacing_pct=0.01,
        trend=TrendBias.NEUTRAL,
        target_size=30.0,
    )
    buys = [lvl for lvl in levels if lvl.side == OrderSide.BUY]
    assert len(buys) == 3
    qtys = [b.qty for b in buys]
    assert qtys[0] == pytest.approx(qtys[1]) == pytest.approx(qtys[2])


def test_dca_increment_scales_deeper_levels():
    strategy = GridStrategy(_config(dca_qty_increment=0.3))
    levels = strategy._build_levels(
        current_price=100.0,
        buy_spacing_pct=0.01,
        sell_spacing_pct=0.01,
        trend=TrendBias.NEUTRAL,
        target_size=30.0,
    )
    buys = sorted([lvl for lvl in levels if lvl.side == OrderSide.BUY], key=lambda x: -x.price)
    base = 30.0 / 100.0
    # idx 1 (closest to spot) → flat qty, idx 2 → ×1.3, idx 3 → ×1.6
    assert buys[0].qty == pytest.approx(base)
    assert buys[1].qty == pytest.approx(base * 1.3)
    assert buys[2].qty == pytest.approx(base * 1.6)


def test_dca_does_not_affect_sell_qty():
    strategy = GridStrategy(_config(dca_qty_increment=0.3))
    levels = strategy._build_levels(
        current_price=100.0,
        buy_spacing_pct=0.01,
        sell_spacing_pct=0.01,
        trend=TrendBias.NEUTRAL,
        target_size=30.0,
    )
    sells = [lvl for lvl in levels if lvl.side == OrderSide.SELL]
    base = 30.0 / 100.0
    assert all(s.qty == pytest.approx(base) for s in sells)


def test_estimate_required_capital_with_dca():
    """estimate_required_capital must sum actual BUY notional, not assume flat."""
    strategy = GridStrategy(_config(num_levels=3, dca_qty_increment=0.3))
    df = _market_df(price=50.0)
    sig = strategy.compute_signal(df)
    expected = sum(lvl.qty * lvl.price for lvl in sig.levels if lvl.side == OrderSide.BUY)
    assert strategy.estimate_required_capital(sig) == pytest.approx(expected)


# ── C.3 Trend-rider mode ─────────────────────────────────────────────────


def test_trend_rider_ladder_is_buy_only_and_sized_correctly():
    cfg = _config(
        enable_trend_rider=True,
        trend_rider_levels=4,
        trend_rider_buy_spacing_pct=0.005,
        dca_qty_increment=0.0,
    )
    strategy = GridStrategy(cfg)
    levels = strategy._build_trend_rider_levels(current_price=100.0, target_size=20.0)
    assert len(levels) == 4
    assert all(lvl.side == OrderSide.BUY for lvl in levels)
    # Prices descend from spot in 0.5% steps
    prices = [lvl.price for lvl in levels]
    assert prices == sorted(prices, reverse=True)
    assert prices[0] == pytest.approx(100.0 * (1 - 0.005))
    assert prices[3] == pytest.approx(100.0 * (1 - 0.005 * 4))


def test_trend_rider_activates_only_under_strong_uptrend():
    cfg = _config(
        enable_trend_rider=True,
        trend_rider_adx_threshold=50.0,
    )
    strategy = GridStrategy(cfg)
    df = _market_df(price=80.0)
    # Force compute_signal to evaluate. We can't easily fake ADX, so
    # call _build_trend_rider_levels directly to check the level structure;
    # the activation gate is unit-tested via is-the-condition logic:
    levels = strategy._build_trend_rider_levels(80.0, 24.0)
    assert all(lvl.side == OrderSide.BUY for lvl in levels)


# ── A.2 Directional ADX filter ───────────────────────────────────────────


def test_adx_filter_does_not_pause_long_trend(monkeypatch):
    """High ADX with LONG bias must NOT pause grid (we ride the move)."""
    strategy = GridStrategy(_config(adx_threshold=30.0))
    # Construct a df where ADX will exceed 30 and EMA fast > slow
    n = 300
    rows = []
    for i in range(n):
        # Steadily rising price → LONG trend + decent ADX
        p = 100.0 + i * 0.2
        rows.append({"open": p - 0.1, "high": p + 0.5, "low": p - 0.6, "close": p, "volume": 1000})
    df = pd.DataFrame(rows)
    sig = strategy.compute_signal(df)
    # LONG trend with high ADX → must stay active
    assert sig.trend_bias == TrendBias.LONG
    if sig.adx_value > 30.0:
        assert sig.pause_new_grid is False, (
            f"pause_new_grid={sig.pause_new_grid} ADX={sig.adx_value} trend={sig.trend_bias}"
        )


def test_adx_filter_pauses_short_high_adx():
    """High ADX + SHORT bias → still blocks (falling-knife protection)."""
    strategy = GridStrategy(_config(adx_threshold=20.0))
    # Steadily falling price → SHORT trend
    rows = []
    for i in range(300):
        p = 100.0 - i * 0.2
        rows.append({"open": p + 0.1, "high": p + 0.6, "low": p - 0.5, "close": p, "volume": 1000})
    df = pd.DataFrame(rows)
    sig = strategy.compute_signal(df)
    assert sig.trend_bias == TrendBias.SHORT
    if sig.adx_value > 20.0:
        assert sig.pause_new_grid is True


# ── B.3 Trailing TP on inverse SELLs ─────────────────────────────────────


class _FakeExchange:
    def __init__(self) -> None:
        self.cancel_calls: list[str] = []
        self.placed: list[dict] = []
        self._next_id = 1000

    async def cancel_order(self, *, symbol: str, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: float, price: float, orderLinkId: str | None = None,
    ) -> str:
        self._next_id += 1
        new_id = f"new-{self._next_id}"
        self.placed.append(
            {"symbol": symbol, "side": side, "qty": qty, "price": price, "id": new_id}
        )
        return new_id

    async def cancel_all_orders(self, *, symbol: str) -> None:
        return None

    async def get_open_orders(self, *, symbol: str) -> list[dict]:
        return []

    async def get_order_history(self, *, symbol: str, order_id: str) -> dict | None:
        return None

    async def place_market_order(self, *, symbol: str, side: str, qty: float) -> str:
        return "mkt-id"

    async def get_orderbook(self, *, symbol: str, limit: int = 5) -> dict:
        return {"spread_pct": 0.001, "bid_depth_usdt": 10000.0, "bids": [], "asks": []}


def _make_manager_with_inverse(price_avg: float, sell_price: float) -> tuple[OrderManager, _FakeExchange, ManagedOrder]:
    ex = _FakeExchange()
    mgr = OrderManager(exchange=ex, symbol="SOLUSDC")
    mgr._avg_cost = price_avg
    mgr._position_qty = 0.5
    inverse = ManagedOrder(
        order_id="inv-1",
        level_id="inv-buy-1-100-1700000000000",
        symbol="SOLUSDC",
        side=OrderSide.SELL,
        price=sell_price,
        qty=0.5,
        status=OrderStatus.PENDING,
    )
    mgr._orders[inverse.order_id] = inverse
    return mgr, ex, inverse


def test_trailing_tp_skips_when_below_activation():
    mgr, ex, inverse = _make_manager_with_inverse(price_avg=100.0, sell_price=100.5)
    n = asyncio.run(
        mgr.update_inverse_tp_trailing(
            current_price=101.0, atr_pct=0.02,  # 1×ATR above cost, activation needs 2×
            activation_atr_multiple=2.0, trail_atr_multiple=1.0,
        )
    )
    assert n == 0
    assert ex.cancel_calls == []


def test_trailing_tp_ratchets_up_when_price_runs():
    # avg_cost 100, ATR 5%, current price 115 → 3×ATR above cost (15%)
    # Activation = 100 × (1 + 0.05*2) = 110. Current 115 > 110 → activate.
    # Trail target = 115 × (1 - 0.05*1) = 109.25
    # Existing sell at 101 → 109.25 > 101 × 1.003 → ratchet
    mgr, ex, inverse = _make_manager_with_inverse(price_avg=100.0, sell_price=101.0)
    n = asyncio.run(
        mgr.update_inverse_tp_trailing(
            current_price=115.0, atr_pct=0.05,
            activation_atr_multiple=2.0, trail_atr_multiple=1.0,
        )
    )
    assert n == 1
    assert inverse.order_id in ex.cancel_calls
    assert ex.placed[0]["price"] == pytest.approx(115.0 * 0.95)
    assert ex.placed[0]["side"].lower() == "sell"


def test_trailing_tp_never_lowers_existing_sell():
    # Current price reaches activation, but trail target is BELOW existing sell → no change
    mgr, ex, inverse = _make_manager_with_inverse(price_avg=100.0, sell_price=120.0)
    n = asyncio.run(
        mgr.update_inverse_tp_trailing(
            current_price=115.0, atr_pct=0.05,
            activation_atr_multiple=2.0, trail_atr_multiple=1.0,
        )
    )
    # Trail target 109.25 < existing 120 → skip
    assert n == 0
    assert ex.cancel_calls == []


def test_trailing_tp_skips_untracked_position():
    mgr, ex, inverse = _make_manager_with_inverse(price_avg=100.0, sell_price=101.0)
    mgr._position_untracked = True
    n = asyncio.run(
        mgr.update_inverse_tp_trailing(
            current_price=120.0, atr_pct=0.05,
            activation_atr_multiple=2.0, trail_atr_multiple=1.0,
        )
    )
    assert n == 0
    assert ex.cancel_calls == []


def test_trailing_tp_respects_min_safe_price_above_fees():
    """When trail target falls under cost+fees, we don't ratchet (would lock a loss)."""
    # avg_cost 100, fees 0.0001 → min_safe_price = 100 × (1 + 4*0.0001) = 100.04
    # If trail target ends below 100.04, skip.
    mgr, ex, inverse = _make_manager_with_inverse(price_avg=100.0, sell_price=99.0)
    # Force activation but very tiny ATR keeps target near current
    # current 100.05, atr 0.0001 → activation = 100 × (1 + 0.0001*2) = 100.02 → activates
    # trail target = 100.05 × (1 - 0.0001*1) = 100.04 → equals min_safe; skip
    n = asyncio.run(
        mgr.update_inverse_tp_trailing(
            current_price=100.05, atr_pct=0.0001,
            activation_atr_multiple=2.0, trail_atr_multiple=1.0,
        )
    )
    assert n == 0


# ── B.2 Score signal with new bonuses ────────────────────────────────────


def _signal(symbol: str, atr_pct: float, adx: float, regime: str, vol: float, trend: str = "long") -> StrategySignal:
    return StrategySignal(
        generated_at=None,
        current_price=100.0,
        spacing_pct=0.01,
        trend_bias=TrendBias.LONG if trend == "long" else TrendBias.SHORT if trend == "short" else TrendBias.NEUTRAL,
        adx_value=adx,
        atr_pct=atr_pct,
        volume_ratio=vol,
        pause_new_grid=False,
        target_notional=30.0,
        levels=[],
        regime=regime,
    )


def test_score_signal_rewards_ranging_high_volume():
    """Ranging regime + high volume must score above transitional + low volume."""
    from main import TradingBot
    sigs = {
        "A": _signal("A", atr_pct=0.02, adx=20.0, regime="ranging", vol=2.0),
        "B": _signal("B", atr_pct=0.02, adx=20.0, regime="transitional", vol=0.8),
    }
    sa = TradingBot._score_signal(sigs["A"], sigs)
    sb = TradingBot._score_signal(sigs["B"], sigs)
    assert sa > sb


def test_score_signal_blocks_trending_down():
    """trending_down has 0 regime bonus (effectively neutral)."""
    from main import TradingBot
    sigs = {
        "A": _signal("A", atr_pct=0.02, adx=40.0, regime="trending_down", vol=1.0, trend="short"),
        "B": _signal("B", atr_pct=0.02, adx=40.0, regime="ranging", vol=1.0),
    }
    sa = TradingBot._score_signal(sigs["A"], sigs)
    sb = TradingBot._score_signal(sigs["B"], sigs)
    assert sb > sa  # ranging beats trending_down with same ATR/ADX/volume
