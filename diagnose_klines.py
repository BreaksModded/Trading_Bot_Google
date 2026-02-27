"""Diagnostic: check raw kline data and indicators for BTCUSDC."""
import asyncio
from config.settings import Settings
from core.exchange import BybitExchangeClient
from core.indicators import enrich_indicators

async def diagnose():
    s = Settings()
    e = BybitExchangeClient(
        api_key=s.exchange.api_key,
        api_secret=s.exchange.api_secret,
        testnet=s.exchange.testnet,
        symbol='BTCUSDC',
        timeframe='1',
        domain=s.exchange.domain,
        tld=s.exchange.tld,
    )
    klines = await e.get_klines(symbol='BTCUSDC', limit=500)
    print(f"Klines shape: {klines.shape}")
    print(f"Klines dtypes:\n{klines.dtypes}")
    print(f"\nLast 5 rows (raw):")
    print(klines[['open','high','low','close','volume']].tail(5).to_string())
    
    # Check if high == low == close (flat data)
    flat_rows = (klines['high'] == klines['low']).sum()
    print(f"\nFlat rows (high==low): {flat_rows} / {len(klines)}")
    
    enriched = enrich_indicators(klines, ema_fast=50, ema_slow=200)
    print(f"\nLast 3 enriched rows:")
    print(enriched[['close','atr','atr_pct','adx','volume_ratio','ema_fast','ema_slow']].tail(3).to_string())

if __name__ == '__main__':
    asyncio.run(diagnose())
