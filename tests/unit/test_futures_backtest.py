"""Smoke tests for the futures trend backtest."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from backtesting.futures_backtest import FuturesTrendBacktest
from config.settings import Settings


def _df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="1h"),
        "open": closes,
        "high": [c + abs(c) * 0.002 for c in closes],
        "low": [c - abs(c) * 0.002 for c in closes],
        "close": closes,
        "volume": [100.0] * n,
    })


def _bt() -> FuturesTrendBacktest:
    return FuturesTrendBacktest(settings=Settings(), initial_capital=150.0)


def test_uptrend_produces_a_winning_long():
    closes = [1000 + 3 * i for i in range(400)]  # steady strong uptrend
    res = _bt().run(_df(closes))
    m = res["metrics"]
    assert m["trades"] >= 1
    assert m["net_pnl"] > 0          # a ridden uptrend must be net positive
    assert any(t["side"] == "Buy" for t in res["trades"])


def test_downtrend_produces_a_winning_short():
    closes = [3000 - 3 * i for i in range(400)]  # steady strong downtrend
    res = _bt().run(_df(closes))
    m = res["metrics"]
    assert m["trades"] >= 1
    assert m["net_pnl"] > 0          # shorts profit in a downtrend (the whole point)
    assert any(t["side"] == "Sell" for t in res["trades"])


def test_flat_market_does_not_overtrade():
    closes = [1000 + (5 if i % 2 else -5) for i in range(400)]  # choppy, no trend
    res = _bt().run(_df(closes))
    # Low ADX -> few or no trend entries; certainly not a blowup.
    assert res["metrics"]["trades"] <= 5
    assert res["metrics"]["max_drawdown_pct"] < 50


def test_metrics_structure_is_complete():
    res = _bt().run(_df([1000 + 3 * i for i in range(400)]))
    m = res["metrics"]
    for key in ("net_pnl", "return_pct", "trades", "win_rate_pct", "win_loss_ratio",
                "expectancy_per_trade", "profit_factor", "max_drawdown_pct",
                "fees_total", "funding_total"):
        assert key in m
    assert len(res["equity_curve"]) > 0
    assert not math.isnan(m["net_pnl"])
