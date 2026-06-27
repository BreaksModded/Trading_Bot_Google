"""Run the futures trend backtest on real Bybit history.

Usage:
    python scripts/backtest_futures.py [CCXT_SYMBOL] [MONTHS]
    python scripts/backtest_futures.py ETH/USDT 6

Notes:
    - Uses spot OHLCV as a faithful proxy for the perpetual's price path
      (they track within a fraction of a percent for ETH).
    - TREND mode only — a CONSERVATIVE lower bound (range/grid income excluded).
    - It runs the REAL strategy code (regime + entry + sizing + Chandelier).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from backtesting.data_loader import (
    download_funding_history, download_months, load_cache, save_cache,
)
from backtesting.futures_backtest import FuturesTrendBacktest
from config.settings import Settings


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "ETH/USDT"
    months = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    timeframe = "1h"
    quote = symbol.split("/")[1]
    perp = symbol if ":" in symbol else f"{symbol}:{quote}"
    since = (datetime.utcnow() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    cache_name = f"{symbol.replace('/', '')}_{timeframe}_{months}m"
    df = load_cache(cache_name)
    if df is None:
        df = download_months(months=months, symbol=symbol, timeframe=timeframe)
        save_cache(df, cache_name)

    funding = None
    try:
        funding = download_funding_history(symbol=perp, since=since)
    except Exception as exc:
        print(f"(funding history unavailable: {exc} — using a flat estimate)")

    bt = FuturesTrendBacktest(settings=Settings(), initial_capital=150.0)
    # Real ETH-perp granularity (qty_step / min_qty); edit for other symbols.
    result = bt.run(df, qty_step="0.01", min_qty="0.01", funding_series=funding)

    print(f"\n=== Futures TREND backtest: {symbol} {timeframe} ~{months}m "
          f"({len(df)} bars) ===")
    print(json.dumps(result["metrics"], indent=2))
    print("\nInterpretation: positive net_pnl + profit_factor > 1 + win_loss_ratio > 1.5 "
          "(despite win_rate < 50%) = the positive-skew design is working.")


if __name__ == "__main__":
    main()
