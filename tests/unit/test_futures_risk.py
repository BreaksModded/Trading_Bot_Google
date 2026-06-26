"""Tests for the futures risk manager: kill-switch, death-loop fix, liq buffer."""

from __future__ import annotations

from datetime import UTC, datetime

from core.futures_risk import FuturesRiskManager


def _rm() -> FuturesRiskManager:
    return FuturesRiskManager(max_daily_loss_pct=0.06, max_total_drawdown_pct=0.20)


def _now(day: int = 1) -> datetime:
    return datetime(2026, 6, day, 12, 0, tzinfo=UTC)


def test_normal_state_allows_trading():
    rm = _rm()
    d = rm.evaluate(equity=150, now=_now())
    assert d.allow_new_entries and not d.flatten_and_halt


def test_daily_loss_kill_switch_fires():
    rm = _rm()
    rm.evaluate(equity=150, now=_now())          # day start = 150
    d = rm.evaluate(equity=140, now=_now())       # -6.7% on the day
    assert d.flatten_and_halt and "max_daily_loss" in d.reason
    assert rm.halted


def test_total_drawdown_kill_switch_fires():
    rm = _rm()
    rm.evaluate(equity=200, now=_now())           # peak = 200
    d = rm.evaluate(equity=159, now=_now())        # -20.5% from peak (daily uses same-day start 200 too)
    assert d.flatten_and_halt


def test_halt_persists_and_does_not_reloop():
    # Simulate a restart: restore halted=True with a stale (high) peak.
    rm = _rm()
    rm.restore(peak_equity=200, halted=True)
    d = rm.evaluate(equity=150, now=_now())  # would be -25% drawdown
    # It must NOT re-trigger an unwind loop; it just stays halted.
    assert d.flatten_and_halt and d.reason == "halted_awaiting_manual_resume"


def test_resume_rebases_peak_so_it_does_not_refire():
    rm = _rm()
    rm.restore(peak_equity=200, halted=True)
    rm.resume(equity=150)
    d = rm.evaluate(equity=150, now=_now())
    assert d.allow_new_entries and not d.flatten_and_halt
    assert rm.peak_equity == 150  # rebased


def test_liquidation_safe_long_and_short():
    # Long: liq must sit below the stop by the buffer.
    assert FuturesRiskManager.liquidation_safe(side="Buy", liq_price=1700, stop_price=1900, buffer_pct=0.05)
    assert not FuturesRiskManager.liquidation_safe(side="Buy", liq_price=1890, stop_price=1900, buffer_pct=0.05)
    # Short: liq must sit above the stop.
    assert FuturesRiskManager.liquidation_safe(side="Sell", liq_price=2200, stop_price=2050, buffer_pct=0.05)
    assert not FuturesRiskManager.liquidation_safe(side="Sell", liq_price=2060, stop_price=2050, buffer_pct=0.05)
    # Flat (no liq price) is safe.
    assert FuturesRiskManager.liquidation_safe(side="Buy", liq_price=0, stop_price=1900, buffer_pct=0.05)
