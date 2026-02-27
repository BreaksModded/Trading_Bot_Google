import asyncio
import pprint
import sys
from pathlib import Path

# Add PROYECTO2 to path
sys.path.insert(0, r"c:\Users\Diego\Documents\proyecto_bot\PROYECTO2\proyecto_bot_vs")

from core.exchange import BybitExchangeClient
from config.settings import Settings

async def run():
    s = Settings()
    print(f"PROYECTO 2 Keys config: {s.exchange.api_key}")
    print(f"PROYECTO 2 Testnet Setting: {s.exchange.testnet}")
    
    e = BybitExchangeClient(
        api_key=s.exchange.api_key, 
        api_secret=s.exchange.api_secret, 
        testnet=s.exchange.testnet, 
        symbol='BTCUSDC', 
        timeframe='1'
    )
    c = e._ensure_http()
    
    try:
        w = await e._run_http(c.get_wallet_balance, accountType='UNIFIED')
        print("PROYECTO 2 WALLET SUCCESS:")
        pprint.pprint(w)
    except Exception as exc:
        print("PROYECTO 2 WALLET ERROR:", exc)

if __name__ == "__main__":
    asyncio.run(run())
