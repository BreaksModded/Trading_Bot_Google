"""Portfolio holdings API route."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.middleware import verify_token

router = APIRouter()

_DUST_THRESHOLD = 0.0001


@router.get("/portfolio/holdings")
async def get_holdings(
    request: Request,
    username: str = Depends(verify_token),
) -> dict[str, Any]:
    """Return current holdings snapshot built from in-memory bot data.

    Reads ``positions`` (qty + avg_cost per symbol) and ``latest_indicators``
    (current_price per symbol) from runtime_config — both written by the bot
    each cycle.  Never calls the exchange.
    """
    db = request.app.state.db

    positions: dict[str, Any] = db.get_runtime_config("positions") or {}
    indicators: dict[str, Any] = db.get_runtime_config("latest_indicators") or {}

    holdings: list[dict[str, Any]] = []
    total_value = 0.0
    total_pnl = 0.0

    for symbol, pos in positions.items():
        qty = float(pos.get("qty", 0.0))
        if qty < _DUST_THRESHOLD:
            continue

        avg_cost = float(pos.get("avg_cost", 0.0))

        inds = indicators.get(symbol, {})
        current_price = float(inds.get("current_price", 0.0))

        value_usdt = qty * current_price if current_price > 0 else 0.0

        if avg_cost > 0 and current_price > 0:
            pnl_usdt = (current_price - avg_cost) * qty
            pnl_pct = ((current_price - avg_cost) / avg_cost) * 100.0
        else:
            pnl_usdt = 0.0
            pnl_pct = 0.0

        coin = _parse_coin(symbol)

        holdings.append({
            "symbol": symbol,
            "coin": coin,
            "qty": qty,
            "avg_cost": avg_cost,
            "current_price": current_price,
            "value_usdt": round(value_usdt, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "pnl_pct": round(pnl_pct, 4),
            "has_sell_order": False,
            "sell_order_price": None,
        })

        total_value += value_usdt
        total_pnl += pnl_usdt

    free_usdt = 0.0
    total_equity = total_value + free_usdt
    cost_basis = sum(
        float(positions[s].get("avg_cost", 0.0)) * float(positions[s].get("qty", 0.0))
        for s in positions
        if float(positions[s].get("qty", 0.0)) >= _DUST_THRESHOLD
        and float(positions[s].get("avg_cost", 0.0)) > 0
    )
    total_pnl_pct = (total_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

    return {
        "holdings": holdings,
        "free_usdt": round(free_usdt, 4),
        "total_equity": round(total_equity, 4),
        "total_pnl_usdt": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _parse_coin(symbol: str) -> str:
    """Extract base coin from symbol.  BTCUSDC → BTC, SOLUSDC → SOL, ETHUSDT → ETH."""
    for quote in ("USDC", "USDT", "BTC", "ETH", "BNB"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol
