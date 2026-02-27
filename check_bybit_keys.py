import os
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

def check_keys():
    load_dotenv()
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret or api_key == "tu_api_key_de_testnet":
        print("[ERROR] API keys not found or are default values in .env")
        return

    print(f"Testing API Key: {api_key[:4]}...{api_key[-4:]}\n")

    environments = [
        {"name": "Standard Testnet (testnet.bybit.com)", "kwargs": {"testnet": True, "demo": False}},
        {"name": "Demo Trading (bybit.com/demo)", "kwargs": {"testnet": False, "demo": True}},
        {"name": "Mainnet (bybit.com)", "kwargs": {"testnet": False, "demo": False}},
    ]

    for env in environments:
        print(f"Testing environment: {env['name']}...")
        try:
            session = HTTP(
                api_key=api_key,
                api_secret=api_secret,
                **env['kwargs']
            )
            
            # Use get_api_key_information first as it's the most basic auth endpoint
            resp = session.get_api_key_information()
            
            if resp.get("retCode") == 0:
                print(f"[SUCCESS] The keys belong to {env['name']}")
                
                # Check permissions
                info = resp.get("result", {})
                permissions = info.get("permissions", {})
                print("   Permissions:")
                for k, v in permissions.items():
                    if v:
                        print(f"      - {k}: {v}")
                
                # Check Unified Trading Account
                is_uta = info.get("uta", 0) == 1
                if not is_uta:
                    print("   [WARNING] This is NOT a Unified Trading Account (UTA). The bot requires UTA.")
                else:
                    print("   [SUCCESS] Validated as Unified Trading Account (UTA)")
                    
                break
            else:
                print(f"[FAILED] {resp.get('retMsg')} (code: {resp.get('retCode')})")
        except Exception as e:
            msg = str(e)
            if "Http status code is not 200" in msg or "ErrCode: 10003" in msg or "ErrCode: 10004" in msg:
                print(f"[FAILED] API Key invalid for this environment.")
            else:
                print(f"[FAILED] with error: {msg.splitlines()[0]}")
        print()

if __name__ == "__main__":
    check_keys()
