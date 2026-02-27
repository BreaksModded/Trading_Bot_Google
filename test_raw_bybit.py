import asyncio
import pprint
from core.exchange import BybitExchangeClient

async def run():
    keys = {
        "api_key": "IsZk7tMLC6r0epfRhi",
        "api_secret": "58yP6Xw9UePLrKHmEeZvesDtIFd7BiwVOWPh"
    }

    print("--- TESTING TESTNET ---")
    e_test = BybitExchangeClient(**keys, testnet=True, symbol='BTCUSDC', domain='bybit', tld='eu')
    c_test = e_test._ensure_http()
    try:
        w = await e_test._run_http(c_test.get_wallet_balance, accountType='UNIFIED')
        print("SUCCESS Testnet:")
        pprint.pprint(w)
    except Exception as exc:
        print("ERROR Testnet:", exc)

    print("\n--- TESTING MAINNET ---")
    e_main = BybitExchangeClient(**keys, testnet=False, symbol='BTCUSDC', domain='bybit', tld='eu')
    c_main = e_main._ensure_http()
    try:
        w = await e_main._run_http(c_main.get_wallet_balance, accountType='UNIFIED')
        print("SUCCESS Mainnet:")
        pprint.pprint(w)
    except Exception as exc:
        print("ERROR Mainnet:", exc)

if __name__ == "__main__":
    asyncio.run(run())
