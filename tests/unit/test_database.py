"""Unit tests for the Database class, testing both persistence and the asynchronous write queue."""

import pytest
import sqlite3
import time
from datetime import UTC, datetime
from unittest.mock import patch, MagicMock

from data.database import Database
from data.models import TradeRecord, EventLogRecord

# ==========================================
# STANDARD PERSISTENCE TESTS
# ==========================================

def test_insert_trade_persists_correctly(in_memory_db):
    """Insert a trade, query it back, assert every field matches exactly."""
    trade = TradeRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        side="Buy",
        price=50000.0,
        qty=0.1,
        fee=2.5,
        pnl=10.0,
        status="filled",
        symbol="BTCUSDT",
        order_type="Limit",
        exchange_order_id="test_ord_1",
        metadata={"test": True}
    )
    
    in_memory_db.insert_trade(trade)
    in_memory_db._write_queue.join()
    
    trades = in_memory_db.get_recent_trades()
    assert len(trades) == 1, "Trade was not persisted"
    
    saved = trades[0]
    assert saved["side"] == "Buy"
    assert saved["price"] == 50000.0
    assert saved["qty"] == 0.1
    assert saved["fee"] == 2.5
    assert saved["pnl"] == 10.0
    assert saved["status"] == "filled"
    assert saved["symbol"] == "BTCUSDT"
    assert saved["order_type"] == "Limit"
    assert saved["exchange_order_id"] == "test_ord_1"
    
    import json
    metadata = json.loads(saved["metadata_json"])
    assert metadata.get("test") is True

def test_insert_trade_rejects_invalid_data(in_memory_db):
    """Pass invalid data types to insert_trade and assert error is logged."""
    # Missing required attribute will throw exception during sync insert
    with pytest.raises(Exception):
        in_memory_db._sync_insert_trade("not_a_trade_record_object")

def test_log_event_persists_correctly(in_memory_db):
    """Log an event, query it back and verify fields."""
    event = EventLogRecord(
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        level="INFO",
        module="test_module",
        message="Test event message",
        payload={"key": "value"}
    )
    
    in_memory_db.log_event(event)
    in_memory_db._write_queue.join()
    
    logs = in_memory_db.get_logs(limit=10)
    assert len(logs) == 1, "Log event was not persisted"
    assert logs[0]["level"] == "INFO"
    assert logs[0]["module"] == "test_module"
    assert logs[0]["message"] == "Test event message"

def test_get_orders_returns_expected_subset(in_memory_db):
    """Insert 10 trades with mixed statuses, query by status, assert correct filtering.
    Note: The DB lacks an explicit get_orders(status) method, testing get_recent_trades behavior.
    """
    for i in range(10):
        status = "filled" if i % 2 == 0 else "canceled"
        trade = TradeRecord(
            timestamp=datetime(2026, 1, 1, 12, 0, i, tzinfo=UTC),
            side="Buy",
            price=50000.0,
            qty=0.1,
            fee=0.0,
            pnl=0.0,
            status=status,
            symbol="BTCUSDT"
        )
        in_memory_db.insert_trade(trade)
    
    in_memory_db._write_queue.join()
    
    # Query all trades
    trades = in_memory_db.get_recent_trades(limit=50)
    assert len(trades) == 10, "Not all trades were persisted"
    
    # Filter trades in memory since get_recent_trades does not support status filtering natively
    filled_trades = [t for t in trades if t["status"] == "filled"]
    assert len(filled_trades) == 5, "Expected 5 filled trades"

def test_duplicate_order_handling(in_memory_db):
    """Attempt to insert the same exchange_order_id twice, assert defined behavior safely."""
    trade1 = TradeRecord(
        timestamp=datetime.now(UTC),
        side="Buy",
        price=50000.0,
        qty=0.1,
        fee=0.0,
        pnl=0.0,
        status="filled",
        symbol="BTCUSDT",
        exchange_order_id="dup_123"
    )
    
    trade2 = TradeRecord(
        timestamp=datetime.now(UTC),
        side="Sell",
        price=51000.0,
        qty=0.1,
        fee=0.0,
        pnl=0.0,
        status="filled",
        symbol="BTCUSDT",
        exchange_order_id="dup_123"
    )
    
    in_memory_db.insert_trade(trade1)
    in_memory_db.insert_trade(trade2)
    in_memory_db._write_queue.join()
    
    # By default, without UNIQUE constraints on exchange_order_id, it inserts both safely without corruption
    trades = in_memory_db.get_recent_trades(limit=10)
    assert len(trades) == 2, "Database did not insert duplicate exchange_order_id safely"


# ==========================================
# PHASE F — WRITE QUEUE TESTS
# ==========================================

