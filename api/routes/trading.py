"""Trading and grid data API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from api.middleware import verify_token

router = APIRouter()


@router.get("/trading/market")
async def get_market_data(request: Request, username: str = Depends(verify_token)):
    """Get current market data and exchange status."""
    exchange = request.app.state.exchange
    settings = request.app.state.settings

    symbol = settings.active_symbols[0] if settings.active_symbols else "BTCUSDC"
    result = {"symbol": symbol, "price": 0.0, "latency_ms": 0.0}

    try:
        result["price"] = await exchange.get_last_price(symbol=symbol)
        result["latency_ms"] = getattr(request.app.state, "latest_latency_ms", 0.0)
    except Exception as e:
        result["error"] = str(e)

    return result


@router.get("/trading/klines")
async def get_klines(
    request: Request,
    symbol: str = Query(None, description="Trading pair symbol"),
    interval: str = Query("60", description="Kline interval (1, 5, 15, 60, 240, D)"),
    limit: int = Query(200, ge=10, le=1000),
    username: str = Depends(verify_token),
):
    """Get historical kline data for the chart."""
    exchange = request.app.state.exchange
    settings = request.app.state.settings
    
    target_symbol = symbol or (settings.active_symbols[0] if settings.active_symbols else "BTCUSDC")
    
    try:
        # Pass the resolved symbol to get_klines
        df = await exchange.get_klines(symbol=target_symbol, interval=interval, limit=limit)
        klines = df.to_dict(orient="records")
        for row in klines:
            if hasattr(row.get("timestamp"), "isoformat"):
                row["timestamp"] = row["timestamp"].isoformat()
        return {"klines": klines, "count": len(klines)}
    except Exception as e:
        return {"klines": [], "error": str(e)}


@router.get("/trading/grid")
async def get_grid_state(request: Request, username: str = Depends(verify_token)):
    """Get current grid state with all levels from the database."""
    db = request.app.state.db
    grid_states = db.get_latest_grid_states()
    if not grid_states:
        latest = db.get_latest_grid_state()
        if latest:
            grid_states = [latest]

    return {"active": bool(grid_states), "grid_states": grid_states}


@router.get("/trading/trades")
async def get_trade_history(
    request: Request,
    symbol: str = Query(None, description="Filter by generic symbol"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    username: str = Depends(verify_token),
):
    """Get trade history with pagination."""
    db = request.app.state.db
    # Pass symbol filter to db
    trades = db.get_recent_trades(limit=limit, offset=offset, symbol=symbol)
    total = db.get_trade_count(symbol=symbol)

    return {
        "trades": trades,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
