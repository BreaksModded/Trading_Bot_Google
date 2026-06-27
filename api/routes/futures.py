"""Futures dashboard API — consolidated, auth-protected endpoints.

The bot (a separate process) persists its full state into runtime_config
('futures_state', 'futures_risk_status') and the trades / equity_curve tables;
these endpoints read that and serve it to the dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from api.middleware import verify_token

router = APIRouter()

_ALLOWED_ACTIONS = {"resume", "flatten", "stop"}


class ControlRequest(BaseModel):
    action: str


@router.get("/futures/overview")
async def overview(request: Request, _: str = Depends(verify_token)) -> dict:
    """Everything the dashboard needs in one read: state, risk, bot, stats."""
    db = request.app.state.db
    symbol = request.app.state.settings.futures.symbol
    try:
        stats = db.get_trade_stats(symbol=symbol)
    except Exception:
        stats = {}
    return {
        "state": db.get_runtime_config("futures_state") or {},
        "risk": db.get_runtime_config("futures_risk_status") or {},
        "bot": db.get_bot_state() or {},
        "stats": stats,
    }


@router.get("/futures/equity")
async def equity(request: Request, limit: int = 300, _: str = Depends(verify_token)) -> dict:
    return {"points": request.app.state.db.get_equity_curve(limit=limit)}


@router.get("/futures/trades")
async def trades(request: Request, limit: int = 50, _: str = Depends(verify_token)) -> dict:
    symbol = request.app.state.settings.futures.symbol
    return {"trades": request.app.state.db.get_recent_trades(limit=limit, symbol=symbol)}


@router.get("/futures/config")
async def config(request: Request, _: str = Depends(verify_token)) -> dict:
    """Read-only snapshot of the live futures strategy parameters (Config screen).

    The dashboard runs in a SEPARATE process from the bot, so these are the values
    the bot booted with (env / .env). Editing is intentionally not supported in v1:
    applying changes to a running, separate bot process would need a config-reload
    channel, and that would touch the trading cycle (out of scope). The UI renders
    these read-only and states the reason.
    """
    s = request.app.state.settings.futures
    return {
        "editable": False,
        "reason": (
            "El bot se ejecuta en un proceso separado del panel. Aplicar cambios "
            "exigiría recargar la configuración del bot en caliente, lo que tocaría "
            "el ciclo de trading (fuera del alcance de esta versión). Estos valores "
            "son los que el bot cargó al arrancar."
        ),
        "values": {
            "symbol": s.symbol,
            "timeframe": s.timeframe,
            "higher_timeframe": s.higher_timeframe,
            "require_higher_tf_confirmation": s.require_higher_tf_confirmation,
            "adx_trend_threshold": s.adx_trend_threshold,
            "adx_range_threshold": s.adx_range_threshold,
            "ema_fast": s.ema_fast,
            "ema_slow": s.ema_slow,
            "atr_period": s.atr_period,
            "leverage": s.leverage,
            "risk_per_trade_pct": s.risk_per_trade_pct,
            "min_order_usdt": s.min_order_usdt,
            "capital_fraction": s.capital_fraction,
            "grid_levels": s.grid_levels,
            "chandelier_period": s.chandelier_period,
            "chandelier_atr_mult": s.chandelier_atr_mult,
            "max_daily_loss_pct": s.max_daily_loss_pct,
            "max_total_drawdown_pct": s.max_total_drawdown_pct,
            "stop_loss_pct": s.stop_loss_pct,
            "min_liquidation_buffer_pct": s.min_liquidation_buffer_pct,
            "loop_interval_seconds": s.loop_interval_seconds,
        },
    }


@router.post("/futures/control")
async def control(body: ControlRequest, request: Request, _: str = Depends(verify_token)) -> dict:
    """Queue a control command for the bot (resume | flatten | stop)."""
    action = body.action.lower().strip()
    if action not in _ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"invalid action: {action}")
    cid = request.app.state.db.enqueue_command(action, {})
    return {"queued": cid, "action": action}
