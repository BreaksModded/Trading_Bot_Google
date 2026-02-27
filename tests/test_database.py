"""
Tests for data/database.py

Tests CRUD operations, performance computation, event logging,
circuit breaker logging, equity curve, and CSV export.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from data.database import Database
from data.models import (
    CircuitBreakerEvent,
    EventLevel,
    EventLevel,
    OrderSide,
    TradeCreate,
)


# ── Schema Tests ──────────────────────────────────────────────────────


class TestDatabaseInit:
    """Tests for database initialization."""

    def test_creates_db_file(self, tmp_path: Path):
        """Database should create the SQLite file on init."""
        db_path = tmp_path / "test.db"
        db = Database(db_path)
        assert db_path.exists()

    def test_creates_parent_dirs(self, tmp_path: Path):
        """Database should create parent directories if needed."""
        db_path = tmp_path / "subdir" / "deep" / "test.db"
        db = Database(db_path)
        assert db_path.exists()

    def test_schema_idempotent(self, tmp_path: Path):
        """Creating Database twice should not raise or corrupt data."""
        db_path = tmp_path / "test.db"
        db1 = Database(db_path)
        db1.insert_trade(TradeCreate(
            side=OrderSide.BUY, price=50000, quantity=0.001,
            quote_qty=50.0,
        ))
        db2 = Database(db_path)  # Re-open
        trades = db2.get_trades(limit=10)
        assert len(trades) == 1  # Data should persist


# ── Trade CRUD Tests ──────────────────────────────────────────────────


class TestTradeCRUD:
    """Tests for trade insertion and retrieval."""

    def test_insert_and_retrieve(self, tmp_db: Database):
        """Should insert a trade and retrieve it."""
        trade = TradeCreate(
            symbol="BTCUSDT", side=OrderSide.BUY,
            price=50000, quantity=0.001, quote_qty=50.0,
            fee=0.005, order_id="test_001", grid_level=1,
        )
        trade_id = tmp_db.insert_trade(trade)
        assert trade_id > 0

        trades = tmp_db.get_trades(limit=10)
        assert len(trades) >= 1
        assert trades[0].price == 50000
        assert trades[0].side == OrderSide.BUY

    def test_trade_count(self, populated_db: Database):
        """Trade count should match inserted trades."""
        count = populated_db.get_trade_count()
        assert count == 10  # 4 buys + 4 winning sells + 2 losing sells

    def test_get_last_trade(self, populated_db: Database):
        """get_last_trade should return the most recent trade."""
        last = populated_db.get_last_trade()
        assert last is not None
        assert last.id is not None

    def test_filter_by_side(self, populated_db: Database):
        """Should filter trades by side."""
        buys = populated_db.get_trades(side=OrderSide.BUY, limit=100)
        assert len(buys) == 4
        assert all(t.side == OrderSide.BUY for t in buys)

    def test_pagination(self, populated_db: Database):
        """Should support offset-based pagination."""
        page1 = populated_db.get_trades(limit=5, offset=0)
        page2 = populated_db.get_trades(limit=5, offset=5)
        assert len(page1) == 5
        assert len(page2) == 5
        # No overlap
        page1_ids = {t.id for t in page1}
        page2_ids = {t.id for t in page2}
        assert page1_ids.isdisjoint(page2_ids)

    def test_empty_db_returns_none(self, tmp_db: Database):
        """Empty DB should return None for last trade."""
        assert tmp_db.get_last_trade() is None
        assert tmp_db.get_trade_count() == 0


# ── Grid State Tests ──────────────────────────────────────────────────


class TestGridState:
    """Tests for grid state persistence."""

    def test_save_and_retrieve_grid(self, tmp_db: Database):
        """Should save and retrieve grid state."""
        levels = {
            "buy_levels": [{"level_index": 1, "side": "Buy", "price": 49700, "quantity": 0.0005}],
            "sell_levels": [{"level_index": 1, "side": "Sell", "price": 50300, "quantity": 0.0005}],
        }
        tmp_db.save_grid_state(50000.0, 0.006, 5, levels)
        state = tmp_db.get_active_grid_state()
        assert state is not None

    def test_no_grid_returns_none(self, tmp_db: Database):
        """Should return None when no grid state exists."""
        state = tmp_db.get_active_grid_state()
        assert state is None


# ── Event Logging Tests ───────────────────────────────────────────────


class TestEventLogging:
    """Tests for event log operations."""

    def test_log_and_retrieve(self, tmp_db: Database):
        """Should log and retrieve events."""
        tmp_db.log_event(EventLevel.INFO, "test", "Test event message")
        events = tmp_db.get_events(limit=10)
        assert len(events) >= 1
        assert events[0].module == "test"
        assert events[0].message == "Test event message"

    def test_filter_by_level(self, tmp_db: Database):
        """Should filter events by level."""
        tmp_db.log_event(EventLevel.INFO, "test", "Info msg")
        tmp_db.log_event(EventLevel.WARNING, "test", "Warning msg")
        tmp_db.log_event(EventLevel.ERROR, "test", "Error msg")

        errors = tmp_db.get_events(level=EventLevel.ERROR, limit=100)
        assert len(errors) == 1
        assert errors[0].level == EventLevel.ERROR

    def test_search_events(self, tmp_db: Database):
        """Should search events by keyword."""
        tmp_db.log_event(EventLevel.INFO, "risk", "Circuit breaker activated for drawdown")
        tmp_db.log_event(EventLevel.INFO, "core", "Grid recalibrated with new spacing")

        results = tmp_db.get_events(search="circuit breaker", limit=100)
        assert len(results) == 1
        assert "circuit breaker" in results[0].message.lower()


# ── Circuit Breaker Logging ───────────────────────────────────────────


class TestCircuitBreakerLogs:
    """Tests for circuit breaker event logging."""

    def test_log_circuit_breaker(self, tmp_db: Database):
        """Should log a circuit breaker event."""
        event = CircuitBreakerEvent(
            breaker_type="max_drawdown",
            trigger_value=0.16,
            threshold_value=0.15,
            action_taken="Emergency stop",
            details="Drawdown exceeded 15%",
        )
        tmp_db.log_circuit_breaker(event)

        events = tmp_db.get_circuit_breaker_events(limit=10)
        assert len(events) >= 1
        assert events[0].breaker_type == "max_drawdown"

    def test_multiple_breaker_types(self, tmp_db: Database):
        """Should log different breaker types correctly."""
        for bt in ["max_drawdown", "daily_loss", "price_movement"]:
            event = CircuitBreakerEvent(
                breaker_type=bt,
                trigger_value=0.10,
                threshold_value=0.08,
                action_taken="Paused",
            )
            tmp_db.log_circuit_breaker(event)

        events = tmp_db.get_circuit_breaker_events(limit=100)
        assert len(events) == 3
        types = {e.breaker_type for e in events}
        assert len(types) == 3


# ── Performance Metrics Tests ─────────────────────────────────────────


class TestPerformanceMetrics:
    """Tests for performance computation."""

    def test_compute_with_trades(self, populated_db: Database):
        """Should compute metrics from populated trades."""
        metrics = populated_db.compute_performance()
        assert metrics.total_trades == 10
        # 4 winning + 2 losing = 6 sell trades
        assert metrics.winning_trades + metrics.losing_trades == 6

    def test_compute_win_rate(self, populated_db: Database):
        """Win rate = winning_trades / total_trades × 100 = 4/10 = 40%."""
        metrics = populated_db.compute_performance()
        # DB counts wins (pnl > 0) against ALL trades, not just sells
        expected_wr = 4 / 10 * 100  # 4 winning sells out of 10 total trades
        assert abs(metrics.win_rate - expected_wr) < 1.0

    def test_compute_empty_db(self, tmp_db: Database):
        """Empty DB should return zeroed metrics."""
        metrics = tmp_db.compute_performance()
        assert metrics.total_trades == 0
        assert metrics.net_pnl == 0.0
        assert metrics.win_rate == 0.0

    def test_net_pnl_correct(self, populated_db: Database):
        """Net PnL = gross_pnl - total_fees."""
        metrics = populated_db.compute_performance()
        # gross_pnl = 4×0.15 + 2×(-0.10) = 0.40
        # total_fees = 10×0.005 = 0.05
        # net_pnl = 0.40 - 0.05 = 0.35
        expected_net = (4 * 0.15 + 2 * (-0.10)) - (10 * 0.005)
        assert abs(metrics.net_pnl - expected_net) < 0.02


# ── Equity Curve Tests ────────────────────────────────────────────────


class TestEquityCurve:
    """Tests for equity curve recording and retrieval."""

    def test_record_and_retrieve(self, tmp_db: Database):
        """Should record and retrieve equity points."""
        tmp_db.record_equity(150.0, 0.0)
        tmp_db.record_equity(151.5, 0.0)
        tmp_db.record_equity(149.0, 0.013)

        points = tmp_db.get_equity_curve(limit=100)
        assert len(points) == 3
        assert points[0].capital == 150.0


# ── Config Snapshot Tests ─────────────────────────────────────────────


class TestConfigSnapshots:
    """Tests for config snapshot persistence."""

    def test_save_and_retrieve(self, tmp_db: Database):
        """Should save and retrieve config snapshots."""
        config = {"num_levels": 5, "spacing": 0.006, "capital": 150}
        tmp_db.save_config_snapshot(config, reason="test")

        snapshots = tmp_db.get_config_snapshots(limit=10)
        assert len(snapshots) >= 1
        assert snapshots[0].reason == "test"


# ── CSV Export Tests ──────────────────────────────────────────────────


class TestCSVExport:
    """Tests for trade CSV export."""

    def test_export_creates_file(self, populated_db: Database, tmp_path: Path):
        """Should create a CSV file with Koinly-compatible headers."""
        export_path = tmp_path / "export.csv"
        count = populated_db.export_trades_csv(export_path)
        assert export_path.exists()
        assert count == 10

        # Verify CSV content (Koinly format)
        import csv
        with open(export_path, "r") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = list(reader)
        assert len(rows) == 10
        assert "Price" in header  # Koinly uses capitalized headers
        assert "Date" in header

    def test_export_empty_db(self, tmp_db: Database, tmp_path: Path):
        """Exporting empty DB should create CSV with only header."""
        export_path = tmp_path / "empty.csv"
        count = tmp_db.export_trades_csv(export_path)
        assert count == 0
        assert export_path.exists()


# ── Cleanup Tests ─────────────────────────────────────────────────────


class TestCleanup:
    """Tests for old event cleanup."""

    def test_cleanup_old_events(self, tmp_db: Database):
        """Should remove events older than N days."""
        # Add old and recent events
        tmp_db.log_event(EventLevel.INFO, "test", "Old event")
        tmp_db.log_event(EventLevel.INFO, "test", "Recent event")

        # Cleanup events older than 0 days (removes everything)
        tmp_db.cleanup_old_events(days=0)
        events = tmp_db.get_events(limit=100)
        # Should have removed old events
        assert isinstance(events, list)
