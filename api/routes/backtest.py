"""Backtesting API routes."""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel, Field
from api.middleware import verify_token

router = APIRouter()

# In-memory backtest state
_backtest_state = {"running": False, "progress": 0, "results": None, "error": None}


class BacktestRequest(BaseModel):
    """Parameters for running a backtest."""
    months: int = Field(6, ge=1, le=24)
    timeframe: str = Field("1h", description="1m, 5m, 15m, 1h, 4h")
    num_levels: int = Field(5, ge=2, le=10)
    min_spacing_pct: float = Field(0.006, ge=0.003, le=0.05)
    atr_multiplier: float = Field(1.5, ge=0.5, le=5.0)
    order_size_usdt: float = Field(25, ge=5, le=500)
    adx_threshold: float = Field(25, ge=15, le=50)
    ema_fast: int = Field(50, ge=5, le=100)
    ema_slow: int = Field(200, ge=50, le=500)
    initial_capital: float = Field(150, ge=10)
    walk_forward: bool = Field(False, description="Enable walk-forward testing")


def _run_backtest(request: BacktestRequest) -> None:
    """Background task to run a backtest."""
    global _backtest_state
    _backtest_state = {"running": True, "progress": 0, "results": None, "error": None}

    try:
        from backtesting.data_loader import download_months
        from backtesting.engine import BacktestEngine
        from backtesting.reporter import generate_report

        # Download data
        _backtest_state["progress"] = 5
        df = download_months(months=request.months, timeframe=request.timeframe)
        _backtest_state["progress"] = 20

        config = {
            "num_levels": request.num_levels,
            "min_spacing_pct": request.min_spacing_pct,
            "atr_multiplier": request.atr_multiplier,
            "order_size_usdt": request.order_size_usdt,
            "adx_threshold": request.adx_threshold,
            "ema_fast": request.ema_fast,
            "ema_slow": request.ema_slow,
        }

        engine = BacktestEngine(initial_capital=request.initial_capital)

        def on_progress(pct):
            _backtest_state["progress"] = 20 + int(pct * 0.7)

        if request.walk_forward:
            results = engine.walk_forward(df, config)
            _backtest_state["progress"] = 95
            report = {
                "train": generate_report(results["train"]),
                "test": generate_report(results["test"]),
                "train_period": results["train_period"],
                "test_period": results["test_period"],
            }
        else:
            raw_results = engine.run(df, config, progress_callback=on_progress)
            _backtest_state["progress"] = 95
            report = generate_report(raw_results)

        _backtest_state["results"] = report
        _backtest_state["progress"] = 100
        _backtest_state["running"] = False

    except Exception as e:
        _backtest_state["error"] = str(e)
        _backtest_state["running"] = False


@router.post("/backtest/run")
async def run_backtest(
    request: BacktestRequest,
    background_tasks: BackgroundTasks,
    username: str = Depends(verify_token),
):
    """Start a backtest in the background."""
    if _backtest_state["running"]:
        return {"error": "A backtest is already running"}

    background_tasks.add_task(_run_backtest, request)
    return {"message": "Backtest started", "status": "running"}


@router.get("/backtest/status")
async def get_backtest_status(username: str = Depends(verify_token)):
    """Get current backtest progress."""
    return _backtest_state


@router.get("/backtest/results")
async def get_backtest_results(username: str = Depends(verify_token)):
    """Get backtest results (after completion)."""
    if _backtest_state["running"]:
        return {"status": "running", "progress": _backtest_state["progress"]}
    return _backtest_state.get("results") or {"error": "No results available"}
