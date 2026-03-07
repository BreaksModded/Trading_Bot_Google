"""
Tests for FIX-4: avg_cost Reconstruction with Partial Sells (BUG-5).
"""
import pytest
from main import _reconstruct_avg_cost
from datetime import datetime, timedelta, UTC

def test_reconstruct_avg_cost_with_partial_sells():
    """
    BUG-5 Fix: Should not cut history at the first SELL.
    Should calculate cumulative avg_cost across partial sells.
    
    Scenario:
    1. BUY 1.0 @ 100
    2. BUY 1.0 @ 200 (Avg = 150, Qty = 2.0)
    3. SELL 0.5 @ 250 (Avg remains 150, Qty = 1.5)
    4. BUY 0.5 @ 300 (Avg = (150*1.5 + 300*0.5)/2.0 = 187.5)
    """
    now = datetime.now(UTC)
    trades = [
        {"side": "buy", "price": 300, "qty": 0.5, "timestamp": (now - timedelta(minutes=1)).isoformat()},
        {"side": "sell", "price": 250, "qty": 0.5, "timestamp": (now - timedelta(minutes=2)).isoformat()},
        {"side": "buy", "price": 200, "qty": 1.0, "timestamp": (now - timedelta(minutes=3)).isoformat()},
        {"side": "buy", "price": 100, "qty": 1.0, "timestamp": (now - timedelta(minutes=4)).isoformat()},
    ]
    # Function receives DESC order from DB: [newest, ..., oldest]
    
    avg_cost = _reconstruct_avg_cost(trades)
    
    # Expected: 187.5
    assert avg_cost == pytest.approx(187.5)

def test_reconstruct_avg_cost_reset_on_zero_qty():
    """
    If qty hits 0, avg_cost should reset.
    """
    now = datetime.now(UTC)
    trades = [
        {"side": "buy", "price": 500, "qty": 1.0, "timestamp": (now - timedelta(minutes=1)).isoformat()},
        {"side": "sell", "price": 400, "qty": 1.0, "timestamp": (now - timedelta(minutes=2)).isoformat()}, # Full exit
        {"side": "buy", "price": 100, "qty": 1.0, "timestamp": (now - timedelta(minutes=3)).isoformat()},
    ]
    
    avg_cost = _reconstruct_avg_cost(trades)
    
    # Expected: 500 (the 100 was cleared by the full sell)
    assert avg_cost == pytest.approx(500.0)
