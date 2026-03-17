"""Tests for GET /api/portfolio/holdings endpoint."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from api.routes.portfolio import get_holdings, _parse_coin


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_request(portfolio_snapshot=None, latest_indicators=None):
    """Build a mock FastAPI Request with a mocked db in app.state."""
    req = MagicMock()

    def _get_runtime_config(key):
        if key == "portfolio_snapshot":
            return portfolio_snapshot
        if key == "latest_indicators":
            return latest_indicators
        return None

    req.app.state.db.get_runtime_config.side_effect = _get_runtime_config
    return req


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_holdings_endpoint_returns_expected_keys():
    """Endpoint returns all required top-level keys (status 200 implied)."""
    req = _make_request(portfolio_snapshot=None, latest_indicators=None)
    result = await get_holdings(req, username="admin")

    assert "holdings" in result
    assert "free_usdt" in result
    assert "total_equity" in result
    assert "total_pnl_usdt" in result
    assert "total_pnl_pct" in result
    assert "updated_at" in result


@pytest.mark.asyncio
async def test_holdings_includes_coins_with_balance():
    """SOL qty=0.5, avg_cost=95, price=96 → included with pnl_pct ≈ 1.05%."""
    snapshot = {
        "free_usdt": 10.0,
        "holdings": {
            "SOLUSDC": {
                "qty": 0.5,
                "avg_cost": 95.0,
                "has_sell_order": False,
                "sell_order_price": None,
            }
        },
        "updated_at": "2026-03-17T08:00:00Z",
    }
    indicators = {"SOLUSDC": {"current_price": 96.0}}

    req = _make_request(portfolio_snapshot=snapshot, latest_indicators=indicators)
    result = await get_holdings(req, username="admin")

    assert len(result["holdings"]) == 1
    h = result["holdings"][0]
    assert h["symbol"] == "SOLUSDC"
    assert h["coin"] == "SOL"
    assert h["qty"] == pytest.approx(0.5)
    assert h["avg_cost"] == pytest.approx(95.0)
    assert h["current_price"] == pytest.approx(96.0)
    assert h["value_usdt"] == pytest.approx(48.0)
    assert h["pnl_usdt"] == pytest.approx(0.5)           # (96-95)*0.5
    assert h["pnl_pct"] == pytest.approx(1.0526, abs=0.01)  # 1/95*100


@pytest.mark.asyncio
async def test_holdings_excludes_dust():
    """ETH qty=0.000005 is below dust threshold → not included in holdings."""
    snapshot = {
        "free_usdt": 50.0,
        "holdings": {
            "ETHUSDT": {
                "qty": 0.000005,
                "avg_cost": 3000.0,
                "has_sell_order": False,
                "sell_order_price": None,
            }
        },
        "updated_at": "2026-03-17T08:00:00Z",
    }
    indicators = {"ETHUSDT": {"current_price": 3200.0}}

    req = _make_request(portfolio_snapshot=snapshot, latest_indicators=indicators)
    result = await get_holdings(req, username="admin")

    assert result["holdings"] == []
    assert result["free_usdt"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_holdings_pnl_zero_when_no_avg_cost():
    """avg_cost=0 → pnl_usdt=0, pnl_pct=0 (no ZeroDivisionError)."""
    snapshot = {
        "free_usdt": 0.0,
        "holdings": {
            "BTCUSDC": {
                "qty": 0.001,
                "avg_cost": 0.0,   # unknown cost basis
                "has_sell_order": False,
                "sell_order_price": None,
            }
        },
        "updated_at": "2026-03-17T08:00:00Z",
    }
    indicators = {"BTCUSDC": {"current_price": 85_000.0}}

    req = _make_request(portfolio_snapshot=snapshot, latest_indicators=indicators)
    result = await get_holdings(req, username="admin")

    assert len(result["holdings"]) == 1
    h = result["holdings"][0]
    assert h["pnl_usdt"] == pytest.approx(0.0)
    assert h["pnl_pct"] == pytest.approx(0.0)
    assert h["value_usdt"] == pytest.approx(85.0)  # qty * price still computed


@pytest.mark.asyncio
async def test_holdings_has_sell_order_flag():
    """has_sell_order=True + sell_order_price propagated correctly."""
    snapshot = {
        "free_usdt": 20.0,
        "holdings": {
            "SOLUSDC": {
                "qty": 0.5,
                "avg_cost": 95.0,
                "has_sell_order": True,
                "sell_order_price": 96.10,
            }
        },
        "updated_at": "2026-03-17T08:00:00Z",
    }
    indicators = {"SOLUSDC": {"current_price": 95.77}}

    req = _make_request(portfolio_snapshot=snapshot, latest_indicators=indicators)
    result = await get_holdings(req, username="admin")

    h = result["holdings"][0]
    assert h["has_sell_order"] is True
    assert h["sell_order_price"] == pytest.approx(96.10)
