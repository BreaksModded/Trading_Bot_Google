"""Trend-following engine: entry confirmation, fixed-fractional sizing, and
Chandelier-Exit trailing stops.

This is the positive-skew half of the bot. It enters ONE position aligned with
a confirmed trend (long in an uptrend, short in a downtrend), sizes it so that
hitting the initial stop loses a fixed fraction of equity, and trails the stop
with a ratcheting Chandelier Exit so winners run. Pure logic — no exchange I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from core.regime import MarketRegime


def _round_down(value: float, step: Decimal) -> float:
    if step <= 0:
        return value
    units = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_DOWN)
    return float(units * step)


@dataclass(slots=True)
class TrendEntry:
    """Result of an entry evaluation."""

    side: str | None  # "Buy" (long) | "Sell" (short) | None
    reason: str


@dataclass(slots=True)
class TrendStop:
    """Stateful, ratcheting Chandelier stop for an open trend position."""

    side: str  # "Buy" (long) | "Sell" (short)
    stop_price: float

    def update(self, *, chandelier_long: float, chandelier_short: float) -> float:
        """Ratchet the stop in the favourable direction only."""
        if self.side == "Buy":
            self.stop_price = max(self.stop_price, chandelier_long)  # long stop only rises
        else:
            self.stop_price = min(self.stop_price, chandelier_short)  # short stop only falls
        return self.stop_price

    def is_hit(self, price: float) -> bool:
        return price <= self.stop_price if self.side == "Buy" else price >= self.stop_price


def evaluate_trend_entry(
    *,
    regime: MarketRegime,
    higher_tf_regime: MarketRegime | None,
    require_htf: bool,
) -> TrendEntry:
    """Confirm a trend entry. When ``require_htf`` is set, a higher-timeframe
    trend in the *opposite* direction blocks the entry (the multi-TF filter that
    research shows removes 40-60% of false signals)."""
    if regime == MarketRegime.TRENDING_UP:
        if require_htf and higher_tf_regime == MarketRegime.TRENDING_DOWN:
            return TrendEntry(None, "htf_conflict")
        return TrendEntry("Buy", "trend_up_confirmed")
    if regime == MarketRegime.TRENDING_DOWN:
        if require_htf and higher_tf_regime == MarketRegime.TRENDING_UP:
            return TrendEntry(None, "htf_conflict")
        return TrendEntry("Sell", "trend_down_confirmed")
    return TrendEntry(None, f"no_trend:{regime}")


def initial_trend_stop(side: str, *, chandelier_long: float, chandelier_short: float) -> TrendStop:
    """Build the initial Chandelier stop for a fresh entry."""
    return TrendStop(side=side, stop_price=chandelier_long if side == "Buy" else chandelier_short)


def compute_fixed_fractional_qty(
    *,
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    qty_step: Decimal,
    min_qty: Decimal,
    available_margin: float = 0.0,
    leverage: int = 1,
) -> float:
    """Position size so that hitting ``stop_price`` loses ``risk_pct`` of equity.

    qty = (equity * risk_pct) / |entry - stop|, capped so the notional never
    exceeds available_margin * leverage, then rounded DOWN to the qty step.
    Returns 0.0 if the result is below the exchange minimum (caller skips).
    """
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0 or entry_price <= 0 or equity <= 0 or risk_pct <= 0:
        return 0.0

    qty = (equity * risk_pct) / stop_dist

    if available_margin > 0 and leverage > 0:
        qty = min(qty, (available_margin * leverage) / entry_price)

    qty = _round_down(qty, qty_step)
    return qty if qty >= float(min_qty) else 0.0
