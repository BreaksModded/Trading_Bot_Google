"""
FastAPI application — Dashboard API backend.

Serves the REST API, WebSocket endpoints, and static dashboard files.
Includes JWT authentication, CORS, rate limiting, and all route modules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from config.settings import Settings
from core.exchange import BybitExchangeClient
from data.database import Database


# ── Project paths ─────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build FastAPI app with dependency objects attached to `app.state`."""
    settings = settings or Settings()

    db = Database(settings.db_full_path)
    db.init_schema()

    exchange = BybitExchangeClient(
        api_key=settings.exchange.api_key,
        api_secret=settings.exchange.api_secret,
        testnet=settings.exchange.testnet,
        symbol=settings.active_symbols[0],
        timeframe="1",
        domain=settings.exchange.domain,
        tld=settings.exchange.tld,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from api.websocket import manager as ws_manager

        async def broadcast_loop() -> None:
            while True:
                overview_payload = _build_overview_payload(app)
                # M14: broadcast to all connected WebSocket dashboard clients
                await ws_manager.broadcast({"type": "overview", "data": overview_payload})
                await _update_latency(app)
                await asyncio.sleep(5)

        app.state.broadcast_task = asyncio.create_task(
            broadcast_loop(), name="dashboard-broadcast",
        )
        logger.info(
            "Dashboard API started on {}:{}",
            settings.dashboard.host, settings.dashboard.port,
        )
        try:
            yield
        finally:
            task = app.state.broadcast_task
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            db.close()

    app = FastAPI(
        title="Trading Bot Dashboard",
        version="2.0.0",
        lifespan=lifespan,
    )

    # Attach shared state to app.state
    app.state.settings = settings
    app.state.db = db
    app.state.exchange = exchange
    app.state.latest_latency_ms = 0.0
    app.state.broadcast_task = None

    # CORS — M18: configurable origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.dashboard.allowed_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Rate limiting
    from api.middleware import RateLimitMiddleware
    app.add_middleware(
        RateLimitMiddleware,
        requests=60,
        window_seconds=60,
    )

    # Routes — deferred imports to avoid circular dependencies
    from api.middleware import auth_router
    from api.routes.dashboard import router as dashboard_router
    from api.routes.trading import router as trading_router

    app.include_router(auth_router, prefix="/api")
    app.include_router(dashboard_router, prefix="/api")
    app.include_router(trading_router, prefix="/api")

    from api.websocket import router as ws_router
    app.include_router(ws_router)

    # Include additional route modules if they exist
    try:
        from api.routes.backtest import router as backtest_router
        app.include_router(backtest_router, prefix="/api")
    except (ImportError, Exception):
        pass
    try:
        from api.routes.performance import router as perf_router
        app.include_router(perf_router, prefix="/api")
    except (ImportError, Exception):
        pass
    try:
        from api.routes.logs import router as logs_router
        app.include_router(logs_router, prefix="/api")
    except (ImportError, Exception):
        pass
    try:
        from api.routes.config import router as config_router
        app.include_router(config_router, prefix="/api")
    except (ImportError, Exception):
        pass

    # Static files (Dashboard)
    dashboard_dir = PROJECT_ROOT / "dashboard"
    if dashboard_dir.exists():
        if (dashboard_dir / "assets").exists():
            app.mount("/assets", StaticFiles(directory=str(dashboard_dir / "assets")), name="assets")
        if (dashboard_dir / "css").exists():
            app.mount("/css", StaticFiles(directory=str(dashboard_dir / "css")), name="css")
        if (dashboard_dir / "js").exists():
            app.mount("/js", StaticFiles(directory=str(dashboard_dir / "js")), name="js")

        @app.get("/")
        async def serve_dashboard() -> FileResponse:
            return FileResponse(str(dashboard_dir / "index.html"))

    return app


def _build_overview_payload(app: FastAPI) -> dict[str, Any]:
    """Build KPI snapshot for WebSocket broadcast."""
    db: Database = app.state.db
    settings: Settings = app.state.settings

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

    active_symbols = settings.active_symbols
    quote_coin = settings.parse_quote_coin(active_symbols[0]) if active_symbols else settings.quote_coin

    safe_grid_states = [dict(s) for s in grid_states]
    latest_indicators = db.get_runtime_config("latest_indicators") or {}

    return {
        "bot_state": db.get_bot_state(),
        "metrics": db.get_latest_metrics() or {},
        "grid_levels": grid_levels,
        "grid_states": safe_grid_states,
        "latest_trade": next(iter(db.get_recent_trades(limit=1)), None),
        "quote_coin": quote_coin,
        "active_symbols": active_symbols,
        "latest_indicators": latest_indicators,
        "positions": db.get_runtime_config("positions") or {},
    }


async def _update_latency(app: FastAPI) -> None:
    try:
        app.state.latest_latency_ms = await app.state.exchange.ping()
    except Exception:
        app.state.latest_latency_ms = 0.0


# Create the app instance
app = create_app()
