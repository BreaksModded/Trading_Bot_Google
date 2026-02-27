"""
Event-driven backtesting engine.

Simulates the grid trading strategy on historical data with
realistic commissions, slippage, and position tracking.
Supports walk-forward testing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import pandas_ta as ta
from loguru import logger


class BacktestTrade:
    """Record of a simulated trade."""

    def __init__(
        self,
        timestamp: datetime,
        side: str,
        price: float,
        quantity: float,
        fee: float,
        slippage: float,
        pnl: float = 0.0,
    ) -> None:
        self.timestamp = timestamp
        self.side = side
        self.price = price
        self.quantity = quantity
        self.fee = fee
        self.slippage = slippage
        self.pnl = pnl

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp),
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "fee": self.fee,
            "slippage": self.slippage,
            "pnl": self.pnl,
        }


class BacktestEngine:
    """
    Event-driven backtesting engine for grid trading strategies.

    Simulates order execution with realistic costs:
    - Maker fee: 0.01% (Bybit)
    - Estimated slippage: 0.01%

    Supports walk-forward testing: train on first portion, validate on last.

    Args:
        maker_fee_pct: Maker fee as decimal (0.0001 = 0.01%).
        slippage_pct: Estimated slippage as decimal.
        initial_capital: Starting capital in USDT.
    """

    MAKER_FEE: float = 0.0001   # 0.01%
    SLIPPAGE: float = 0.0001    # 0.01%

    def __init__(
        self,
        maker_fee_pct: float = 0.0001,
        slippage_pct: float = 0.0001,
        initial_capital: float = 150.0,
    ) -> None:
        self.maker_fee_pct = maker_fee_pct
        self.slippage_pct = slippage_pct
        self.initial_capital = initial_capital

    def run(
        self,
        df: pd.DataFrame,
        config: dict,
        progress_callback: Optional[callable] = None,
    ) -> dict:
        """
        Run backtest on historical OHLCV data.

        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume.
            config: Strategy configuration dict with grid parameters.
            progress_callback: Optional function(progress_pct) for UI updates.

        Returns:
            Dict with results: trades, metrics, equity_curve.
        """
        logger.info(f"Starting backtest: {len(df)} candles, capital={self.initial_capital}")

        min_lookback = max(config.get("ema_slow", 200), 201)
        if len(df) < min_lookback:
            raise ValueError(f"Need at least {min_lookback} candles (got {len(df)})")

        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        capital = self.initial_capital
        peak_capital = capital

        # Grid parameters
        num_levels = config.get("num_levels", 5)
        min_spacing = config.get("min_spacing_pct", 0.006)
        atr_multiplier = config.get("atr_multiplier", 1.5)
        adx_threshold = config.get("adx_threshold", 25)
        order_size = config.get("order_size_usdt", 25)
        ema_fast_p = config.get("ema_fast", 50)
        ema_slow_p = config.get("ema_slow", 200)

        # Active grid orders (simulated)
        buy_orders: list[dict] = []  # {"price": float, "qty": float}
        sell_orders: list[dict] = []

        total_bars = len(df)

        for i in range(min_lookback, total_bars):
            window = df.iloc[:i + 1].copy()
            current = df.iloc[i]
            price = float(current["close"])
            high = float(current["high"])
            low = float(current["low"])
            ts = current.get("timestamp", current.name)

            # Progress
            if progress_callback and i % 50 == 0:
                pct = (i - min_lookback) / (total_bars - min_lookback) * 100
                progress_callback(pct)

            # Compute indicators
            atr_series = ta.atr(window["high"], window["low"], window["close"], length=14)
            atr_val = float(atr_series.iloc[-1]) if atr_series is not None and not atr_series.dropna().empty else 0

            adx_df = ta.adx(window["high"], window["low"], window["close"], length=14)
            adx_val = float(adx_df["ADX_14"].iloc[-1]) if adx_df is not None and not adx_df.dropna().empty else 0

            ema_fast = ta.ema(window["close"], length=ema_fast_p)
            ema_slow = ta.ema(window["close"], length=ema_slow_p)

            # Check if market is trending
            if adx_val > adx_threshold:
                buy_orders.clear()
                sell_orders.clear()
                equity_curve.append({"timestamp": str(ts), "capital": capital, "price": price})
                continue

            # Compute spacing
            atr_pct = (atr_val / price) * atr_multiplier if price > 0 else min_spacing
            spacing = max(min_spacing, atr_pct)

            # Check fills against high/low of this candle
            filled_buys = [o for o in buy_orders if low <= o["price"]]
            filled_sells = [o for o in sell_orders if high >= o["price"]]

            for order in filled_buys:
                fill_p = order["price"] * (1 + self.slippage_pct)
                fee = order["qty"] * fill_p * self.maker_fee_pct
                buy_orders.remove(order)

                # Place counter sell
                sell_price = fill_p * (1 + spacing)
                sell_qty = order_size / sell_price
                sell_orders.append({"price": sell_price, "qty": sell_qty, "entry": fill_p})

                trades.append(BacktestTrade(
                    timestamp=ts, side="Buy", price=fill_p,
                    quantity=order["qty"], fee=fee,
                    slippage=fill_p * self.slippage_pct * order["qty"],
                ))

            for order in filled_sells:
                fill_p = order["price"] * (1 - self.slippage_pct)
                fee = order["qty"] * fill_p * self.maker_fee_pct
                entry_price = order.get("entry", fill_p * (1 - spacing))
                pnl = (fill_p - entry_price) * order["qty"] - fee
                capital += pnl

                sell_orders.remove(order)

                # Place counter buy
                buy_price = fill_p * (1 - spacing)
                buy_qty = order_size / buy_price
                buy_orders.append({"price": buy_price, "qty": buy_qty})

                trades.append(BacktestTrade(
                    timestamp=ts, side="Sell", price=fill_p,
                    quantity=order["qty"], fee=fee,
                    slippage=fill_p * self.slippage_pct * order["qty"],
                    pnl=pnl,
                ))

            # Place initial grid if empty
            if not buy_orders and not sell_orders:
                for j in range(1, num_levels + 1):
                    bp = price * (1 - spacing * j)
                    buy_orders.append({"price": bp, "qty": order_size / bp})

                    sp = price * (1 + spacing * j)
                    sell_orders.append({"price": sp, "qty": order_size / sp, "entry": price})

            # Track equity
            if capital > peak_capital:
                peak_capital = capital

            equity_curve.append({
                "timestamp": str(ts),
                "capital": round(capital, 4),
                "price": price,
            })

        # Compute final metrics
        metrics = self._compute_metrics(trades, capital, peak_capital)
        metrics["equity_curve_length"] = len(equity_curve)

        logger.info(
            f"Backtest complete: {metrics['total_trades']} trades, "
            f"net PnL={metrics['net_pnl']:.4f}, "
            f"max DD={metrics['max_drawdown_pct']:.2f}%"
        )

        return {
            "trades": [t.to_dict() for t in trades],
            "metrics": metrics,
            "equity_curve": equity_curve,
            "config": config,
        }

    def _compute_metrics(
        self, trades: list[BacktestTrade], final_capital: float, peak_capital: float
    ) -> dict:
        """Compute performance metrics from trade list."""
        if not trades:
            return {
                "total_trades": 0, "net_pnl": 0, "max_drawdown_pct": 0,
                "sharpe_ratio": 0, "win_rate": 0, "profit_factor": 0,
            }

        sell_trades = [t for t in trades if t.side == "Sell"]
        winning = [t for t in sell_trades if t.pnl > 0]
        losing = [t for t in sell_trades if t.pnl < 0]

        gross_pnl = sum(t.pnl for t in sell_trades)
        total_fees = sum(t.fee for t in trades)
        net_pnl = gross_pnl

        gross_profit = sum(t.pnl for t in winning) if winning else 0
        gross_loss = abs(sum(t.pnl for t in losing)) if losing else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max drawdown
        max_dd = 0.0
        running = self.initial_capital
        peak = running
        for t in sorted(trades, key=lambda x: x.timestamp):
            if t.side == "Sell":
                running += t.pnl
            if running > peak:
                peak = running
            dd = (peak - running) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        # Sharpe ratio (simplified: daily returns)
        pnl_list = [t.pnl for t in sell_trades if t.pnl != 0]
        sharpe = 0.0
        if len(pnl_list) > 1:
            import numpy as np
            returns = np.array(pnl_list)
            if returns.std() > 0:
                sharpe = float((returns.mean() / returns.std()) * (252 ** 0.5))

        return {
            "total_trades": len(trades),
            "sell_trades": len(sell_trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": round(len(winning) / len(sell_trades) * 100, 2) if sell_trades else 0,
            "gross_pnl": round(gross_pnl, 4),
            "total_fees": round(total_fees, 4),
            "net_pnl": round(net_pnl, 4),
            "avg_win": round(sum(t.pnl for t in winning) / len(winning), 4) if winning else 0,
            "avg_loss": round(abs(sum(t.pnl for t in losing) / len(losing)), 4) if losing else 0,
            "profit_factor": round(profit_factor, 4),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe_ratio": round(sharpe, 4),
            "final_capital": round(final_capital, 4),
            "capital_return_pct": round(
                (final_capital - self.initial_capital) / self.initial_capital * 100, 2
            ),
        }

    def walk_forward(
        self,
        df: pd.DataFrame,
        config: dict,
        train_ratio: float = 0.67,
    ) -> dict:
        """
        Walk-forward test: train on first portion, validate on remainder.

        Args:
            df: Full OHLCV dataset.
            config: Strategy configuration.
            train_ratio: Fraction of data for training (default 67% ≈ 4/6 months).

        Returns:
            Dict with train_results and test_results.
        """
        split_idx = int(len(df) * train_ratio)
        train_df = df.iloc[:split_idx].reset_index(drop=True)
        test_df = df.iloc[split_idx:].reset_index(drop=True)

        logger.info(
            f"Walk-forward: train={len(train_df)} candles, "
            f"test={len(test_df)} candles (ratio={train_ratio:.0%})"
        )

        train_results = self.run(train_df, config)
        test_results = self.run(test_df, config)

        return {
            "train": train_results,
            "test": test_results,
            "train_period": f"{train_df['timestamp'].iloc[0]} to {train_df['timestamp'].iloc[-1]}",
            "test_period": f"{test_df['timestamp'].iloc[0]} to {test_df['timestamp'].iloc[-1]}",
        }
