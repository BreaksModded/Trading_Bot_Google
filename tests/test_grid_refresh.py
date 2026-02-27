import pytest
from datetime import datetime, timezone, timedelta
from core.grid_refresh import (
    RefreshConfig,
    RefreshState,
    StalenessResult,
    compute_effective_threshold,
    is_grid_stale,
    should_refresh,
)

def test_compute_effective_threshold():
    # base is 4%, ATR is none
    assert compute_effective_threshold(0.04, 2.5, None) == 0.04
    # base is 4%, ATR is 1% (2.5 * 1% = 2.5% < 4%) -> use base
    assert compute_effective_threshold(0.04, 2.5, 0.01) == 0.04
    # base is 4%, ATR is 2% (2.5 * 2% = 5.0% > 4%) -> use adaptive
    assert compute_effective_threshold(0.04, 2.5, 0.02) == 0.05

def test_is_grid_stale():
    cfg = RefreshConfig(max_grid_age_hours=6.0, price_distance_pct=0.04, atr_multiplier=2.5)
    now = datetime.now(timezone.utc)
    
    # Not stale (age < 6.0, dev < base)
    recent_grid = now - timedelta(hours=2)
    res = is_grid_stale(
        current_price=101, anchor_price=100, grid_created_at=recent_grid, atr_pct=0.01, cfg=cfg
    )
    assert not res.is_stale

    # Age is stale, but dev < base
    old_grid = now - timedelta(hours=7)
    res = is_grid_stale(
        current_price=101, anchor_price=100, grid_created_at=old_grid, atr_pct=0.01, cfg=cfg
    )
    assert not res.is_stale

    # Dev >= base, but age < 6.0
    res = is_grid_stale(
        current_price=105, anchor_price=100, grid_created_at=recent_grid, atr_pct=0.01, cfg=cfg
    )
    assert not res.is_stale

    # Both age >= 6.0 and dev >= base
    res = is_grid_stale(
        current_price=105, anchor_price=100, grid_created_at=old_grid, atr_pct=0.01, cfg=cfg
    )
    assert res.is_stale
    assert res.price_deviation_pct == 0.05
    assert res.threshold_used_pct == 0.04

    # Both age and dev are stale, but high ATR raises threshold so it's NOT stale
    res = is_grid_stale(
        current_price=105, anchor_price=100, grid_created_at=old_grid, atr_pct=0.04, cfg=cfg
    )
    # Threshold = 2.5 * 0.04 = 0.10. Deviation is 0.05 < 0.10
    assert not res.is_stale
    assert res.threshold_used_pct == 0.10

def test_should_refresh():
    cfg = RefreshConfig(max_refreshes_per_day=2, cooldown_minutes=90, stale_confirm_cycles=2)
    state = RefreshState()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    stale_res = StalenessResult(True, "foo", 0.05, 10.0, 0.04)

    # 1st cycle
    go, _ = should_refresh(state, stale_res, cfg, now)
    assert not go
    assert state.stale_cycle_count == 1

    # 2nd cycle -> goes
    go, _ = should_refresh(state, stale_res, cfg, now)
    assert go
    assert state.stale_cycle_count == 2
    
    # Simulate refresh executed
    state.last_refresh_at = now
    state.refresh_count_today += 1
    state.stale_cycle_count = 0

    # Next cycle, inside cooldown
    later = now + timedelta(minutes=30)
    go, reason = should_refresh(state, stale_res, cfg, later)
    assert not go
    assert "in_cooldown" in reason

    # Next cycle, outside cooldown
    much_later = now + timedelta(minutes=100)
    
    # 1st confirm
    go, _ = should_refresh(state, stale_res, cfg, much_later)
    assert not go
    assert state.stale_cycle_count == 1
    
    # 2nd confirm -> goes
    go, _ = should_refresh(state, stale_res, cfg, much_later)
    assert go
    state.refresh_count_today += 1
    
    # Simulate refresh executed 2nd time
    state.last_refresh_at = much_later
    state.stale_cycle_count = 0

    # Even outside cooldown, hit max daily refreshes
    even_later = much_later + timedelta(minutes=100)
    
    # At limit right away
    go, reason = should_refresh(state, stale_res, cfg, even_later)
    assert not go
    assert "daily_cap_reached" in reason

    # Turn of day resets counts
    next_day = now + timedelta(days=1)
    go, _ = should_refresh(state, stale_res, cfg, next_day)
    # This resets daily limit, starts confirming...
    assert not go
    assert state.stale_cycle_count == 1
    
    # 2nd confirm on next day -> goes!
    go, _ = should_refresh(state, stale_res, cfg, next_day)
    assert go
