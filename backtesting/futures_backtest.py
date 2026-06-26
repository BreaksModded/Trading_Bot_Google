"""Backtest for the futures TREND engine (the positive-skew core).

It runs the *real* strategy functions (regime classification, entry rule,
fixed-fractional sizing, ratcheting Chandelier stop) bar-by-bar over historical
OHLCV, with realistic costs:
  - taker fee on market entry/exit (Bybit linear 0.055%)
  - funding every 8h on the open position
  - liquidation guard (sanity)

Honest scope: this validates TREND mode only. RANGING/TRANSITIONAL bars are
treated as flat (no grid income is modelled), so the result is a CONSERVATIVE
lower bound — if the trend engine is positive net of costs here, the live bot
(which also harvests ranges) should do at least this well. The grid harvest is
a separate, later simulation.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.indicators import compute_chandelier_exit, enrich_indicators
from core.regime import MarketRegime, classify_futures_regime
from core.trend import (
    compute_fixed_fractional_qty, evaluate_trend_entry, initial_trend_stop,
)


class FuturesTrendBacktest:
    """Event-driven backtest of the trend engine."""

    def __init__(
        self, *, settings, initial_capital: float = 150.0,
        taker_fee: float = 0.00055, funding_rate_8h: float = 0.0001,
        tf_minutes: int = 60,
    ) -> None:
        self.s = settings.futures
        self.initial_capital = initial_capital
        self.taker_fee = taker_fee
        self.funding_rate_8h = funding_rate_8h
        self.bars_per_funding = max(1, round(8 * 60 / max(1, tf_minutes)))

    def run(self, df: pd.DataFrame) -> dict:
        s = self.s
        enriched = enrich_indicators(
            df, ema_fast=s.ema_fast, ema_slow=s.ema_slow,
            atr_period=s.atr_period, adx_period=s.adx_period,
        )
        ce = compute_chandelier_exit(
            df, period=s.chandelier_period, atr_mult=s.chandelier_atr_mult, atr_period=s.atr_period,
        )
        warmup = max(s.ema_slow, s.chandelier_period) + 1
        if len(df) <= warmup:
            raise ValueError(f"need > {warmup} bars (got {len(df)})")

        equity = self.initial_capital
        position: dict | None = None
        trades: list[dict] = []
        equity_curve: list[dict] = []
        fees_total = 0.0
        funding_total = 0.0
        peak = equity
        max_dd = 0.0

        cl_arr = ce["chandelier_long"].to_numpy()
        cs_arr = ce["chandelier_short"].to_numpy()

        for i in range(warmup, len(df)):
            row = enriched.iloc[i]
            price = float(row["close"]); high = float(row["high"]); low = float(row["low"])
            ts = row.get("timestamp", i)
            regime = classify_futures_regime(
                float(row["adx"]), float(row["ema_fast"]), float(row["ema_slow"]),
                adx_trend=s.adx_trend_threshold, adx_range=s.adx_range_threshold,
            )
            cl = float(cl_arr[i]); cs = float(cs_arr[i])

            # Funding on the open position every 8h.
            if position is not None and i % self.bars_per_funding == 0:
                pay = position["qty"] * price * self.funding_rate_8h
                if position["side"] == "Buy":
                    equity -= pay; funding_total += pay
                else:
                    equity += pay; funding_total -= pay

            # Manage an open position.
            if position is not None:
                position["stop"].update(chandelier_long=cl, chandelier_short=cs)
                stop_px = position["stop"].stop_price
                exit_px = None; reason = ""
                if position["side"] == "Buy" and low <= stop_px:
                    exit_px = stop_px; reason = "chandelier_stop"
                elif position["side"] == "Sell" and high >= stop_px:
                    exit_px = stop_px; reason = "chandelier_stop"
                elif position["side"] == "Buy" and regime != MarketRegime.TRENDING_UP:
                    exit_px = price; reason = "regime_exit"
                elif position["side"] == "Sell" and regime != MarketRegime.TRENDING_DOWN:
                    exit_px = price; reason = "regime_exit"
                if exit_px is not None:
                    equity, fee, pnl = self._close(equity, position, exit_px)
                    fees_total += fee
                    trades.append({
                        "side": position["side"], "entry": position["entry"], "exit": exit_px,
                        "qty": position["qty"], "pnl": round(pnl, 4), "reason": reason,
                    })
                    position = None

            # Entry (only from flat, only in a confirmed trend).
            if position is None and regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
                entry = evaluate_trend_entry(regime=regime, higher_tf_regime=None, require_htf=False)
                if entry.side:
                    stop = initial_trend_stop(entry.side, chandelier_long=cl, chandelier_short=cs)
                    qty = compute_fixed_fractional_qty(
                        equity=equity, risk_pct=s.risk_per_trade_pct, entry_price=price,
                        stop_price=stop.stop_price, qty_step=Decimal("0.0001"),
                        min_qty=Decimal("0.0001"), available_margin=equity, leverage=s.leverage,
                    )
                    if qty > 0:
                        fee = price * qty * self.taker_fee
                        fees_total += fee; equity -= fee
                        position = {"side": entry.side, "qty": qty, "entry": price, "stop": stop}

            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            equity_curve.append({"ts": str(ts), "equity": round(equity, 4), "price": price})

        # Close any open position at the last price (mark to market).
        if position is not None:
            last_price = float(enriched.iloc[-1]["close"])
            equity, fee, pnl = self._close(equity, position, last_price)
            fees_total += fee
            trades.append({"side": position["side"], "entry": position["entry"],
                           "exit": last_price, "qty": position["qty"], "pnl": round(pnl, 4),
                           "reason": "eod_close"})

        return {
            "metrics": self._metrics(trades, equity, max_dd, fees_total, funding_total),
            "trades": trades,
            "equity_curve": equity_curve,
        }

    def _close(self, equity: float, position: dict, exit_px: float) -> tuple[float, float, float]:
        sign = 1 if position["side"] == "Buy" else -1
        gross = (exit_px - position["entry"]) * position["qty"] * sign
        fee = exit_px * position["qty"] * self.taker_fee
        pnl = gross - fee
        return equity + pnl, fee, pnl

    def _metrics(self, trades, equity, max_dd, fees_total, funding_total) -> dict:
        n = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        net = equity - self.initial_capital
        return {
            "net_pnl": round(net, 4),
            "return_pct": round(net / self.initial_capital * 100, 2),
            "final_equity": round(equity, 2),
            "trades": n,
            "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "win_loss_ratio": round(avg_win / avg_loss, 2) if avg_loss else float("inf"),
            "expectancy_per_trade": round(net / n, 4) if n else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else float("inf"),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "fees_total": round(fees_total, 4),
            "funding_total": round(funding_total, 4),
            "note": "TREND mode only; range/transitional bars flat (grid income not modelled -> conservative).",
        }
