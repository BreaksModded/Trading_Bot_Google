"""Risk controls and circuit breakers for live trading safety."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger


@dataclass(slots=True)
class RiskDecision:
    """Decision output for each cycle."""

    allow_trading: bool
    emergency_stop: bool
    reason: str
    paused_until: datetime | None = None
    # Phase J: price shock pause — True while grids are blocked for volatility
    block_new_grids: bool = False
    # Phase J: True during an active price shock pause (False during emergency or normal)
    price_shock_paused: bool = False


class RiskManager:
    """Evaluates drawdown, daily loss, and price shock circuit breakers."""

    def __init__(
        self,
        *,
        max_drawdown_pct: float,
        max_daily_loss_pct: float,
        max_hourly_move_pct: float,
        # Phase J: cold-start guard
        min_price_shock_samples: int = 10,
        # Phase J: consecutive clean cycles required before auto-resume
        price_shock_resume_cycles: int = 3,
        # Phase J: seconds before sustained pause escalates to emergency stop
        price_shock_max_pause_secs: int = 7200,
        # Dynamic daily loss pause: minimum hours before equity-recovery check
        daily_loss_pause_hours: float = 1.0,
    ) -> None:
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_hourly_move_pct = max_hourly_move_pct
        self.min_price_shock_samples = min_price_shock_samples
        self.price_shock_resume_cycles = price_shock_resume_cycles
        self.price_shock_max_pause_secs = price_shock_max_pause_secs
        self.daily_loss_pause_hours = daily_loss_pause_hours

        self.peak_equity: float | None = None
        self.day_start_equity: float | None = None
        self.current_day: date | None = None
        self.paused_until: datetime | None = None
        self._loss_trigger_equity: float | None = None
        self._loss_paused_at: datetime | None = None
        self._loss_watermark: float | None = None
        self._price_windows: dict[str, deque[tuple[datetime, float]]] = {}

        # Phase G: lazy evaluation — price shock runs at most every 5s
        self._last_price_shock_eval: float = 0.0
        self._PRICE_SHOCK_EVAL_INTERVAL: float = 5.0
        self._cached_price_shock_result: float = 0.0

        # Phase J: three-stage price shock state
        self._price_shock_paused: bool = False
        self._price_shock_pause_start: float | None = None
        self._price_shock_clean_cycles: int = 0

        # Phase J: warmup logging guards (each message fires exactly once)
        self._warmup_logged: bool = False
        self._warmup_complete_logged: bool = False

        # Phase K: last evaluation snapshot for dashboard (percentage as ratio 0–1)
        self._last_drawdown_pct: float = 0.0
        self._last_daily_loss_pct: float = 0.0

    # ── Equity tracking ───────────────────────────────────────────────

    def _roll_day(self, now: datetime, equity: float) -> None:
        if self.current_day != now.date():
            self.current_day = now.date()
            self.day_start_equity = equity
            self._loss_watermark = None

    def _update_peak(self, equity: float) -> None:
        if self.peak_equity is None:
            self.peak_equity = equity
        else:
            self.peak_equity = max(self.peak_equity, equity)

    # ── Price registration ────────────────────────────────────────────

    def register_price(self, *, now: datetime, price: float, symbol: str = "_default") -> None:
        """Store latest prices for one-hour movement checks (per-symbol)."""
        window = self._price_windows.setdefault(symbol, deque(maxlen=1024))
        window.append((now, price))
        cutoff = now - timedelta(hours=1)
        while window and window[0][0] < cutoff:
            window.popleft()

    @staticmethod
    def _window_move(window: deque[tuple[datetime, float]]) -> float:
        if len(window) < 2:
            return 0.0
        first = window[0][1]
        last = window[-1][1]
        if first == 0:
            return 0.0
        return abs(last - first) / first

    def price_move_one_hour(self) -> float:
        """Return maximum absolute one-hour percentage move across all symbols."""
        if not self._price_windows:
            return 0.0
        return max(self._window_move(w) for w in self._price_windows.values())

    # ── Phase J helpers ───────────────────────────────────────────────

    def _max_sample_count(self) -> int:
        """Maximum number of samples across all symbol windows."""
        if not self._price_windows:
            return 0
        return max(len(w) for w in self._price_windows.values())

    def _run_price_shock_eval_cycle(self, t_now: float) -> None:
        """
        Run one price shock evaluation cycle (called at lazy-eval intervals).

        Manages the warmup guard, pause-state transitions, and auto-resume
        logic. Escalation to emergency stop is checked per-call in evaluate().

        Phase G lazy-eval timing:
          interval = 5s, main loop = 1s
          "3 clean cycles" ≈ 15 s of confirmed stability
          "2-hour escalation" = 7200s / 5s = 1440 lazy-eval cycles
        """
        sample_count = self._max_sample_count()

        # ── Cold-start guard ──────────────────────────────────────────
        if sample_count < self.min_price_shock_samples:
            if not self._warmup_logged:
                logger.info(
                    "[RiskManager] Price shock circuit breaker in warmup mode. "
                    "Collecting price samples ({}/{}). Will activate after {} samples.",
                    sample_count,
                    self.min_price_shock_samples,
                    self.min_price_shock_samples,
                )
                self._warmup_logged = True
            else:
                logger.debug(
                    "[RiskManager] Price shock evaluation skipped: "
                    "only {} samples collected, minimum is {}. Warmup period active.",
                    sample_count,
                    self.min_price_shock_samples,
                )
            return

        # Log once when warmup completes
        if not self._warmup_complete_logged:
            logger.info(
                "[RiskManager] Price shock circuit breaker now active. "
                "Sufficient price history collected ({}/{} samples).",
                sample_count,
                self.min_price_shock_samples,
            )
            self._warmup_complete_logged = True

        current_move = self.price_move_one_hour()
        self._cached_price_shock_result = current_move
        threshold = self.max_hourly_move_pct

        if current_move >= threshold:
            # Above threshold: reset clean cycle counter
            self._price_shock_clean_cycles = 0

            if not self._price_shock_paused:
                # First detection: enter pause mode
                self._price_shock_paused = True
                self._price_shock_pause_start = t_now
                logger.warning(
                    "[RiskManager] Price shock detected: {:.2%} move "
                    "(threshold: {:.2%}). Pausing new grid placements. "
                    "Monitoring for stabilization.",
                    current_move,
                    threshold,
                )
            # If already paused and still above threshold: escalation check
            # happens in evaluate() on every call (not just lazy-eval cycles)

        else:
            # Below threshold
            if self._price_shock_paused:
                self._price_shock_clean_cycles += 1
                logger.debug(
                    "[RiskManager] Price move below threshold ({:.2%}), "
                    "waiting for {} consecutive clean cycles ({}/{}).",
                    current_move,
                    self.price_shock_resume_cycles,
                    self._price_shock_clean_cycles,
                    self.price_shock_resume_cycles,
                )

                if self._price_shock_clean_cycles >= self.price_shock_resume_cycles:
                    # Confirmed stabilization: auto-resume
                    if self._price_shock_pause_start is None:
                        logger.error("[RiskManager] price_shock_pause_start is None during resume — resetting state.")
                        self._price_shock_paused = False
                        self._price_shock_clean_cycles = 0
                        return
                    pause_duration = t_now - self._price_shock_pause_start
                    self._price_shock_paused = False
                    self._price_shock_pause_start = None
                    self._price_shock_clean_cycles = 0
                    logger.info(
                        "[RiskManager] Price shock resolved. Market stabilized "
                        "({} consecutive clean cycles). Resuming grid placements. "
                        "Pause duration: {:.0f}s.",
                        self.price_shock_resume_cycles,
                        pause_duration,
                    )

    # ── Main evaluation ───────────────────────────────────────────────

    def evaluate(self, *, equity: float, now: datetime | None = None) -> RiskDecision:
        """Evaluate current account state against all active risk rules."""
        timestamp = now or datetime.now(UTC)
        self._roll_day(timestamp, equity)
        self._update_peak(equity)

        # ── Daily loss pause (dynamic: equity-recovery + min hours) ───
        if self.paused_until and self._loss_trigger_equity is not None:
            min_elapsed = timestamp >= self.paused_until
            equity_recovered = equity >= self._loss_trigger_equity
            fallback_24h = (
                self._loss_paused_at is not None
                and timestamp >= self._loss_paused_at + timedelta(hours=24)
            )

            if min_elapsed and equity_recovered:
                hours_paused = (
                    (timestamp - self._loss_paused_at).total_seconds() / 3600
                    if self._loss_paused_at else 0.0
                )
                logger.info(
                    "[RiskManager] Daily loss CB cleared — equity recovered "
                    "to {:.2f} (trigger was {:.2f}) after {:.1f}h",
                    equity, self._loss_trigger_equity, hours_paused,
                )
                self.paused_until = None
                self._loss_trigger_equity = None
                self._loss_paused_at = None
            elif fallback_24h:
                logger.warning(
                    "[RiskManager] Daily loss CB cleared after 24h fallback "
                    "(equity did not recover: {:.2f} vs trigger {:.2f})",
                    equity, self._loss_trigger_equity,
                )
                self.paused_until = None
                self._loss_trigger_equity = None
                self._loss_paused_at = None
            else:
                return RiskDecision(
                    allow_trading=False,
                    emergency_stop=False,
                    reason="daily_loss_pause",
                    paused_until=self.paused_until,
                )

        if self.peak_equity is None or self.day_start_equity is None:
            return RiskDecision(
                allow_trading=False,
                emergency_stop=False,
                reason="awaiting_first_equity",
            )

        # ── Max drawdown (unchanged) ──────────────────────────────────
        drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0.0
        self._last_drawdown_pct = drawdown
        if drawdown >= self.max_drawdown_pct:
            return RiskDecision(
                allow_trading=False,
                emergency_stop=True,
                reason=f"max_drawdown_triggered:{drawdown:.4f}",
            )

        # ── Daily loss (unchanged) ────────────────────────────────────
        day_loss = (
            (self.day_start_equity - equity) / self.day_start_equity
            if self.day_start_equity else 0.0
        )
        self._last_daily_loss_pct = day_loss
        if day_loss >= self.max_daily_loss_pct:
            if self._loss_watermark is None or equity < self._loss_watermark:
                self._loss_trigger_equity = equity
                self._loss_watermark = equity
                self._loss_paused_at = timestamp
                self.paused_until = timestamp + timedelta(hours=self.daily_loss_pause_hours)
                return RiskDecision(
                    allow_trading=False,
                    emergency_stop=False,
                    reason=f"max_daily_loss_triggered:{day_loss:.4f}",
                    paused_until=self.paused_until,
                )

        # ── Hourly price shock — Phase G lazy eval + Phase J pause ────
        t_now = time.monotonic()
        if (t_now - self._last_price_shock_eval) >= self._PRICE_SHOCK_EVAL_INTERVAL:
            self._last_price_shock_eval = t_now
            self._run_price_shock_eval_cycle(t_now)

        # Escalation and pause-state checks run on EVERY call (not just at
        # lazy-eval intervals), so escalation is detected within one loop cycle.
        if self._price_shock_paused:
            if self._price_shock_pause_start is None:
                logger.error("[RiskManager] price_shock_pause_start is None while paused — resetting state.")
                self._price_shock_paused = False
                self._price_shock_clean_cycles = 0
            else:
                pause_duration = t_now - self._price_shock_pause_start

                if pause_duration > self.price_shock_max_pause_secs:
                    # Sustained extreme volatility: escalate to emergency stop
                    logger.critical(
                        "[RiskManager] Price shock sustained for {:.0f}s "
                        "(limit: {}s). Escalating to emergency stop.",
                        pause_duration,
                        self.price_shock_max_pause_secs,
                    )
                    return RiskDecision(
                        allow_trading=False,
                        emergency_stop=True,
                        block_new_grids=True,
                        reason=(
                            f"price_shock_sustained:"
                            f"{self._cached_price_shock_result:.4f}:{pause_duration:.0f}s"
                        ),
                    )

                return RiskDecision(
                    allow_trading=True,
                    emergency_stop=False,
                    block_new_grids=True,
                    price_shock_paused=True,
                    reason=f"price_shock_pause_active:{self._cached_price_shock_result:.4f}",
                )

        return RiskDecision(allow_trading=True, emergency_stop=False, reason="ok")

    # ── Dashboard status snapshot ─────────────────────────────────────

    def get_risk_status(self) -> dict[str, Any]:
        """Return current risk metrics as a percentage-scale dict for the dashboard.

        Values are in 0–100 range (multiply ratios by 100) so the frontend
        can display them directly with .toFixed(2)%.
        """
        return {
            "available": True,
            "drawdown_pct": round(self._last_drawdown_pct * 100, 4),
            "daily_loss_pct": round(max(0.0, self._last_daily_loss_pct) * 100, 4),
            "drawdown_limit_pct": round(self.max_drawdown_pct * 100, 4),
            "daily_loss_limit_pct": round(self.max_daily_loss_pct * 100, 4),
            "price_move_1h": round(self._cached_price_shock_result * 100, 4),
            "price_move_limit_pct": round(self.max_hourly_move_pct * 100, 4),
            "price_shock_paused": self._price_shock_paused,
            "warmup_complete": self._warmup_complete_logged,
            "is_paused": self._price_shock_paused,
        }

    # ── Manual controls ───────────────────────────────────────────────

    def force_pause(self, hours: int, *, now: datetime | None = None) -> None:
        """Pause trading manually for a fixed time window."""
        base = now or datetime.now(UTC)
        self.paused_until = base + timedelta(hours=hours)

    def clear_pause(self) -> None:
        """Resume trading immediately from a paused state."""
        self.paused_until = None

    def drawdown_pct(self, equity: float) -> float:
        """Compute current drawdown from known peak."""
        if self.peak_equity is None or self.peak_equity == 0:
            return 0.0
        return (self.peak_equity - equity) / self.peak_equity
