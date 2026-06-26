"""Account-level risk for the futures bot.

Three responsibilities:
  1. Kill-switch: halt + flatten if the account is down more than the configured
     daily-loss or total-drawdown limit (the per-trade fixed-fractional sizing
     bounds single trades; this bounds streaks and flash events).
  2. DEATH-LOOP FIX: a *persisted* HALTED state. The spot bot looped because it
     restored peak_equity on every restart and re-triggered the emergency unwind.
     Here, once halted, the bot flattens once and stays halted until a MANUAL
     resume, which rebases the peak so it cannot immediately re-fire.
  3. Liquidation-buffer check: confirm the liquidation price sits beyond the stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True)
class RiskDecision:
    allow_new_entries: bool
    flatten_and_halt: bool
    reason: str


class FuturesRiskManager:
    """Account-level kill-switch and peak/drawdown tracking."""

    def __init__(self, *, max_daily_loss_pct: float, max_total_drawdown_pct: float) -> None:
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_total_drawdown_pct = max_total_drawdown_pct
        self.peak_equity: float | None = None
        self.day_start_equity: float | None = None
        self.current_day: date | None = None
        self.halted: bool = False
        self._last_daily_loss: float = 0.0
        self._last_drawdown: float = 0.0

    def restore(
        self, *, peak_equity: float | None = None, halted: bool = False,
        day_start_equity: float | None = None, current_day: date | None = None,
    ) -> None:
        """Restore persisted state on startup (peak + halted flag)."""
        if peak_equity and peak_equity > 0:
            self.peak_equity = peak_equity
        self.halted = halted
        if day_start_equity and day_start_equity > 0:
            self.day_start_equity = day_start_equity
        self.current_day = current_day

    def _roll_day(self, now: datetime, equity: float) -> None:
        if self.current_day != now.date():
            self.current_day = now.date()
            self.day_start_equity = equity

    def evaluate(self, *, equity: float, now: datetime) -> RiskDecision:
        self._roll_day(now, equity)
        self.peak_equity = equity if self.peak_equity is None else max(self.peak_equity, equity)

        # DEATH-LOOP FIX: once halted, stay halted. No repeated unwind on restart.
        if self.halted:
            return RiskDecision(False, True, "halted_awaiting_manual_resume")

        ds = self.day_start_equity or equity
        pk = self.peak_equity or equity
        self._last_daily_loss = (ds - equity) / ds if ds > 0 else 0.0
        self._last_drawdown = (pk - equity) / pk if pk > 0 else 0.0

        if self._last_daily_loss >= self.max_daily_loss_pct:
            self.halted = True
            return RiskDecision(False, True, f"max_daily_loss:{self._last_daily_loss:.4f}")
        if self._last_drawdown >= self.max_total_drawdown_pct:
            self.halted = True
            return RiskDecision(False, True, f"max_total_drawdown:{self._last_drawdown:.4f}")
        return RiskDecision(True, False, "ok")

    def resume(self, equity: float) -> None:
        """Manual resume: clear halt and rebase the peak/day baselines to current
        equity so the kill-switch does not immediately re-fire (death-loop fix)."""
        self.halted = False
        self.peak_equity = equity
        self.day_start_equity = equity
        self._last_daily_loss = 0.0
        self._last_drawdown = 0.0

    @staticmethod
    def liquidation_safe(
        *, side: str, liq_price: float, stop_price: float, buffer_pct: float,
    ) -> bool:
        """True if the liquidation price is beyond the stop by ``buffer_pct`` (so
        the stop fires first). For a long, liq must be below the stop; for a short,
        above. Returns True when there is no liq price yet (flat)."""
        if liq_price <= 0:
            return True
        if side == "Buy":
            return liq_price < stop_price * (1 - buffer_pct)
        return liq_price > stop_price * (1 + buffer_pct)

    def status(self) -> dict:
        """Snapshot for dashboard / persistence."""
        return {
            "halted": self.halted,
            "daily_loss_pct": round(self._last_daily_loss * 100, 4),
            "drawdown_pct": round(self._last_drawdown * 100, 4),
            "max_daily_loss_pct": round(self.max_daily_loss_pct * 100, 4),
            "max_total_drawdown_pct": round(self.max_total_drawdown_pct * 100, 4),
            "peak_equity": self.peak_equity,
        }
