import sqlite3
import json
from datetime import datetime, UTC
from data.models import PerformanceMetrics
from data.database import Database

db = Database("data/trading_bot.db")
print("Writing metrics...")
metrics = PerformanceMetrics(
    timestamp=datetime.now(UTC),
    equity=100.0,
    pnl_total=1.0,
    pnl_daily=0.5,
    drawdown_pct=0.1,
    win_rate=50.0,
    sharpe_ratio=1.0,
    total_trades=10
)

# Simulate the exact insert statement from database.py
with db._cursor() as cur:
    val = getattr(metrics, "values", {})
    json_val = json.dumps(val, default=str)
    print("Value going into SQL:", repr(json_val), "Type:", type(json_val))
    try:
        cur.execute(
            """INSERT INTO performance_snapshots
               (timestamp, equity, pnl_total, pnl_daily, drawdown_pct,
                win_rate, sharpe_ratio, total_trades, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                metrics.timestamp.isoformat(),
                metrics.equity,
                metrics.pnl_total,
                metrics.pnl_daily,
                metrics.drawdown_pct,
                metrics.win_rate,
                metrics.sharpe_ratio,
                metrics.total_trades,
                json_val,
            ),
        )
        print("Success!")
    except Exception as e:
        print("Exception:", str(e))
