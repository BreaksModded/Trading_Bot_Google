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

    rules = dict(qty_step="0.01", min_qty="0.01", funding_series=funding)
    bt = FuturesTrendBacktest(settings=Settings(), initial_capital=150.0)

    print(f"\n=== Futures TREND backtest: {symbol} {timeframe} ~{months}m ({len(df)} bars) ===")
    _line("FULL ", bt.run(df, **rules)["metrics"], verbose=True)

    # Walk-forward: train on the first 60%, validate OUT-OF-SAMPLE on the last 40%.
    split = int(len(df) * 0.6)
    train, test = df.iloc[:split].reset_index(drop=True), df.iloc[split:].reset_index(drop=True)
    tr = bt.run(train, **rules)["metrics"]
    te = bt.run(test, **rules)["metrics"]
    print("\n--- Walk-forward ---")
    _line("TRAIN", tr)
    _line("TEST ", te)

    # Sensitivity (anti-curve-fit): the edge should not collapse with small changes.
    print("\n--- Sensibilidad (robustez) ---")
    for mult in (2.5, 3.0, 3.5):
        for adx in (20.0, 25.0, 30.0):
            sset = Settings()
            sset.futures.chandelier_atr_mult = mult
            sset.futures.adx_trend_threshold = adx
            r = FuturesTrendBacktest(settings=sset, initial_capital=150.0).run(df, **rules)["metrics"]
            print(f"  Chandelier x{mult} / ADX>{adx:.0f}: net=${r['net_pnl']:>8} "
                  f"PF={r['profit_factor']:>6} trades={r['trades']:>3}")

    # Verdict against an explicit acceptance criterion.
    pf = te["profit_factor"]
    ok = te["net_pnl"] > 0 and te["trades"] >= 5 and (pf == float("inf") or pf > 1.0)
    print("\n=== VEREDICTO (out-of-sample) ===")
    print("✅ Hay edge: el TEST (datos NO vistos) es positivo con PF>1."
          if ok else "❌ Sin edge claro out-of-sample — NO desplegar tal cual.")
    print("Criterio: TEST net>0 y PF>1 con >=5 trades, y que la sensibilidad no se desplome.")
    print("\nRecuerda: el backtest descarta estrategias malas; el juez final es forward (real pequeño).")


def _line(title: str, m: dict, verbose: bool = False) -> None:
    print(f"{title}: net=${m['net_pnl']} ({m['return_pct']}%) trades={m['trades']} "
          f"win={m['win_rate_pct']}% PF={m['profit_factor']} W/L={m['win_loss_ratio']} "
          f"maxDD={m['max_drawdown_pct']}%")
    if verbose:
        print(f"   long ${m['long_pnl']} ({m['long_trades']}) | short ${m['short_pnl']} "
              f"({m['short_trades']}) | fees ${m['fees_total']} | funding ${m['funding_total']}")
        if m.get("time_in_regime_pct"):
            print(f"   tiempo en régimen: {m['time_in_regime_pct']}")


if __name__ == "__main__":
    main()
