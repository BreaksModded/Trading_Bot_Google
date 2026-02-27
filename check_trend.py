import asyncio
from config.settings import Settings
from core.exchange import BybitExchangeClient
from core.strategy import GridStrategy, StrategyConfig

async def check_trend():
    s = Settings()
    e = BybitExchangeClient(
        api_key=s.exchange.api_key, 
        api_secret=s.exchange.api_secret, 
        testnet=s.exchange.testnet, 
        symbol='BTCUSDC', 
        timeframe='1',
        domain=s.exchange.domain,
        tld=s.exchange.tld
    )
    
    klines = await e.get_klines(symbol='BTCUSDC', limit=500)
    strategy_args = s.strategy_dict()
    strategy_args["symbol"] = 'BTCUSDC'
    strategy = GridStrategy(StrategyConfig(**strategy_args))
    signal = strategy.compute_signal(klines)
    
    print(f"Current Price: {signal.current_price}")
    print(f"Trend Bias: {signal.trend}")
    print(f"ADX: {signal.adx:.2f}")
    print(f"ATR: {signal.atr:.2f}")

if __name__ == '__main__':
    asyncio.run(check_trend())
