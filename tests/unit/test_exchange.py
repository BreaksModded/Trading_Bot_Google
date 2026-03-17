import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from decimal import Decimal

from core.exchange import BybitExchangeClient, ExchangeError, SpotSymbolRules

@pytest.fixture
def exchange():
    return BybitExchangeClient(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True,
        symbol="BTCUSDT",
    )

@pytest.fixture
def mock_http():
    with patch("core.exchange.BybitExchangeClient._ensure_http") as ensure_mock:
        client_mock = MagicMock()
        ensure_mock.return_value = client_mock
        yield client_mock

@pytest.mark.asyncio
async def test_run_http_retries_on_rate_limit(exchange):
    """A3/A4: Ensure _run_http retries on transient errors like 10006 (rate limit)."""
    func = MagicMock()
    # First two calls fail with transient rate limit error, third succeeds
    func.side_effect = [
        Exception("rate limit 10006 exceeded"),
        Exception("rate limit 10018 exceeded"),
        {"retCode": 0, "result": "success"}
    ]
    
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await exchange._run_http(func)
        
        assert func.call_count == 3
        assert mock_sleep.call_count == 2
        # First retry sleeps 2^0 = 1s, second retry sleeps 2^1 = 2s
        mock_sleep.assert_any_call(1)
        mock_sleep.assert_any_call(2)
        assert res["result"] == "success"

@pytest.mark.asyncio
async def test_run_http_raises_on_persistent_error(exchange):
    """Ensure _run_http raises ExchangeError after max retries."""
    func = MagicMock()
    func.side_effect = Exception("timeout 504")
    
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(Exception, match="timeout 504"):
            await exchange._run_http(func)
            
        assert func.call_count == 3

@pytest.mark.asyncio
async def test_get_portfolio_equity_uses_cached_price_on_failure(exchange, mock_http):
    """Ensure get_portfolio_equity falls back to cached prices if ticker fetch fails."""
    # Mock get_wallet_balance to return 1000 USDT and 1 BTC
    mock_http.get_wallet_balance.return_value = {
        "result": {
            "list": [{
                "coin": [
                    {"coin": "USDT", "walletBalance": "1000.0", "availableToWithdraw": "1000.0"},
                    {"coin": "BTC", "walletBalance": "1.0", "availableToWithdraw": "1.0"}
                ]
            }]
        }
    }
    
    # 1. First call: Success, caches price at 50000
    with patch.object(exchange, "get_last_price", new_callable=AsyncMock) as mock_price:
        mock_price.return_value = 50000.0
        total, free, balances = await exchange.get_portfolio_equity(symbols=["BTCUSDT"])
        
        assert total == 51000.0 # 1000 USDT + 50000 BTC
        assert free == 1000.0
        assert exchange._last_price_cache["BTCUSDT"] == 50000.0
    
    # 2. Second call: Ticker fails, uses cached price
    with patch.object(exchange, "get_last_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("API Timeout")
        total, free, balances = await exchange.get_portfolio_equity(symbols=["BTCUSDT"])
        
        assert total == 51000.0
        assert exchange._price_fail_counts["BTCUSDT"] == 1

@pytest.mark.asyncio
async def test_get_portfolio_equity_skips_on_first_failure(exchange, mock_http):
    """If there's no cache and ticker fails, it should exclude the asset safely."""
    mock_http.get_wallet_balance.return_value = {
        "result": {
            "list": [{
                "coin": [
                    {"coin": "USDT", "walletBalance": "1000.0"},
                    {"coin": "ETH", "walletBalance": "10.0"}
                ]
            }]
        }
    }
    
    with patch.object(exchange, "get_last_price", new_callable=AsyncMock) as mock_price:
        mock_price.side_effect = Exception("API Timeout")
        total, free, balances = await exchange.get_portfolio_equity(symbols=["ETHUSDT"])
        
        # Drops ETH valuation but keeps USDT
        assert total == 1000.0

@pytest.mark.asyncio
async def test_place_limit_order_normalizes_precision(exchange, mock_http):
    """Verify size and price are safely truncated down to symbol step sizes."""
    # Mock spot rules
    rules = SpotSymbolRules(qty_step=Decimal("0.001"), min_qty=Decimal("0.01"), tick_size=Decimal("0.5"))
    with patch.object(exchange, "get_spot_symbol_rules", new_callable=AsyncMock) as mock_rules:
        mock_rules.return_value = rules
        
        mock_http.place_order.return_value = {"result": {"orderId": "123"}}
        
        await exchange.place_limit_order(
            symbol="BTCUSDT", side="Buy", qty=0.1239, price=40000.8
        )
        
        # 0.1239 -> 0.123 (step 0.001)
        # 40000.8 -> 40000.5 (step 0.5)
        mock_http.place_order.assert_called_once_with(
            category="spot",
            symbol="BTCUSDT",
            side="Buy",
            orderType="Limit",
            qty="0.123",
            price="40000.5",
            timeInForce="PostOnly"
        )