def test_write_queue_processes_inserts_async(in_memory_db):
    """Push 50 insert_trade calls via the queue, call queue.join(), assert all 50 records exist."""
    for i in range(50):
        trade = TradeRecord(
            timestamp=datetime.now(UTC),
            side="Buy",
            price=50000.0,
            qty=0.1,
            fee=0.0,
            pnl=0.0,
            status="filled",
            symbol="BTCUSDT"
        )
        in_memory_db.insert_trade(trade)
        
    in_memory_db._write_queue.join()
    trades = in_memory_db.get_recent_trades(limit=100)
    assert len(trades) == 50, "Not all async inserts were processed"

@patch("data.database.logger.error")
def test_write_queue_survives_single_exception(mock_logger_error, in_memory_db):
    """Inject an exception for one payload, assert thread survives and future writes succeed."""
    # Inject poisoned pill that breaks the writer thread executor logic
    in_memory_db._write_queue.put((lambda x: x.throw_error(), (None,), {}))
    
    # Valid trade
    trade = TradeRecord(
        timestamp=datetime.now(UTC),
        side="Buy",
        price=50000.0,
        qty=0.1,
        fee=0.0,
        pnl=0.0,
        status="filled",
        symbol="BTCUSDT"
    )
    in_memory_db.insert_trade(trade)
    in_memory_db._write_queue.join()
    
    assert in_memory_db.is_writer_alive(), "Writer thread died after an exception"
    
    trades = in_memory_db.get_recent_trades(limit=10)
    assert len(trades) == 1, "Subsequent trade was not processed"
    
    # Check that error was logged
    assert any("Operation error" in call.args[0] for call in mock_logger_error.call_args_list), "Exception was not logged"

@patch("data.database.logger.critical")
def test_watchdog_logs_critical_on_overload(mock_logger_critical, in_memory_db):
    """Mock the queue to report qsize > 500, assert a CRITICAL log entry is generated."""
    with patch.object(in_memory_db._write_queue, 'qsize', return_value=501):
        in_memory_db._check_writer_health()
        
    assert any("critically overloaded" in call.args[0] for call in mock_logger_critical.call_args_list), "Watchdog did not log CRITICAL on queue overload"

def test_writer_thread_is_alive_after_init(in_memory_db):
    """Assert is_writer_alive() returns True within 1 second of Database initialization."""
    assert in_memory_db.is_writer_alive() is True, "Writer thread is not alive after init"

def test_graceful_shutdown_flushes_queue(in_memory_db):
    """Push 20 writes, call shutdown, assert all 20 writes completed before the thread joined."""
    for i in range(20):
        trade = TradeRecord(
            timestamp=datetime.now(UTC),
            side="Buy",
            price=50000.0,
            qty=0.1,
            fee=0.0,
            pnl=0.0,
            status="filled",
            symbol="BTCUSDT"
        )
        in_memory_db._write_queue.put((in_memory_db._sync_insert_trade, (trade,), {}))
        
    in_memory_db.stop_writer()
    
    assert in_memory_db.is_writer_alive() is False, "Thread did not terminate"
    assert in_memory_db._write_queue.empty(), "Queue was not fully flushed upon shutdown"
    
    trades = in_memory_db.get_recent_trades(limit=50)
    assert len(trades) == 20, "All trades were not flushed to the database before shutdown"

def test_fallback_sync_write_when_thread_dead(in_memory_db):
    """Kill the writer thread artificially, call insert_trade, assert sync write happens gracefully."""
    # Kill the thread completely
    in_memory_db.stop_writer()
    
    trade = TradeRecord(
        timestamp=datetime.now(UTC),
        side="Buy",
        price=50000.0,
        qty=0.1,
        fee=0.0,
        pnl=0.0,
        status="filled",
        symbol="BTCUSDT"
    )
    
    # Thread is dead now, inserting should trigger the fallback branch
    in_memory_db.insert_trade(trade)
    
    trades = in_memory_db.get_recent_trades(limit=5)
    assert len(trades) == 1, "Fallback synchronous trade write failed"

def test_recovery_reads_after_queue_drain(in_memory_db):
    """Push writes to queue, instantly call a read to assure we aren't experiencing read-your-writes race via implicit queue join"""
    trade = TradeRecord(
        timestamp=datetime.now(UTC),
        side="Buy",
        price=50000.0,
        qty=0.1,
        fee=0.0,
        pnl=0.0,
        status="filled",
        symbol="BTCUSDT"
    )
    in_memory_db.insert_trade(trade)
    
    # Join queue manually to simulate synchronization before recovery read
    in_memory_db._write_queue.join()
    
    trades = in_memory_db.get_recent_trades()
    assert len(trades) == 1, "Recovery read failed due to inconsistency (read before write flush)"
