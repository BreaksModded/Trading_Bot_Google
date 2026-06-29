"""Faithful backtest for the futures TREND engine.

It drives the SAME decision function the live bot uses (`decide_trend`), so the
entry rule, fixed-fractional sizing and Chandelier stop cannot drift from
production. It also models the costs/filters the live bot is subject to:

  - higher-timeframe regime filter (resampled, no look-ahead)
  - real exchange qty_step / min_qty / min-notional viability
  - taker fee + slippage on entry, exit and stop fills
  - funding at the real 00/08/16 UTC settlements (historical rates when provided)
  - intra-bar stop fills (the bar's low/high vs the prior-bar stop) — conservative

Scope: TREND mode only (range/transitional bars are flat). At ~$150 the live bot
is trend-only anyway (the grid risk cap rejects the grid), so this matches it;
for larger capital the grid contribution is a separate, later simulation.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from core.indicators import compute_chandelier_exit, enrich_indicators
from core.regime import MarketRegime, classify_futures_regime
from core.trend import decide_trend

_SETTLEMENT_HOURS = (0, 8, 16)


class FuturesTrendBacktest:
    """Event-driven backtest of the trend engine (shares live decision logic)."""

    def __init__(
        self, *, settings, initial_capital: float = 150.0,
        taker_fee: float = 0.00055, slippage: float = 0.0002,
        funding_rate_8h: float = 0.0001,
    ) -> None:
        self.s = settings.futures
        self.initial_capital = initial_capital
        self.taker_fee = taker_fee
        self.slippage = slippage
        self.funding_rate_8h = funding_rate_8h

    # ── Higher-timeframe regime (no look-ahead) ───────────────────────

    def _htf_regime(self, df: pd.DataFrame):
        s = self.s
        if "timestamp" not in df.columns:
            return [MarketRegime.TRANSITIONAL] * len(df)
        htf_min = int(s.higher_timeframe)
        d = df.set_index(pd.DatetimeIndex(df["timestamp"]))
        # label/closed='right' => each HTF bar is stamped at its CLOSE, so a lower
        # bar only ever sees a fully-completed HTF bar (no future data).
        htf = d.resample(f"{htf_min}min", label="right", closed="right").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        if len(htf) < s.ema_slow:
            return [MarketRegime.TRANSITIONAL] * len(df)
        e = enrich_indicators(htf.reset_index(drop=True), ema_fast=s.ema_fast,
                              ema_slow=s.ema_slow, atr_period=s.atr_period, adx_period=s.adx_period)
        regs = [
            classify_futures_regime(a, ef, es, adx_trend=s.adx_trend_threshold, adx_range=s.adx_range_threshold)
            for a, ef, es in zip(e["adx"], e["ema_fast"], e["ema_slow"])
        ]
        htf_series = pd.Series(regs, index=htf.index)
        aligned = htf_series.reindex(d.index, method="ffill")
        return [r if isinstance(r, MarketRegime) else MarketRegime.TRANSITIONAL for r in aligned]

    def _funding_at(self, ts, funding_series) -> float | None:
        if ts is None or ts.hour not in _SETTLEMENT_HOURS or ts.minute != 0:
            return None
        if funding_series is not None and len(funding_series):
            try:
                r = funding_series.asof(ts)
                if pd.notna(r):
                    return float(r)
            except Exception:
                pass
        return self.funding_rate_8h

    # ── Run ───────────────────────────────────────────────────────────

    def run(self, df: pd.DataFrame, *, qty_step=None, min_qty=None, funding_series=None) -> dict:
        s = self.s
        qstep = Decimal(str(qty_step)) if qty_step is not None else Decimal("0.0001")
        mqty = Decimal(str(min_qty)) if min_qty is not None else Decimal("0.0001")

        enriched = enrich_indicators(df, ema_fast=s.ema_fast, ema_slow=s.ema_slow,
                                     atr_period=s.atr_period, adx_period=s.adx_period)
        ce = compute_chandelier_exit(df, period=s.chandelier_period,
                                     atr_mult=s.chandelier_atr_mult, atr_period=s.atr_period)
        htf_reg = self._htf_regime(df)
        warmup = max(s.ema_slow, s.chandelier_period) + 1
        if len(df) <= warmup:
            raise ValueError(f"need > {warmup} bars (got {len(df)})")

        close_a = enriched["close"].to_numpy(); high_a = enriched["high"].to_numpy()
        low_a = enriched["low"].to_numpy(); adx_a = enriched["adx"].to_numpy()
        ef_a = enriched["ema_fast"].to_numpy(); es_a = enriched["ema_slow"].to_numpy()
        cl_a = ce["chandelier_long"].to_numpy(); cs_a = ce["chandelier_short"].to_numpy()
        ts_a = pd.DatetimeIndex(df["timestamp"]) if "timestamp" in df.columns else None

        equity = self.initial_capital
        pos = None  # {"side","qty","entry","stop"}
        trades: list[dict] = []
        equity_curve: list[dict] = []
        fees_total = funding_total = 0.0
        peak = equity; max_dd = 0.0
        regime_counts: dict = {}
        prev_regime = MarketRegime.TRANSITIONAL   # hysteresis state (audit H4)

        for i in range(warmup, len(df)):
            close = float(close_a[i]); high = float(high_a[i]); low = float(low_a[i])
            ts = ts_a[i] if ts_a is not None else None
            regime = classify_futures_regime(
                float(adx_a[i]), float(ef_a[i]), float(es_a[i]),
                adx_trend=s.adx_trend_threshold, adx_range=s.adx_range_threshold,
                current=prev_regime,   # hysteresis: same sticky behaviour as live
            )
            prev_regime = regime
            regime_htf = htf_reg[i]
            regime_counts[regime] = regime_counts.get(regime, 0) + 1
            cl = float(cl_a[i]); cs = float(cs_a[i])

            # Funding at the real settlement times.
            if pos is not None:
                rate = self._funding_at(ts, funding_series)
                if rate is not None:
                    pay = pos["qty"] * close * rate
                    if pos["side"] == "Buy":
                        equity -= pay; funding_total += pay
                    else:
                        equity += pay; funding_total -= pay

            # Manage an open position.
            if pos is not None:
                desired = MarketRegime.TRENDING_UP if pos["side"] == "Buy" else MarketRegime.TRENDING_DOWN
                stop_px = pos["stop"].stop_price
                hit = (pos["side"] == "Buy" and low <= stop_px) or (pos["side"] == "Sell" and high >= stop_px)
                if hit:  # intra-bar stop fill against the prior-bar stop level
                    equity, fee = self._exit(equity, pos, stop_px, "chandelier_stop", trades)
                    fees_total += fee; pos = None
                elif regime != desired:
                    reason = "trend_reversal" if regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN) else "regime_exit"
                    equity, fee = self._exit(equity, pos, close, reason, trades)
                    fees_total += fee; pos = None
                else:
                    pos["stop"].update(chandelier_long=cl, chandelier_short=cs)  # ratchet for next bar

            # Entry — the SAME decision the live bot makes.
            if pos is None and regime in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN):
                d = decide_trend(
                    regime=regime, regime_htf=regime_htf, position_side="flat", position_flat=True,
                    chandelier_long=cl, chandelier_short=cs, live_price=close, equity=equity,
                    available_margin=equity, trend_stop=None, risk_pct=s.risk_per_trade_pct,
                    leverage=s.leverage, require_htf=s.require_higher_tf_confirmation,
                    qty_step=qstep, min_qty=mqty,
                )
                if d.action == "enter" and d.qty * close >= s.min_order_usdt:  # live viability
                    fill = close * (1 + self.slippage) if d.side == "Buy" else close * (1 - self.slippage)
                    fee = fill * d.qty * self.taker_fee
                    fees_total += fee; equity -= fee
                    pos = {"side": d.side, "qty": d.qty, "entry": fill, "stop": d.stop}

            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak if peak > 0 else 0.0)
            equity_curve.append({"ts": str(ts), "equity": round(equity, 4), "price": close})

        if pos is not None:
            equity, fee = self._exit(equity, pos, float(close_a[-1]), "eod_close", trades)
            fees_total += fee

        return {
            "metrics": self._metrics(trades, equity, max_dd, fees_total, funding_total, regime_counts),
            "trades": trades,
            "equity_curve": equity_curve,
        }

    def _exit(self, equity, pos, raw_price, reason, trades) -> tuple[float, float]:
        fill = raw_price * (1 - self.slippage) if pos["side"] == "Buy" else raw_price * (1 + self.slippage)
        sign = 1 if pos["side"] == "Buy" else -1
        gross = (fill - pos["entry"]) * pos["qty"] * sign
        fee = fill * pos["qty"] * self.taker_fee
        pnl = gross - fee
        trades.append({"side": pos["side"], "entry": round(pos["entry"], 4), "exit": round(fill, 4),
                       "qty": pos["qty"], "pnl": round(pnl, 4), "reason": reason})
        return equity + pnl, fee

    def _metrics(self, trades, equity, max_dd, fees_total, funding_total, regime_counts=None) -> dict:
        n = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        gp = sum(t["pnl"] for t in wins); gl = abs(sum(t["pnl"] for t in losses))
        avg_win = gp / len(wins) if wins else 0.0
        avg_loss = gl / len(losses) if losses else 0.0
        net = equity - self.initial_capital
        longs = [t for t in trades if t["side"] == "Buy"]
        shorts = [t for t in trades if t["side"] == "Sell"]
        m = {
            "net_pnl": round(net, 4),
            "return_pct": round(net / self.initial_capital * 100, 2),
            "final_equity": round(equity, 2),
            "trades": n,
            "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "win_loss_ratio": round(avg_win / avg_loss, 2) if avg_loss else float("inf"),
            "expectancy_per_trade": round(net / n, 4) if n else 0.0,
            "profit_factor": round(gp / gl, 3) if gl else float("inf"),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "fees_total": round(fees_total, 4),
            "funding_total": round(funding_total, 4),
            "long_pnl": round(sum(t["pnl"] for t in longs), 4), "long_trades": len(longs),
            "short_pnl": round(sum(t["pnl"] for t in shorts), 4), "short_trades": len(shorts),
            "note": "TREND mode only (range/transitional flat); shares live decide_trend; "
                    "real fees+slippage+funding+HTF filter -> faithful to the live bot.",
        }
        if regime_counts:
            total = sum(regime_counts.values()) or 1
            m["time_in_regime_pct"] = {k.value: round(v / total * 100, 1) for k, v in regime_counts.items()}
        return m
