import asyncio
import json
from config.settings import Settings
from core.exchange import BybitExchangeClient

async def check_balance():
    s = Settings()
    e = BybitExchangeClient(
        api_key=s.exchange.api_key, 
        api_secret=s.exchange.api_secret, 
        testnet=s.exchange.testnet, 
        symbol='BTCUSDT', 
        timeframe='1',
        domain=s.exchange.domain,
        tld=s.exchange.tld
    )
    usdc = await e.get_balance("USDC")
    usdt = await e.get_balance("USDT")
    print(f"USDC Balance: {usdc}")
    print(f"USDT Balance: {usdt}")

if __name__ == '__main__':
    asyncio.run(check_balance())
