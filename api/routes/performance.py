"""Performance metrics API routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query, Request
from api.middleware import verify_token

router = APIRouter()


@router.get("/performance/metrics")
async def get_performance_metrics(
    request: Request,
    period: str = Query("all", description="Period: 24h, 7d, 30d, 90d, all"),
    username: str = Depends(verify_token),
):
    """Get aggregated performance metrics for a period."""
    state = request.app.state
    if not state.db:
        return {}

    since = None
    period_map = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}
    if period in period_map:
        since = datetime.utcnow() - timedelta(days=period_map[period])

    # Trade-level stats — includes profit_factor, win_rate, gross_profit/loss
    trade_stats = state.db.get_trade_stats(since=since)
    # Latest equity snapshot — for drawdown_pct and sharpe_ratio (not derivable from trades)
    latest = state.db.get_latest_metrics() or {}

    return {
        "period": period,
        "total_trades": trade_stats.get("total_trades", 0),
        "total_executions": trade_stats.get("total_executions", 0),
        "winners": trade_stats.get("winners", 0),
        "losers": trade_stats.get("losers", 0),
        "win_rate": trade_stats.get("win_rate", 0.0),
        "pnl_total": trade_stats.get("total_pnl", 0.0),
        "pnl_daily": trade_stats.get("pnl_24h", 0.0),
        "average_pnl": trade_stats.get("average_pnl", 0.0),
        "gross_profit": trade_stats.get("gross_profit", 0.0),
        "gross_loss": trade_stats.get("gross_loss", 0.0),
        "profit_factor": trade_stats.get("profit_factor", 0.0),
        "drawdown_pct": latest.get("drawdown_pct", 0.0),
        "sharpe_ratio": latest.get("sharpe_ratio", 0.0),
        "equity": latest.get("equity", 0.0),
    }


@router.get("/performance/equity")
async def get_equity_curve(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    username: str = Depends(verify_token),
):
    """Get equity curve data for charting."""
    state = request.app.state
    if not state.db:
        return {"points": []}

    # using limit mapping since get_equity_curve accepts limit not since
    points = state.db.get_equity_curve(limit=days * 24) 
    return {
        "points": [{"timestamp": p["timestamp"], "capital": p["capital"], "drawdown": p["drawdown_pct"]} for p in points],
        "count": len(points),
    }


@router.get("/performance/daily-pnl")
async def get_daily_pnl(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    username: str = Depends(verify_token),
):
    """Get daily PnL data for bar chart."""
    state = request.app.state
    if not state.db:
        return {"data": []}

    data = state.db.get_daily_pnl(limit=days)
    return {"data": data, "count": len(data)}


@router.get("/performance/export")
async def export_trades_csv(request: Request, username: str = Depends(verify_token)):
    """Export trades to CSV for tax reporting."""
    from pathlib import Path
    from fastapi.responses import FileResponse

    state = request.app.state
    if not state.db:
        return {"error": "Database not available"}

    export_path = Path("data") / f"trades_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    state.db.export_trades_to_csv(export_path)
    count = state.db.get_trade_count()

    return FileResponse(
        path=str(export_path),
        filename=export_path.name,
        media_type="text/csv",
        headers={"X-Trade-Count": str(count)},
    )
