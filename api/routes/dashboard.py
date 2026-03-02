"""Dashboard overview and bot control API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.middleware import verify_token

router = APIRouter()


@router.get("/dashboard/status")
async def get_bot_status(request: Request, username: str = Depends(verify_token)):
    """Get current bot status, KPIs, and overview data."""
    db = request.app.state.db
    settings = request.app.state.settings

    bot_state = db.get_bot_state()
    metrics = db.get_latest_metrics() or {}
    stats = db.get_trade_stats()
    grid_states = db.get_latest_grid_states()
    if not grid_states:
        latest_state = db.get_latest_grid_state()
        if latest_state:
            grid_states = [latest_state]

    grid_levels: list[dict[str, Any]] = []
    for grid_state in grid_states:
        symbol = grid_state.get("symbol", "")
        for level in grid_state.get("levels_json", []):
            row = dict(level)
            row["symbol"] = symbol
            grid_levels.append(row)

    latest_trade = next(iter(db.get_recent_trades(limit=1)), None)
    active_symbols = settings.active_symbols
    quote_coin = settings.parse_quote_coin(active_symbols[0]) if active_symbols else settings.quote_coin

    initial_capital = settings.grid.capital_usdt
    try:
        with db._cursor() as cur:
            cur.execute("SELECT capital FROM equity_curve ORDER BY timestamp ASC LIMIT 1")
            row = cur.fetchone()
            if row:
                row_dict = dict(row)
                if "capital" in row_dict and row_dict["capital"] is not None:
                    initial_capital = float(row_dict["capital"])
    except Exception:
        pass

    daily_loss_pct = 0.0
    pnl_daily = metrics.get("pnl_daily", 0.0)
    equity = metrics.get("equity", 1.0)
    if pnl_daily < 0 and equity > 0:
        daily_loss_pct = (abs(pnl_daily) / equity) * 100.0

    latest_indicators = db.get_runtime_config("latest_indicators") or {}

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "bot_state": bot_state,
        "testnet": settings.exchange.testnet,
        "exchange_latency_ms": getattr(request.app.state, "latest_latency_ms", 0.0),
        "pnl": {
            "total": metrics.get("pnl_total", 0.0),
            "daily": metrics.get("pnl_daily", 0.0),
        },
        "capital": {
            "current": metrics.get("equity", 0.0),
            "initial": initial_capital,
        },
        "drawdown_pct": metrics.get("drawdown_pct", 0.0),
        "daily_loss_pct": daily_loss_pct,
        "win_rate": metrics.get("win_rate", 0.0),
        "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
        "total_trades": stats.get("total_trades", 0),
        "trade_stats": stats,
        "grid_levels": grid_levels,
        "grid_states": grid_states,
        "latest_trade": latest_trade,
        "quote_coin": quote_coin,
        "active_symbols": active_symbols,
        "latest_indicators": latest_indicators,
    }


# ── Bot Control via DB Command Queue ─────────────────────────────────
# The bot runs in a separate process (main.py) and polls the commands
# table. The dashboard writes commands, the bot reads and executes them.


@router.post("/dashboard/bot/start")
async def start_bot(request: Request, username: str = Depends(verify_token)):
    """Enqueue a start command for the bot."""
    db = request.app.state.db
    cmd_id = db.enqueue_command("start")
    return {"status": "command_queued", "command": "start", "command_id": cmd_id}


@router.post("/dashboard/bot/stop")
async def stop_bot(request: Request, username: str = Depends(verify_token)):
    """Enqueue a stop command for the bot."""
    db = request.app.state.db
    cmd_id = db.enqueue_command("stop")
    return {"status": "command_queued", "command": "stop", "command_id": cmd_id}


@router.post("/dashboard/bot/pause")
async def pause_bot(request: Request, username: str = Depends(verify_token)):
    """Enqueue a pause command for the bot."""
    db = request.app.state.db
    cmd_id = db.enqueue_command("pause")
    return {"status": "command_queued", "command": "pause", "command_id": cmd_id}


@router.post("/dashboard/bot/resume")
async def resume_bot(request: Request, username: str = Depends(verify_token)):
    """Enqueue a resume command for the bot."""
    db = request.app.state.db
    cmd_id = db.enqueue_command("resume")
    return {"status": "command_queued", "command": "resume", "command_id": cmd_id}


@router.post("/dashboard/bot/emergency")
async def emergency_stop(request: Request, username: str = Depends(verify_token)):
    """Enqueue an emergency stop command for the bot."""
    db = request.app.state.db
    cmd_id = db.enqueue_command("emergency")
    return {"status": "command_queued", "command": "emergency", "command_id": cmd_id}
