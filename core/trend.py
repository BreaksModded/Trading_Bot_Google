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

    qty = (equity * risk_pct) / |entry - stop|, capped so the notional never exceeds
    90% of available_margin * leverage (safety buffer for fees), then rounded DOWN to
    the step. Returns 0.0 if the result is below the exchange minimum (caller skips).
    """
    stop_dist = abs(entry_price - stop_price)
    if stop_dist <= 0 or entry_price <= 0 or equity <= 0 or risk_pct <= 0:
        return 0.0

    qty = (equity * risk_pct) / stop_dist

    # 10% safety buffer: sizing to 100% of free margin gets rejected by OKX (51008) on
    # fees/slippage between sizing and fill (audit H3).
    if available_margin > 0 and leverage > 0:
        qty = min(qty, (available_margin * leverage * 0.90) / entry_price)

    qty = _round_down(qty, qty_step)
    return qty if qty >= float(min_qty) else 0.0


@dataclass(slots=True)
class TrendDecision:
    """The action the bot should take this bar/cycle (no I/O)."""

    action: str  # "enter" | "hold" | "close" | "none"
    side: str | None = None      # for "enter"
    qty: float = 0.0             # for "enter"
    stop: "TrendStop | None" = None  # for "enter" (new) / "hold" (ratcheted)
    reason: str = ""


def decide_trend(
    *,
    regime: MarketRegime,
    regime_htf: MarketRegime | None,
    position_side: str,
    position_flat: bool,
    chandelier_long: float,
    chandelier_short: float,
    live_price: float,
    equity: float,
    available_margin: float,
    trend_stop: TrendStop | None,
    risk_pct: float,
    leverage: int,
    require_htf: bool,
    qty_step: Decimal,
    min_qty: Decimal,
) -> TrendDecision:
    """The SHARED trend decision used by both the live bot and the backtest, so
    they cannot drift. Assumes ``regime`` is TRENDING_UP/DOWN (caller dispatches).

    Returns what to do (enter/hold/close/none); the caller performs the I/O
    (live: real orders; backtest: simulated fills).
    """
    desired_side = "Buy" if regime == MarketRegime.TRENDING_UP else "Sell"
    desired_pos = "long" if desired_side == "Buy" else "short"

    if position_flat:
        entry = evaluate_trend_entry(
            regime=regime, higher_tf_regime=regime_htf, require_htf=require_htf,
        )
        if entry.side is None:
            return TrendDecision("none", reason=entry.reason)
        stop = initial_trend_stop(
            entry.side, chandelier_long=chandelier_long, chandelier_short=chandelier_short,
        )
        qty = compute_fixed_fractional_qty(
            equity=equity, risk_pct=risk_pct, entry_price=live_price,
            stop_price=stop.stop_price, qty_step=qty_step, min_qty=min_qty,
            available_margin=available_margin, leverage=leverage,
        )
        if qty <= 0:
            return TrendDecision("none", reason="qty_below_min")
        return TrendDecision("enter", side=entry.side, qty=qty, stop=stop, reason=entry.reason)

    # Holding a position.
    if position_side != desired_pos:
        return TrendDecision("close", reason="trend_reversal")
    if trend_stop is None or trend_stop.side != desired_side:
        trend_stop = initial_trend_stop(
            desired_side, chandelier_long=chandelier_long, chandelier_short=chandelier_short,
        )
    trend_stop.update(chandelier_long=chandelier_long, chandelier_short=chandelier_short)
    if trend_stop.is_hit(live_price):
        return TrendDecision("close", stop=trend_stop, reason="chandelier_stop")
    return TrendDecision("hold", stop=trend_stop)
