"""Risk controls and circuit breakers for live trading safety."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


@dataclass(slots=True)
class RiskDecision:
    """Decision output for each cycle."""

    allow_trading: bool
    emergency_stop: bool
    reason: str
    paused_until: datetime | None = None


class RiskManager:
    """Evaluates drawdown, daily loss, and price shock circuit breakers."""

    def __init__(
        self,
        *,
        max_drawdown_pct: float,
        max_daily_loss_pct: float,
        max_hourly_move_pct: float,
    ) -> None:
        self.max_drawdown_pct = max_drawdown_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_hourly_move_pct = max_hourly_move_pct

        self.peak_equity: float | None = None
        self.day_start_equity: float | None = None
        self.current_day: date | None = None
        self.paused_until: datetime | None = None
        self._price_windows: dict[str, deque[tuple[datetime, float]]] = {}

    def _roll_day(self, now: datetime, equity: float) -> None:
        if self.current_day != now.date():
            self.current_day = now.date()
            self.day_start_equity = equity

    def _update_peak(self, equity: float) -> None:
        if self.peak_equity is None:
            self.peak_equity = equity
        else:
            self.peak_equity = max(self.peak_equity, equity)

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

    def evaluate(self, *, equity: float, now: datetime | None = None) -> RiskDecision:
        """Evaluate current account state against all active risk rules."""
        timestamp = now or datetime.now(UTC)
        self._roll_day(timestamp, equity)
        self._update_peak(equity)

        # Check if in pause period
        if self.paused_until and timestamp < self.paused_until:
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

        # Max drawdown check
        drawdown = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0.0
        if drawdown >= self.max_drawdown_pct:
            return RiskDecision(
                allow_trading=False,
                emergency_stop=True,
                reason=f"max_drawdown_triggered:{drawdown:.4f}",
            )

        # Daily loss check
        day_loss = (
            (self.day_start_equity - equity) / self.day_start_equity
            if self.day_start_equity else 0.0
        )
        if day_loss >= self.max_daily_loss_pct:
            self.paused_until = timestamp + timedelta(hours=24)
            return RiskDecision(
                allow_trading=False,
                emergency_stop=False,
                reason=f"max_daily_loss_triggered:{day_loss:.4f}",
                paused_until=self.paused_until,
            )

        # Hourly price shock check
        hourly_move = self.price_move_one_hour()
        if hourly_move >= self.max_hourly_move_pct:
            return RiskDecision(
                allow_trading=False,
                emergency_stop=True,
                reason=f"hourly_price_shock:{hourly_move:.4f}",
            )

        return RiskDecision(allow_trading=True, emergency_stop=False, reason="ok")

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
