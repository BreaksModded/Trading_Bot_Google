"""Configuration management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from api.middleware import verify_token

router = APIRouter()


class GridConfigUpdate(BaseModel):
    """Schema for updating grid configuration."""
    num_levels: int | None = Field(None, ge=2, le=10)
    min_spacing_pct: float | None = Field(None, ge=0.003, le=0.05)
    atr_multiplier: float | None = Field(None, ge=0.5, le=5.0)
    order_size_usdt: float | None = Field(None, ge=5.0, le=500.0)
    adx_threshold: float | None = Field(None, ge=15, le=50)
    ema_fast: int | None = Field(None, ge=5, le=100)
    ema_slow: int | None = Field(None, ge=50, le=500)
    max_drawdown_pct: float | None = Field(None, ge=0.05, le=0.50)
    max_daily_loss_pct: float | None = Field(None, ge=0.005, le=0.10)


@router.get("/config/current")
async def get_current_config(request: Request, username: str = Depends(verify_token)):
    """Get current bot configuration."""
    state = request.app.state
    if hasattr(state, "settings") and state.settings:
        if hasattr(state.settings, "to_grid_dict"):
            return state.settings.to_grid_dict()
        return state.settings.model_dump()
    return {}


@router.put("/config/update")
async def update_config(
    request: Request,
    update: GridConfigUpdate,
    username: str = Depends(verify_token),
):
    """Update grid configuration parameters."""
    state = request.app.state
    if not hasattr(state, "settings") or not state.settings:
        return {"error": "Settings not loaded"}

    changes = update.model_dump(exclude_none=True)
    if not changes:
        return {"message": "No changes specified"}

    # Apply changes to settings
    for key, value in changes.items():
        if hasattr(state.settings.grid, key):
            setattr(state.settings.grid, key, value)
        elif hasattr(state.settings.indicators, key):
            setattr(state.settings.indicators, key, value)
        elif hasattr(state.settings.risk, key):
            setattr(state.settings.risk, key, value)

    # Save snapshot if method exists
    if hasattr(state, "db") and state.db:
        if hasattr(state.db, "save_config_snapshot"):
            val = state.settings.to_grid_dict() if hasattr(state.settings, "to_grid_dict") else state.settings.model_dump()
            state.db.save_config_snapshot(val)

    val = state.settings.to_grid_dict() if hasattr(state.settings, "to_grid_dict") else state.settings.model_dump()
    return {
        "message": "Configuration updated",
        "changes": changes,
        "current": val,
    }


@router.post("/config/preview-grid")
async def preview_grid(
    request: Request,
    update: GridConfigUpdate,
    username: str = Depends(verify_token),
):
    """Preview grid levels with given parameters without applying them."""
    state = request.app.state
    price = 0.0

    if hasattr(state, "exchange") and state.exchange:
        try:
            # Fallback if method differs
            if hasattr(state.exchange, "get_ticker_price"):
                price = state.exchange.get_ticker_price()
            elif hasattr(state.exchange, "get_last_price"):
                price = await state.exchange.get_last_price(state.settings.active_symbols[0] if hasattr(state.settings, "active_symbols") else "BTCUSDC")
        except Exception:
            price = 50000.0  # Fallback

    if price <= 0:
        price = 50000.0

    config = update.model_dump(exclude_none=True)
    num_levels = config.get("num_levels", 5)
    spacing = config.get("min_spacing_pct", 0.006)
    order_size = config.get("order_size_usdt", 25)

    buy_levels = [round(price * (1 - spacing * i), 2) for i in range(1, num_levels + 1)]
    sell_levels = [round(price * (1 + spacing * i), 2) for i in range(1, num_levels + 1)]

    return {
        "center_price": price,
        "spacing_pct": spacing,
        "num_levels": num_levels,
        "buy_levels": buy_levels,
        "sell_levels": sell_levels,
        "total_capital_needed": order_size * num_levels,
        "order_size_usdt": order_size,
    }


@router.get("/config/history")
async def get_config_history(request: Request, username: str = Depends(verify_token)):
    """Get configuration change history."""
    state = request.app.state
    if not hasattr(state, "db") or not state.db:
        return {"snapshots": []}

    # Fallback to empty if not implemented
    if hasattr(state.db, "get_config_snapshots"):
        snapshots = state.db.get_config_snapshots(limit=20)
        return {"snapshots": [s.model_dump() if hasattr(s, "model_dump") else s for s in snapshots]}
    return {"snapshots": []}
