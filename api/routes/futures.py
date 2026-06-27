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
    try:
        stats = db.get_trade_stats()
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
    return {"trades": request.app.state.db.get_recent_trades(limit=limit)}


@router.post("/futures/control")
async def control(body: ControlRequest, request: Request, _: str = Depends(verify_token)) -> dict:
    """Queue a control command for the bot (resume | flatten | stop)."""
    action = body.action.lower().strip()
    if action not in _ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"invalid action: {action}")
    cid = request.app.state.db.enqueue_command(action, {})
    return {"queued": cid, "action": action}
