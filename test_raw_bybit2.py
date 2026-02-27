import asyncio
import pprint
from core.exchange import BybitExchangeClient

async def run():
    keys = {
        "api_key": "IsZk7tMLC6r0epfRhi",
        "api_secret": "58yP6Xw9UePLrKHmEeZvesDtIFd7BiwVOWPh"
    }

    print("--- TESTING MAINNET COM ---")
    e_main = BybitExchangeClient(**keys, testnet=False, symbol='BTCUSDC', domain='bybit', tld='com')
    c_main = e_main._ensure_http()
    try:
        w = await e_main._run_http(c_main.get_wallet_balance, accountType='UNIFIED')
        print("SUCCESS Mainnet COM:")
        pprint.pprint(w)
    except Exception as exc:
        print("ERROR Mainnet COM:", exc)

if __name__ == "__main__":
    asyncio.run(run())
