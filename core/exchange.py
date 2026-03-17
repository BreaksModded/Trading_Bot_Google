"""Bybit exchange wrapper for REST and WebSocket connectivity.

Provides an asynchronous interface over the synchronous pybit SDK,
with Decimal-based order precision and configurable domain/TLD for EU.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

import pandas as pd
from loguru import logger

from config.settings import Settings


MarketCallback = Callable[[dict[str, Any]], Awaitable[None]]
PrivateCallback = Callable[[dict[str, Any]], Awaitable[None]]
FailureCallback = Callable[[str], Awaitable[None]]


class ExchangeError(RuntimeError):
    """Generic exchange operation error."""


@dataclass(slots=True)
class SpotSymbolRules:
    """Spot symbol precision/limit rules used for safe order formatting."""

    qty_step: Decimal
    min_qty: Decimal
    tick_size: Decimal


class BybitExchangeClient:
    """Thin asynchronous wrapper around the official pybit SDK."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        testnet: bool,
        symbol: str,
        category: str = "spot",
        timeframe: str = "1",
        domain: str = "bybit",
        tld: str = "com",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.symbol = symbol
        self.category = category
        self.timeframe = timeframe
        self.domain = domain
        self.tld = tld

        self._http_client: Any = None
        self._public_ws: Any = None
        self._private_ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_ws = asyncio.Event()
        self._public_task: asyncio.Task[None] | None = None
        self._private_task: asyncio.Task[None] | None = None

        self._market_callback: MarketCallback | None = None
        self._private_callback: PrivateCallback | None = None
        self._failure_callback: FailureCallback | None = None
        self._symbol_rules_cache: dict[str, SpotSymbolRules] = {}
        self._last_price_cache: dict[str, float] = {}
        self._price_fail_counts: dict[str, int] = {}
        self._public_ws_symbols: list[str] = [symbol]
        # FIX #5: Use a semaphore instead of a lock to allow concurrent requests while respecting rate limits
        # Bybit standard REST limits are generally 20-50 req/s depending on VIP/category. Limit to 10 safe concurrency.
        self._http_semaphore = asyncio.Semaphore(10)

    @staticmethod
    def _load_pybit() -> tuple[Any, Any]:
        try:
            from pybit import unified_trading as unified_trading_module
            from pybit.unified_trading import HTTP, WebSocket
        except ImportError as exc:
            raise ExchangeError(
                "pybit is not installed. Run `pip install -r requirements.txt`."
            ) from exc
        # pybit hardcodes `.com` in some releases; patch template to honor configurable TLD.
        unified_trading_module.PUBLIC_WSS = (
            "wss://{SUBDOMAIN}.{DOMAIN}.{TLD}/v5/public/{CHANNEL_TYPE}"
        )
        return HTTP, WebSocket

    def _ensure_http(self) -> Any:
        if self._http_client is not None:
            return self._http_client
        http_cls, _ = self._load_pybit()
        self._http_client = http_cls(
            testnet=self.testnet,
            api_key=self.api_key,
            api_secret=self.api_secret,
            domain=self.domain,
            tld=self.tld,
            recv_window=20000,
        )
        return self._http_client

    async def _run_http(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        async with self._http_semaphore:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = await asyncio.to_thread(func, *args, **kwargs)
                    
                    # A4: Detect Bybit-specific rate limit codes
                    if isinstance(response, dict):
                        retCode = response.get("retCode", 0)
                        retMsg = response.get("retMsg", "")
                        if retCode in (10006, 10018):
                            raise ExchangeError(f"Rate limit exceeded ({retCode}): {retMsg}")
                    return response
                except Exception as exc:
                    if attempt == max_retries - 1:
                        logger.error("HTTP request failed after {} attempts: {}", max_retries, exc)
                        raise
                    
                    err_str = str(exc).lower()
                    # A3: Retry on transient issues and rate limits
                    is_transient = any(
                        x in err_str for x in 
                        ["rate limit", "10002", "10006", "10018", "timeout", "connection", "500", "502", "503", "504", "retryable"]
                    )
                    if is_transient:
                        sleep_time = 2 ** attempt
                        logger.warning("HTTP transient error (retry {}/{} in {}s): {}", attempt + 1, max_retries, sleep_time, exc)
                        await asyncio.sleep(sleep_time)
                    else:
                        raise

    # ── REST: Account ─────────────────────────────────────────────────

    async def get_balance(self, coin: str = "USDT") -> float:
        """Return available wallet balance for a single coin."""
        client = self._ensure_http()
        response = await self._run_http(
            client.get_wallet_balance, accountType="UNIFIED", coin=coin,
        )
        wallet_list = response.get("result", {}).get("list", [])
        if not wallet_list:
            return 0.0
        coins = wallet_list[0].get("coin", [])
        for item in coins:
            if item.get("coin") == coin:
                return float(item.get("walletBalance") or 0.0)
        return 0.0

    async def get_balances(self, coins: list[str] | None = None) -> dict[str, float]:
        """Fetch multiple coin balances in a single API call.

        Args:
            coins: Specific coins to return. If None, returns all coins with balance > 0.

        Returns:
            Dict mapping coin ticker to wallet balance (e.g. {"USDT": 5000.0, "BTC": 0.1}).
        """
        client = self._ensure_http()
        response = await self._run_http(
            client.get_wallet_balance, accountType="UNIFIED",
        )
        wallet_list = response.get("result", {}).get("list", [])
        if not wallet_list:
            return {}
        coin_list = wallet_list[0].get("coin", [])
        result: dict[str, float] = {}
        for item in coin_list:
            ticker = item.get("coin", "")
            balance = float(item.get("walletBalance", 0.0) or 0.0)
            if coins is not None and ticker not in coins:
                continue
            if balance > 0 or (coins is not None and ticker in coins):
                result[ticker] = balance
        return result

    async def get_portfolio_equity(
        self, *, symbols: list[str], quote_coin: str = "USDT",
    ) -> tuple[float, float, dict[str, float]]:
        """Mark-to-market equity: returns (total_equity, free_quote_balance, all_free_balances)."""
        client = self._ensure_http()
        response = await self._run_http(
            client.get_wallet_balance, accountType="UNIFIED",
        )
        wallet_list = response.get("result", {}).get("list", [])
        if not wallet_list:
            return 0.0, 0.0, {}

        coins_data = wallet_list[0].get("coin", [])
        balances_total: dict[str, float] = {}
        balances_free: dict[str, float] = {}
        
        for item in coins_data:
            coin = item.get("coin", "")
            total_bal = float(item.get("walletBalance") or 0.0)
            # Available balance (not locked in orders).
            # FIX-BUG-8b: convert each field to float BEFORE the `or` chain so that
            # the string "0" (truthy in Python) doesn't short-circuit to 0.0 and
            # block the fallthrough to walletBalance.
            free_bal = (
                float(item.get("availableToWithdraw") or 0.0)
                or float(item.get("availableToBorrow") or 0.0)
                or float(item.get("walletBalance") or 0.0)
            )
            if total_bal > 0:
                balances_total[coin] = total_bal
            if free_bal > 0:
                balances_free[coin] = free_bal

        # Start with quote coin balance
        total_equity = balances_total.get(quote_coin, 0.0)
        free_quote_balance = balances_free.get(quote_coin, 0.0)

        # Add mark-to-market value of base assets
        for symbol in symbols:
            base_coin = symbol.upper().replace(quote_coin, "")
            base_balance = balances_total.get(base_coin, 0.0)
            if base_balance <= 0:
                continue
            try:
                price = await self.get_last_price(symbol=symbol)
                total_equity += base_balance * price
                self._last_price_cache[symbol] = price
                self._price_fail_counts[symbol] = 0
            except Exception as exc:
                self._price_fail_counts[symbol] = self._price_fail_counts.get(symbol, 0) + 1
                fail_count = self._price_fail_counts[symbol]
                
                cached_price = self._last_price_cache.get(symbol)
                if cached_price is not None:
                    logger.warning(
                        "{}: Ticker fetch failed ({} faults). Using cached price {:.2f}. Error: {}", 
                        symbol, fail_count, cached_price, exc
                    )
                    total_equity += base_balance * cached_price
                else:
                    logger.warning(
                        "{}: Ticker fetch failed ({} faults). No cached price available, skipping. Error: {}", 
                        symbol, fail_count, exc
                    )
                    
                if fail_count >= 3:
                    logger.error(
                        "{}: Ticker failed {} consecutive times. Potential critical connectivity issue.", 
                        symbol, fail_count
                    )
                    
        return total_equity, free_quote_balance, balances_free

    async def get_orderbook(
        self, *, symbol: str, limit: int = 5,
    ) -> dict[str, Any]:
        """Fetch top-of-book for spread and depth checks.

        Returns dict with keys: 'bids', 'asks' (list of [price, qty]),
        'best_bid', 'best_ask', 'spread_pct', 'bid_depth_usdt'.
        """
        client = self._ensure_http()
        response = await self._run_http(
            client.get_orderbook, category=self.category, symbol=symbol, limit=limit,
        )
        result = response.get("result", {})
        bids = [[float(p), float(q)] for p, q in result.get("b", [])]
        asks = [[float(p), float(q)] for p, q in result.get("a", [])]

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 1.0
        spread_pct = (best_ask - best_bid) / mid if mid else 0.0
        bid_depth_usdt = sum(p * q for p, q in bids)

        return {
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_depth_usdt": bid_depth_usdt,
        }

    async def ping(self) -> float:
        """Return API latency estimate in milliseconds."""
        client = self._ensure_http()
        started = datetime.now(UTC)
        await self._run_http(client.get_server_time)
        elapsed = datetime.now(UTC) - started
        return elapsed.total_seconds() * 1000

    async def test_connection(self) -> dict[str, Any]:
        """Test connection and return API key info."""
        client = self._ensure_http()
        return await self._run_http(client.get_api_key_information)

    # ── REST: Market Data ─────────────────────────────────────────────

    async def get_last_price(self, symbol: str | None = None) -> float:
        """Fetch latest traded price for symbol."""
        target = symbol or self.symbol
        client = self._ensure_http()
        response = await self._run_http(
            client.get_tickers, category=self.category, symbol=target,
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise ExchangeError(f"No ticker data received for {target}")
        return float(rows[0]["lastPrice"])

    async def get_ticker_snapshot(self, *, symbol: str | None = None) -> dict[str, float | str]:
        """Return normalized spot ticker summary for dashboard cards."""
        target = symbol or self.symbol
        client = self._ensure_http()
        response = await self._run_http(
            client.get_tickers, category=self.category, symbol=target,
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise ExchangeError(f"No ticker data received for {target}")
        row = rows[0]
        return {
            "symbol": target,
            "last_price": float(row.get("lastPrice", 0.0) or 0.0),
            "change_24h_pct": float(row.get("price24hPcnt", 0.0) or 0.0),
            "high_24h": float(row.get("highPrice24h", 0.0) or 0.0),
            "low_24h": float(row.get("lowPrice24h", 0.0) or 0.0),
            "volume_24h": float(row.get("volume24h", 0.0) or 0.0),
            "turnover_24h": float(row.get("turnover24h", 0.0) or 0.0),
        }

    async def get_klines(
        self,
        *,
        symbol: str | None = None,
        interval: str | None = None,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Get kline history as normalized DataFrame for indicators."""
        target_symbol = symbol or self.symbol
        target_interval = interval or self.timeframe
        client = self._ensure_http()
        response = await self._run_http(
            client.get_kline,
            category=self.category,
            symbol=target_symbol,
            interval=target_interval,
            limit=limit,
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise ExchangeError("No kline data received from exchange.")

        frame = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        frame = frame.astype({
            "timestamp": "int64",
            "open": "float64", "high": "float64",
            "low": "float64", "close": "float64",
            "volume": "float64", "turnover": "float64",
        })
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        return frame

    # ── REST: Symbol Rules & Precision ────────────────────────────────

    async def get_spot_symbol_rules(self, symbol: str) -> SpotSymbolRules:
        """Fetch and cache spot precision rules for a symbol."""
        normalized = symbol.upper().replace("/", "")
        if normalized in self._symbol_rules_cache:
            return self._symbol_rules_cache[normalized]

        client = self._ensure_http()
        response = await self._run_http(
            client.get_instruments_info, category=self.category, symbol=normalized,
        )
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise ExchangeError(f"No instrument rules found for {normalized}")

        raw = rows[0]
        lot = raw.get("lotSizeFilter", {})
        price_filter = raw.get("priceFilter", {})

        qty_step = Decimal(str(lot.get("qtyStep") or lot.get("basePrecision") or "0.00000001"))
        min_qty = Decimal(str(lot.get("minOrderQty") or qty_step))
        tick_size = Decimal(str(price_filter.get("tickSize") or "0.01"))

        rules = SpotSymbolRules(qty_step=qty_step, min_qty=min_qty, tick_size=tick_size)
        self._symbol_rules_cache[normalized] = rules
        return rules

    @staticmethod
    def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        units = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step

    @staticmethod
    def _decimal_to_plain_str(value: Decimal) -> str:
        plain = format(value.normalize(), "f")
        if "." in plain:
            plain = plain.rstrip("0").rstrip(".")
        return plain if plain else "0"

    # ── REST: Orders ──────────────────────────────────────────────────

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: float, price: float, orderLinkId: str | None = None,
    ) -> str:
        """Place a post-only limit order and return exchange order ID."""
        client = self._ensure_http()
        rules = await self.get_spot_symbol_rules(symbol)
        normalized_qty = self._round_down_to_step(Decimal(str(qty)), rules.qty_step)
        normalized_price = self._round_down_to_step(Decimal(str(price)), rules.tick_size)

        if normalized_qty < rules.min_qty:
            raise ExchangeError(
                f"Order qty below minimum after normalization: "
                f"{normalized_qty} < {rules.min_qty} for {symbol}"
            )

        params = dict(
            category=self.category,
            symbol=symbol,
            side=side,
            orderType="Limit",
            qty=self._decimal_to_plain_str(normalized_qty),
            price=self._decimal_to_plain_str(normalized_price),
            timeInForce="PostOnly",
        )
        if orderLinkId:
            params["orderLinkId"] = orderLinkId

        response = await self._run_http(
            client.place_order,
            **params
        )
        order_id = response.get("result", {}).get("orderId")
        if not order_id:
            raise ExchangeError(f"Failed to place order: {response}")
        return str(order_id)

    async def get_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        """Return open limit orders."""
        target = symbol or self.symbol
        client = self._ensure_http()
        response = await self._run_http(
            client.get_open_orders, category=self.category, symbol=target,
        )
        return response.get("result", {}).get("list", [])

    async def get_order_history(self, *, symbol: str, order_id: str) -> dict[str, Any] | None:
        """Fetch historical status of a specific order by ID."""
        client = self._ensure_http()
        try:
            response = await self._run_http(
                client.get_order_history, category=self.category, symbol=symbol, orderId=order_id,
            )
            orders = response.get("result", {}).get("list", [])
            return orders[0] if orders else None
        except Exception as exc:
            logger.warning("{}: order_history check failed for {}: {}", symbol, order_id, exc)
            return None

    async def cancel_order(self, *, symbol: str, order_id: str) -> None:
        """Cancel one order by ID."""
        client = self._ensure_http()
        await self._run_http(
            client.cancel_order, category=self.category, symbol=symbol, orderId=order_id,
        )

    async def cancel_all_orders(self, *, symbol: str | None = None) -> None:
        """Cancel all open orders for current symbol."""
        target = symbol or self.symbol
        client = self._ensure_http()
        await self._run_http(client.cancel_all_orders, category=self.category, symbol=target)

    async def place_market_order(
        self, *, symbol: str, side: str, qty: float,
    ) -> str:
        """Place an immediate market order and return exchange order ID.

        Bybit Spot UTA specifics:
        - SELL: qty is in base asset (e.g. BTC); no marketUnit needed.
        - BUY:  qty is in quote asset (e.g. USDC); marketUnit="quoteCoinQty".
        - PostOnly / timeInForce must NOT be set for market orders.
        - retCode != 0 is logged as error but the result dict is returned
          so the caller can decide how to handle it (same as place_limit_order).
        """
        client = self._ensure_http()
        rules = await self.get_spot_symbol_rules(symbol)
        normalized_qty = self._round_down_to_step(Decimal(str(qty)), rules.qty_step)

        if normalized_qty < rules.min_qty:
            raise ExchangeError(
                f"Market order qty below minimum after normalization: "
                f"{normalized_qty} < {rules.min_qty} for {symbol}"
            )

        params: dict[str, Any] = dict(
            category=self.category,
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=self._decimal_to_plain_str(normalized_qty),
        )
        if side == "Buy":
            # Bybit Spot: BUY market qty is interpreted as quote coin amount
            params["marketUnit"] = "quoteCoinQty"

        logger.info("{}: placing market {} order qty={}", symbol, side, params["qty"])

        response = await self._run_http(client.place_order, **params)
        if response.get("retCode") != 0:
            logger.error(
                "{}: market order rejected — retCode={} retMsg={}",
                symbol, response.get("retCode"), response.get("retMsg"),
            )
        order_id = response.get("result", {}).get("orderId", "")
        return str(order_id)

    async def set_stop_loss_hard(
        self, *, symbol: str, trigger_price: float, qty: float,
    ) -> None:
        """Create a reduce-only stop market order as hard stop-loss on exchange."""
        client = self._ensure_http()
        
        # FIX #6: Use dynamic symbol precision rules for stop loss
        rules = await self.get_spot_symbol_rules(symbol)
        normalized_qty = self._round_down_to_step(Decimal(str(qty)), rules.qty_step)
        normalized_price = self._round_down_to_step(Decimal(str(trigger_price)), rules.tick_size)

        await self._run_http(
            client.place_order,
            category=self.category,
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=self._decimal_to_plain_str(normalized_qty),
            triggerPrice=self._decimal_to_plain_str(normalized_price),
            triggerDirection=2,
            orderFilter="StopOrder",
        )

    # ── WebSocket Management ──────────────────────────────────────────

    def set_failure_callback(self, callback: FailureCallback) -> None:
        self._failure_callback = callback

    async def start_websockets(
        self,
        *,
        market_callback: MarketCallback,
        private_callback: PrivateCallback | None = None,
        symbols: list[str] | None = None,
    ) -> None:
        """Start public and private websocket loops."""
        self._loop = asyncio.get_running_loop()
        self._market_callback = market_callback
        self._private_callback = private_callback
        if symbols:
            self._public_ws_symbols = [s.upper().replace("/", "") for s in symbols]
        else:
            self._public_ws_symbols = [self.symbol]
        self._stop_ws.clear()
        self._public_task = asyncio.create_task(
            self._run_public_ws(), name="bybit-public-ws",
        )
        if self.api_key and self.api_secret and private_callback is not None:
            self._private_task = asyncio.create_task(
                self._run_private_ws(), name="bybit-private-ws",
            )

    async def stop_websockets(self) -> None:
        """Stop websocket loops gracefully."""
        self._stop_ws.set()
        tasks = [t for t in [self._public_task, self._private_task] if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _dispatch_market(self, message: dict[str, Any]) -> None:
        if not self._loop or not self._market_callback:
            return
        
        async def _safe_call() -> None:
            try:
                await self._market_callback(message)
            except Exception as exc:
                logger.error("Market callback unhandled error: {}", exc)
                
        asyncio.run_coroutine_threadsafe(_safe_call(), self._loop)

    def _dispatch_private(self, message: dict[str, Any]) -> None:
        if not self._loop or not self._private_callback:
            return
            
        async def _safe_call() -> None:
            try:
                await self._private_callback(message)
            except Exception as exc:
                logger.error("Private callback unhandled error: {}", exc)
                
        asyncio.run_coroutine_threadsafe(_safe_call(), self._loop)

    async def _run_public_ws(self) -> None:
        _, websocket_cls = self._load_pybit()
        retries = 0

        while not self._stop_ws.is_set():
            try:
                self._public_ws = websocket_cls(
                    testnet=self.testnet,
                    channel_type=self.category,
                    domain=self.domain,
                    tld=self.tld,
                )
                for symbol in self._public_ws_symbols:
                    self._public_ws.kline_stream(
                        interval=int(self.timeframe),
                        symbol=symbol,
                        callback=self._dispatch_market,
                    )
                retries = 0
                while not self._stop_ws.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retries += 1
                logger.warning("Public websocket disconnected (attempt {}): {}", retries, exc)
                if retries > 3:
                    await self._on_ws_failure(f"public_ws_failure:{exc}")
                    return
                await asyncio.sleep(2**retries)
            finally:
                if self._public_ws is not None:
                    await asyncio.to_thread(self._public_ws.exit)
                    self._public_ws = None

    async def _run_private_ws(self) -> None:
        _, websocket_cls = self._load_pybit()
        retries = 0

        while not self._stop_ws.is_set():
            try:
                self._private_ws = websocket_cls(
                    testnet=self.testnet,
                    channel_type="private",
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    domain=self.domain,
                    tld=self.tld,
                )
                self._private_ws.order_stream(callback=self._dispatch_private)
                self._private_ws.wallet_stream(callback=self._dispatch_private)
                retries = 0
                while not self._stop_ws.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retries += 1
                logger.warning("Private websocket disconnected (attempt {}): {}", retries, exc)
                if retries > 3:
                    await self._on_ws_failure(f"private_ws_failure:{exc}")
                    return
                await asyncio.sleep(2**retries)
            finally:
                if self._private_ws is not None:
                    await asyncio.to_thread(self._private_ws.exit)
                    self._private_ws = None

    async def _on_ws_failure(self, reason: str) -> None:
        logger.error("WebSocket recovery failed: {}", reason)
        # Cancel all orders for EVERY tracked symbol (A9 fix)
        for sym in self._public_ws_symbols:
            try:
                await self.cancel_all_orders(symbol=sym)
            except Exception as exc:
                logger.error("Emergency cancel-all failed for {}: {}", sym, exc)
        if self._failure_callback:
            await self._failure_callback(reason)
