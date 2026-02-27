"""Event logs and risk management API routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from api.middleware import verify_token
from data.models import EventLevel

router = APIRouter()


@router.get("/logs/events")
async def get_events(
    request: Request,
    level: Optional[str] = Query(None, description="Filter: info, warning, error, critical"),
    module: Optional[str] = Query(None, description="Filter by module"),
    search: Optional[str] = Query(None, description="Search in messages"),
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    username: str = Depends(verify_token),
):
    """Get filtered event logs."""
    state = request.app.state
    if not state.db:
        return {"events": [], "total": 0}

    since = datetime.utcnow() - timedelta(hours=hours)
    ev_level = EventLevel(level) if level else None

    # Fallback missing methods if needed
    get_events_func = getattr(state.db, "get_events", lambda **k: state.db.get_logs(level=level, module=module, search=search, limit=limit))
    events = get_events_func(
        level=ev_level, module=module, since=since,
        limit=limit, offset=offset, search=search,
    )
    
    dict_events = [e.model_dump() if hasattr(e, "model_dump") else e for e in events]

    return {
        "events": dict_events,
        "count": len(events),
        "filters": {"level": level, "module": module, "hours": hours},
    }


@router.get("/logs/circuit-breakers")
async def get_circuit_breaker_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    username: str = Depends(verify_token),
):
    """Get circuit breaker activation history."""
    state = request.app.state
    if not state.db:
        return {"events": []}

    events = state.db.get_circuit_breaker_events(limit=limit)
    return {"events": [e.model_dump() if hasattr(e, "model_dump") else e for e in events]}


@router.get("/logs/risk-status")
async def get_risk_status(request: Request, username: str = Depends(verify_token)):
    """Get current risk management status."""
    state = request.app.state
    if hasattr(state, "risk_manager") and getattr(state, "risk_manager"):
        return state.risk_manager.get_risk_status()
    # Mocking since risk_manager isn't part of app.state currently
    return {
        "is_paused": getattr(state, "risk_paused", False),
        "drawdown_pct": getattr(state, "latest_drawdown", 0.0)
    }
