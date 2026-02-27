"""
OHLCV data loader for backtesting.

Downloads historical candlestick data from Bybit via ccxt,
with local caching to avoid repeated API calls.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger


CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


def download_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    since: Optional[str] = None,
    limit: int = 1000,
    exchange_id: str = "bybit",
) -> pd.DataFrame:
    """
    Download OHLCV data from Bybit via ccxt.

    Args:
        symbol: Trading pair (e.g., "BTC/USDT").
        timeframe: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d").
        since: Start date as "YYYY-MM-DD" string. If None, fetches latest.
        limit: Max candles per request (up to 1000).
        exchange_id: Exchange ID for ccxt.

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    import ccxt

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})

    since_ts = None
    if since:
        since_ts = int(datetime.strptime(since, "%Y-%m-%d").timestamp() * 1000)

    logger.info(f"Downloading {symbol} {timeframe} data (limit={limit})...")

    all_data: list = []
    current_since = since_ts

    while True:
        batch = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            since=current_since,
            limit=limit,
        )

        if not batch:
            break

        all_data.extend(batch)

        if len(batch) < limit:
            break

        # Move to after the last candle
        current_since = batch[-1][0] + 1

        if len(all_data) >= 10000:
            break

    df = pd.DataFrame(
        all_data,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    logger.info(f"Downloaded {len(df)} candles ({df['timestamp'].min()} to {df['timestamp'].max()})")
    return df


def download_months(
    months: int = 6,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> pd.DataFrame:
    """
    Download N months of historical data.

    Args:
        months: Number of months of history.
        symbol: Trading pair.
        timeframe: Candle interval.

    Returns:
        DataFrame with OHLCV data.
    """
    from datetime import timedelta

    since_date = (datetime.utcnow() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    return download_ohlcv(symbol=symbol, timeframe=timeframe, since=since_date)


def save_cache(df: pd.DataFrame, name: str) -> Path:
    """Save DataFrame to local cache as parquet."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.parquet"
    df.to_parquet(path, index=False)
    logger.info(f"Cached {len(df)} rows to {path}")
    return path


def load_cache(name: str) -> Optional[pd.DataFrame]:
    """Load DataFrame from local cache."""
    path = CACHE_DIR / f"{name}.parquet"
    if path.exists():
        df = pd.read_parquet(path)
        logger.info(f"Loaded {len(df)} rows from cache: {path}")
        return df
    return None
