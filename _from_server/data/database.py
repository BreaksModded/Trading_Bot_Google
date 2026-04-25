"""SQLite persistence layer for trading data, state, and control commands."""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import queue
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from data.models import (
    CircuitBreakerEvent,
    ConfigSnapshot,
    EventLogRecord,
    GridLevel,
    GridState,
    PerformanceMetrics,
    TradeRecord,
)


class Database:
    """Thread-safe SQLite helper for bot runtime and API queries."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()

        self._write_queue = queue.Queue()
        self._write_thread = threading.Thread(
            target=self._background_writer,
            daemon=True,
            name="db-write-worker"
        )
        self._write_thread.start()

    def _background_writer(self) -> None:
        """Dedicated thread that consumes the write queue."""
        while True:
            try:
                payload = self._write_queue.get(timeout=5.0)
                if payload is None:  # sentinel for graceful shutdown
                    self._write_queue.task_done()
                    break
                operation, args, kwargs = payload
                try:
                    operation(*args, **kwargs)
                except Exception as op_err:
                    logger.error(f"[DB Writer] Operation error: {op_err}", exc_info=True)
                finally:
                    self._write_queue.task_done()
            except queue.Empty:
                self._check_writer_health()
            except Exception as e:
                logger.error(
                    f"[DB Writer] Unhandled error in background writer: {e}",
                    exc_info=True
                )

    def _check_writer_health(self) -> None:
        qsize = self._write_queue.qsize()
        if qsize > 500:
            logger.critical(
                f"[DB Writer] Write queue critically overloaded: {qsize} pending items. "
                f"Possible writer thread stall."
            )
        elif qsize > 100:
            logger.warning(f"[DB Writer] Write queue backlog growing: {qsize} items pending.")

    def is_writer_alive(self) -> bool:
        return self._write_thread.is_alive()

    def stop_writer(self) -> None:
        """Gracefully shutdown background writer."""
        self._write_queue.put(None)
        self._write_thread.join(timeout=10.0)
        if self._write_thread.is_alive():
            logger.warning("[DB Writer] Worker thread failed to join within timeout.")

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        with self._lock:
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()

    # ── Schema ────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Create all required tables and indexes."""
        with self._cursor() as cur:
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    symbol           TEXT NOT NULL DEFAULT 'BTCUSDC',
                    side             TEXT NOT NULL,
                    price            REAL NOT NULL,
                    qty              REAL NOT NULL,
                    fee              REAL NOT NULL DEFAULT 0.0,
                    pnl              REAL NOT NULL DEFAULT 0.0,
                    status           TEXT NOT NULL DEFAULT 'filled',
                    order_type       TEXT NOT NULL DEFAULT 'Limit',
                    exchange_order_id TEXT,
                    metadata_json    TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS grid_states (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    symbol           TEXT NOT NULL DEFAULT 'BTCUSDC',
                    spacing_pct      REAL NOT NULL,
                    trend_bias       TEXT NOT NULL DEFAULT 'neutral',
                    levels_json      TEXT NOT NULL DEFAULT '[]',
                    is_active        INTEGER NOT NULL DEFAULT 1,
                    grid_created_at  TEXT,
                    grid_anchor_price REAL,
                    pending_retries_json TEXT NOT NULL DEFAULT '[]',
                    position_qty     REAL NOT NULL DEFAULT 0.0,
                    avg_cost         REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS performance_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    equity           REAL NOT NULL DEFAULT 0.0,
                    pnl_total        REAL NOT NULL DEFAULT 0.0,
                    pnl_daily        REAL NOT NULL DEFAULT 0.0,
                    drawdown_pct     REAL NOT NULL DEFAULT 0.0,
                    win_rate         REAL NOT NULL DEFAULT 0.0,
                    sharpe_ratio     REAL NOT NULL DEFAULT 0.0,
                    total_trades     INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS event_logs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    level            TEXT NOT NULL DEFAULT 'INFO',
                    module           TEXT NOT NULL DEFAULT '',
                    message          TEXT NOT NULL DEFAULT '',
                    payload_json     TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS circuit_breaker_events (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    breaker_type     TEXT NOT NULL,
                    trigger_value    REAL NOT NULL,
                    threshold        REAL NOT NULL,
                    action_taken     TEXT NOT NULL,
                    details          TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS config_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    config_json      TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS equity_curve (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL DEFAULT (datetime('now')),
                    capital          REAL NOT NULL,
                    drawdown_pct     REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    id               INTEGER PRIMARY KEY CHECK (id = 1),
                    status           TEXT NOT NULL DEFAULT 'stopped',
                    message          TEXT NOT NULL DEFAULT '',
                    started_at       TEXT,
                    paused_until     TEXT,
                    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS commands (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    command          TEXT NOT NULL,
                    payload_json     TEXT NOT NULL DEFAULT '{}',
                    status           TEXT NOT NULL DEFAULT 'pending',
                    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
                    processed_at     TEXT
                );

                CREATE TABLE IF NOT EXISTS runtime_config (
                    key              TEXT PRIMARY KEY,
                    value_json       TEXT NOT NULL DEFAULT 'null',
                    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
                );

                -- Indexes
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_event_logs_level ON event_logs(level);
                CREATE INDEX IF NOT EXISTS idx_event_logs_timestamp ON event_logs(timestamp);
                CREATE INDEX IF NOT EXISTS idx_equity_curve_timestamp ON equity_curve(timestamp);
                CREATE INDEX IF NOT EXISTS idx_grid_states_symbol ON grid_states(symbol);
                CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
            """)

            # Add missing columns for older databases
            migrations = [
                "ALTER TABLE event_logs ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE performance_snapshots ADD COLUMN equity REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN pnl_total REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN pnl_daily REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN drawdown_pct REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN win_rate REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN sharpe_ratio REAL NOT NULL DEFAULT 0.0",
                "ALTER TABLE performance_snapshots ADD COLUMN total_trades INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE performance_snapshots ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE grid_states ADD COLUMN grid_created_at TEXT",
                "ALTER TABLE grid_states ADD COLUMN grid_anchor_price REAL",
                "ALTER TABLE grid_states ADD COLUMN pending_retries_json TEXT NOT NULL DEFAULT '[]'"
            ]
            for mig in migrations:
                try:
                    cur.execute(mig)
                except sqlite3.OperationalError:
                    pass # Column already exists

            # Ensure singleton bot_state row
            cur.execute(
                "INSERT OR IGNORE INTO bot_state (id, status) VALUES (1, 'stopped')"
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(UTC).isoformat()

    # ── Trades ────────────────────────────────────────────────────────

    def _sync_insert_trade(self, trade: TradeRecord) -> int:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO trades
                   (timestamp, symbol, side, price, qty, fee, pnl, status,
                    order_type, exchange_order_id, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.timestamp.isoformat(),
                    trade.symbol,
                    trade.side,
                    trade.price,
                    trade.qty,
                    trade.fee,
                    trade.pnl,
                    trade.status,
                    trade.order_type,
                    trade.exchange_order_id,
                    json.dumps(trade.metadata, default=str),
                ),
            )
            return cur.lastrowid or 0

    def insert_trade(self, trade: TradeRecord) -> int:
        if not self.is_writer_alive():
            logger.critical("[DB Writer] Write thread dead, falling back to sync insert_trade.")
            return self._sync_insert_trade(trade)
            
        self._write_queue.put((self._sync_insert_trade, (trade,), {}))
        return 0

    def get_recent_trades(self, limit: int = 100, offset: int = 0, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            if symbol:
                cur.execute(
                    "SELECT * FROM trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (symbol, limit, offset),
                )
            else:
                cur.execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [dict(row) for row in cur.fetchall()]

    def get_trade_count(self, since: datetime | None = None, symbol: str | None = None) -> int:
        with self._cursor() as cur:
            clauses = []
            params = []
            if since:
                clauses.append("timestamp >= ?")
                params.append(since.isoformat())
            if symbol:
                clauses.append("symbol = ?")
                params.append(symbol)
                
            if clauses:
                where_sql = " WHERE " + " AND ".join(clauses)
                cur.execute(f"SELECT COUNT(*) FROM trades{where_sql}", tuple(params))
            else:
                cur.execute("SELECT COUNT(*) FROM trades")
            return cur.fetchone()[0]

    def export_trades_to_csv(self, output_path: Path) -> None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM trades ORDER BY timestamp")
            rows = cur.fetchall()
        if not rows:
            return
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(rows[0].keys())
            for row in rows:
                writer.writerow(tuple(row))

    # ── Grid State ────────────────────────────────────────────────────

    def save_grid_state(self, state: GridState) -> None:
        levels_data = [
            {"level_id": lv.level_id, "price": lv.price, "side": lv.side,
             "qty": lv.qty, "status": lv.status}
            for lv in state.levels
        ]
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO grid_states
                   (timestamp, symbol, spacing_pct, trend_bias, levels_json, is_active, grid_created_at, grid_anchor_price, pending_retries_json, position_qty, avg_cost)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    state.symbol,
                    state.spacing_pct,
                    state.trend_bias,
                    json.dumps(levels_data, default=str),
                    state.grid_created_at.isoformat() if state.grid_created_at else None,
                    state.grid_anchor_price,
                    json.dumps(state.pending_retries, default=str),
                    state.position_qty,
                    state.avg_cost,
                ),
            )

    def get_latest_grid_state(self) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM grid_states ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._parse_grid_row(dict(row))

    def get_latest_grid_states(self) -> list[dict[str, Any]]:
        """Return latest grid snapshot per symbol."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT g.* FROM grid_states g
                INNER JOIN (
                    SELECT symbol, MAX(id) AS max_id FROM grid_states GROUP BY symbol
                ) latest ON g.id = latest.max_id
                ORDER BY g.symbol
            """)
            return [self._parse_grid_row(dict(row)) for row in cur.fetchall()]

    @staticmethod
    def _parse_grid_row(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("levels_json", "[]")
        if isinstance(raw, str):
            row["levels_json"] = json.loads(raw)
        
        # Parse timestamp safely
        created_at_str = row.get("grid_created_at")
        if created_at_str:
            try:
                row["grid_created_at"] = datetime.fromisoformat(created_at_str)
            except ValueError:
                row["grid_created_at"] = None
        else:
            row["grid_created_at"] = None
            
        raw_retries = row.get("pending_retries_json")
        if isinstance(raw_retries, str):
            try:
                row["pending_retries"] = json.loads(raw_retries)
            except json.JSONDecodeError:
                row["pending_retries"] = []
        else:
            row["pending_retries"] = []
            
        row["position_qty"] = float(row.get("position_qty") or 0.0)
        row["avg_cost"] = float(row.get("avg_cost") or 0.0)
            
        return row

    def get_day_start_equity(self, target_date: date) -> float | None:
        """Fetch the first recorded equity for a specific UTC date."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT equity FROM performance_snapshots WHERE date(timestamp) = ? ORDER BY timestamp ASC LIMIT 1",
                (target_date.isoformat(),),
            )
            row = cur.fetchone()
            if row:
                return float(row["equity"])
            return None

    # ── Performance Metrics ───────────────────────────────────────────

    def insert_metrics(self, metrics: PerformanceMetrics) -> None:
        with self._cursor() as cur:
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
                    json.dumps(getattr(metrics, "values", {}), default=str),
                ),
            )

    def get_latest_metrics(self) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM performance_snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_equity_curve(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM equity_curve ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in reversed(cur.fetchall())]

    def record_equity(self, capital: float, drawdown_pct: float = 0.0) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO equity_curve (timestamp, capital, drawdown_pct) VALUES (?, ?, ?)",
                (self._utc_now(), capital, drawdown_pct),
            )

    def get_daily_pnl(self, limit: int = 90) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT DATE(timestamp) AS date,
                       SUM(pnl) AS pnl,
                       COUNT(*) AS trades_count
                FROM trades
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in reversed(cur.fetchall())]

    def get_trade_stats(self, since: datetime | None = None) -> dict[str, Any]:
        """Compute aggregate trade statistics with profit factor.

        Separates closed trades (pnl ≠ 0) from raw executions.
        """
        where_sql = "WHERE timestamp >= ?" if since else ""
        params: tuple[Any, ...] = (since.isoformat(),) if since else ()
        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_executions,
                    SUM(CASE WHEN ABS(pnl) > 1e-12 THEN 1 ELSE 0 END) AS total_closed_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS losers,
                    AVG(CASE WHEN ABS(pnl) > 1e-12 THEN pnl END) AS avg_pnl,
                    SUM(pnl) AS total_pnl,
                    SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_profit,
                    ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)) AS gross_loss
                FROM trades
                {where_sql}
                """,
                params,
            )
            row = cur.fetchone()

        total_executions = int(row["total_executions"] or 0)
        total_closed_trades = int(row["total_closed_trades"] or 0)
        winners = int(row["winners"] or 0)
        losers = int(row["losers"] or 0)
        avg_pnl = float(row["avg_pnl"] or 0.0)
        total_pnl = float(row["total_pnl"] or 0.0)
        win_rate = (winners / total_closed_trades * 100.0) if total_closed_trades else 0.0
        gross_profit = float(row["gross_profit"] or 0.0)
        gross_loss = float(row["gross_loss"] or 0.0)
        profit_factor = gross_profit / gross_loss if gross_loss else 0.0

        # 24h PnL for daily summary
        pnl_24h = 0.0
        try:
            from datetime import timedelta as _td
            since_24h = datetime.now(UTC) - _td(hours=24)
            with self._cursor() as cur2:
                cur2.execute(
                    "SELECT SUM(pnl) FROM trades WHERE timestamp >= ?",
                    (since_24h.isoformat(),),
                )
                r = cur2.fetchone()
                pnl_24h = float(r[0] or 0.0) if r else 0.0
        except Exception as exc:
            logger.opt(exception=exc).error("get_trade_stats: failed to compute pnl_24h: {}", exc)

        return {
            "total_trades": total_closed_trades,
            "total_closed_trades": total_closed_trades,
            "total_executions": total_executions,
            "winners": winners,
            "losers": losers,
            "win_rate": win_rate,
            "average_pnl": avg_pnl,
            "total_pnl": total_pnl,
            "pnl_24h": pnl_24h,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
        }

    # ── Event Logs ────────────────────────────────────────────────────

    def _sync_log_event(self, event: EventLogRecord) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO event_logs
                   (timestamp, level, module, message, payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    event.timestamp.isoformat(),
                    event.level,
                    event.module,
                    event.message,
                    json.dumps(event.payload, default=str),
                ),
            )

    def log_event(self, event: EventLogRecord) -> None:
        # In-memory queue. A SIGKILL or OOM kill can lose queued-but-not-written records.
        # This is acceptable for log_event entries.
        self._write_queue.put((self._sync_log_event, (event,), {}))

    def get_logs(
        self,
        *,
        limit: int = 500,
        level: str | None = None,
        module: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if module:
            clauses.append("module = ?")
            params.append(module)
        if search:
            clauses.append("message LIKE ?")
            params.append(f"%{search}%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM event_logs {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            )
            return [dict(row) for row in cur.fetchall()]

    # ── Circuit Breaker Events ────────────────────────────────────────

    def log_circuit_breaker(self, event: CircuitBreakerEvent) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO circuit_breaker_events
                   (timestamp, breaker_type, trigger_value, threshold,
                    action_taken, details)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.timestamp.isoformat(),
                    event.breaker_type,
                    event.trigger_value,
                    event.threshold,
                    event.action_taken,
                    event.details,
                ),
            )

    def get_circuit_breaker_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM circuit_breaker_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    # ── Config Snapshots ──────────────────────────────────────────────

    def save_config_snapshot(self, snapshot: ConfigSnapshot) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO config_snapshots (timestamp, config_json) VALUES (?, ?)",
                (snapshot.timestamp.isoformat(), json.dumps(snapshot.values, default=str)),
            )

    def get_latest_config_snapshot(self) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM config_snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            result = dict(row)
            raw = result.get("config_json", "{}")
            if isinstance(raw, str):
                result["config_json"] = json.loads(raw)
            return result

    # ── Bot State (singleton row) ─────────────────────────────────────

    def update_bot_state(
        self,
        *,
        status: str,
        message: str = "",
        started_at: datetime | None = None,
        paused_until: datetime | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """UPDATE bot_state SET
                       status = ?,
                       message = ?,
                       started_at = COALESCE(?, started_at),
                       paused_until = ?,
                       updated_at = ?
                   WHERE id = 1""",
                (
                    status,
                    message,
                    started_at.isoformat() if started_at else None,
                    paused_until.isoformat() if paused_until else None,
                    self._utc_now(),
                ),
            )

    def get_bot_state(self) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM bot_state WHERE id = 1")
            row = cur.fetchone()
            if row:
                return dict(row)
        return {"status": "stopped", "message": "", "started_at": None, "paused_until": None}

    # ── Command Queue ─────────────────────────────────────────────────

    def enqueue_command(self, command: str, payload: dict[str, Any] | None = None) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO commands (command, payload_json) VALUES (?, ?)",
                (command, json.dumps(payload or {})),
            )
            return cur.lastrowid or 0

    def fetch_pending_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM commands WHERE status = 'pending' ORDER BY id LIMIT ?",
                (limit,),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                raw = row.get("payload_json", "{}")
                if isinstance(raw, str):
                    row["payload_json"] = json.loads(raw)
            return rows

    def mark_command_processed(self, command_id: int, status: str = "processed") -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE commands SET status = ?, processed_at = ? WHERE id = ?",
                (status, self._utc_now(), command_id),
            )

    # ── Runtime Config (key-value store) ──────────────────────────────

    def set_runtime_config(self, key: str, value: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO runtime_config (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json = ?, updated_at = ?""",
                (key, json.dumps(value), self._utc_now(),
                 json.dumps(value), self._utc_now()),
            )

    def get_runtime_config(self, key: str) -> Any:
        with self._cursor() as cur:
            cur.execute("SELECT value_json FROM runtime_config WHERE key = ?", (key,))
            row = cur.fetchone()
            return json.loads(row["value_json"]) if row else None

    def get_all_runtime_config(self) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute("SELECT key, value_json FROM runtime_config")
            return {row["key"]: json.loads(row["value_json"]) for row in cur.fetchall()}

    # ── Cleanup ───────────────────────────────────────────────────────

    def cleanup_old_events(self, days: int = 90) -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM event_logs WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
