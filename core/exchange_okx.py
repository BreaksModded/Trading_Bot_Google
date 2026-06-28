"""OKX exchange wrapper (X-Perps) — drop-in replacement for BybitExchangeClient.

OKX X-Perps are the EU/MiFID-compliant "perpetual": 5-year-expiry LINEAR futures
(settled in USD, collateral USDC) with a funding-rate mechanism. Two things differ
from Bybit linear perps and are handled HERE so the rest of the bot is untouched:

  1. Orders are denominated in CONTRACTS (1 contract = ``contractSize`` base units,
     e.g. 0.001 ETH). This client converts the bot's coin-denominated qty to/from
     contracts. The bot keeps working in ETH.
  2. Auth needs 3 credentials (key + secret + passphrase) and a demo (paper) mode.

Backed by ccxt.pro (async REST + WebSocket). Points that can only be confirmed against
the live API are marked ``# DEMO:`` — validate them on OKX demo before real money.
"""

from __future__ import annotations

import asyncio
import re
import sys
from decimal import Decimal, ROUND_DOWN
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from loguru import logger

from core.exchange import (
    ExchangeError, FailureCallback, MarketCallback, PrivateCallback, SpotSymbolRules,
)

# Bybit numeric interval -> ccxt timeframe string.
_TF = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m", "60": "1h",
    "120": "2h", "240": "4h", "360": "6h", "720": "12h", "D": "1d", "W": "1w", "M": "1M",
}
_QUOTES = ("USDT", "USDC", "USD", "EUR")


class OKXExchangeClient:
    """Async OKX client (ccxt.pro) exposing the same surface the futures bot uses."""

    def __init__(
        self, *, api_key: str, api_secret: str, passphrase: str, demo: bool = False,
        symbol: str, category: str = "linear", timeframe: str = "60",
        quote_coin: str = "USDC", hostname: str = "eea.okx.com", **_ignore: Any,
    ) -> None:
        import ccxt.pro as ccxtpro  # ccxt 4.x ships pro (REST async + WS) in the main pkg

        self.api_key = api_key
        self.api_secret = api_secret
        self.hostname = hostname             # EEA/EU users -> eea.okx.com
        self.symbol = symbol                 # bot symbol, e.g. "ETHUSDT"
        self.category = "linear"
        self.timeframe = timeframe
        self.quote_coin = quote_coin         # collateral stable (USDC for X-Perps)
        self._base = self._base_of(symbol)   # "ETH"

        self._ex = ccxtpro.okx({
            "apiKey": api_key, "secret": api_secret, "password": passphrase,
            "hostname": hostname,                    # REST -> https://{hostname} (EEA: eea.okx.com)
            "enableRateLimit": True, "timeout": 30000,
            # We only trade the linear X-Perp (a FUTURE), so restrict market loading to
            # futures: skips the SPOT/SWAP/OPTION endpoints (a concurrent burst OKX was
            # 503-ing) and makes load_markets faster.
            "options": {"defaultType": "future", "fetchMarkets": ["future"]},
        })
        if demo:
            self._ex.set_sandbox_mode(True)          # OKX demo/paper trading
            logger.warning("OKX client in DEMO (paper) mode")
        self._patch_fetch_markets()
        self._apply_ws_host(demo)

        self._ccxt_symbol: str | None = None
        self._market: dict[str, Any] | None = None
        self._contract_size: float = 0.001
        self._rules: SpotSymbolRules | None = None

        self._market_callback: MarketCallback | None = None
        self._private_callback: PrivateCallback | None = None
        self._failure_callback: FailureCallback | None = None
        self._stop_ws = asyncio.Event()
        self._private_task: asyncio.Task[None] | None = None

    # ── Symbol / contract helpers ─────────────────────────────────────

    @staticmethod
    def _base_of(symbol: str) -> str:
        if "/" in symbol:                       # ccxt unified form "ETH/USD:USD-..."
            return symbol.split("/", 1)[0].upper()
        s = symbol.upper().replace("-", "").replace(":", "")
        for q in _QUOTES:
            if s.endswith(q):
                return s[: -len(q)]
        return s

    def _patch_fetch_markets(self) -> None:
        """OKX demo returns a malformed instrument (id=None, symbol=None) that crashes
        ccxt's set_markets keysort. Filter such entries at the source (no-op in real)."""
        orig = self._ex.fetch_markets

        async def _clean(params={}):
            ms = await orig(params)
            return [m for m in ms if m.get("id") and m.get("symbol")]

        self._ex.fetch_markets = _clean

    def _apply_ws_host(self, demo: bool) -> None:
        """ccxt hardcodes the WS host (ws.okx.com, or wspap.okx.com in demo) and ignores
        `hostname`. EEA accounts need their own WS hosts or the WS login fails (60032)."""
        if "eea.okx.com" in (self.hostname or "").lower():
            ws = "wss://wseeapap.okx.com:8443/ws/v5" if demo else "wss://wseea.okx.com:8443/ws/v5"
            self._ex.urls["api"]["ws"] = ws
            logger.info("OKX WS host -> {}", ws)

    def _ensure_session(self) -> None:
        """ccxt.pro creates its aiohttp session lazily with the default async DNS
        resolver, which fails on some Windows setups ('Could not contact DNS servers').
        On Windows, force a ThreadedResolver (system getaddrinfo, like requests). No-op
        on Linux (the VPS), so production keeps ccxt's default session."""
        if sys.platform != "win32":
            return
        import aiohttp
        sess = getattr(self._ex, "session", None)
        if sess is not None and not getattr(sess, "closed", True):
            return
        self._ex.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            trust_env=True,
        )

    async def _ensure_market(self) -> None:
        """Resolve the current X-Perp for the base coin (linear, USD-settled, farthest
        expiry) and cache its contract size + precision rules. Auto-adapts if OKX rolls
        the series."""
        self._ensure_session()
        if self._ccxt_symbol is not None:
            return
        ms = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                ms = await self._ex.load_markets()
                break
            except Exception as exc:                  # OKX occasionally 503s on load
                last_exc = exc
                logger.warning("OKX load_markets retry {}/3: {}", attempt + 1, exc)
                await asyncio.sleep(1.5 * (attempt + 1))
        if ms is None:
            raise ExchangeError(f"OKX load_markets failed: {last_exc}")
        cands = [
            m for m in ms.values()
            if m.get("base") == self._base and m.get("settle") == "USD"
            and m.get("linear") and m.get("type") == "future"
        ]
        if not cands:
            raise ExchangeError(f"No linear X-Perp future found for {self._base} on OKX")
        cands.sort(key=lambda m: m.get("expiry") or 0, reverse=True)
        m = cands[0]
        self._ccxt_symbol = m["symbol"]
        self._market = m
        self._contract_size = float(m.get("contractSize") or 0.001)
        price_tick = m.get("precision", {}).get("price") or 0.01
        min_contracts = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 1
        self._rules = SpotSymbolRules(
            qty_step=Decimal(str(self._contract_size)),                      # 1 contract, in coin
            min_qty=Decimal(str(min_contracts)) * Decimal(str(self._contract_size)),
            tick_size=Decimal(str(price_tick)),
        )
        logger.info(
            "OKX market resolved: {} (instId {}) contractSize={} min_qty={} tick={}",
            self._ccxt_symbol, m.get("id"), self._contract_size,
            self._rules.min_qty, self._rules.tick_size,
        )

    def _to_contracts(self, qty_coin: float) -> int:
        """Coin qty (e.g. ETH) -> whole contracts (floored)."""
        step = Decimal(str(self._contract_size))
        return int((Decimal(str(qty_coin)) / step).to_integral_value(rounding=ROUND_DOWN))

    def _to_coin(self, contracts: float) -> float:
        return float(contracts) * self._contract_size

    @staticmethod
    def _clean_cl_ord_id(s: str | None) -> str | None:
        """OKX clOrdId: alphanumeric only, <=32 chars (the bot's ids have hyphens)."""
        if not s:
            return None
        return re.sub(r"[^a-zA-Z0-9]", "", s)[:32] or None

    # ── Market data ───────────────────────────────────────────────────

    async def get_klines(
        self, *, symbol: str | None = None, interval: str | None = None,
        limit: int = 500, category: str | None = None,
    ) -> pd.DataFrame:
        await self._ensure_market()
        tf = _TF.get(str(interval or self.timeframe), "1h")
        rows = await self._ex.fetch_ohlcv(self._ccxt_symbol, tf, limit=min(limit, 300))
        if not rows:
            raise ExchangeError("No kline data received from OKX.")
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        frame["turnover"] = frame["close"] * frame["volume"]   # approx; unused by futures bot
        frame = frame.astype({
            "timestamp": "int64", "open": "float64", "high": "float64",
            "low": "float64", "close": "float64", "volume": "float64", "turnover": "float64",
        })
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        return frame.sort_values("timestamp").reset_index(drop=True)

    async def get_last_price(self, symbol: str | None = None, category: str | None = None) -> float:
        await self._ensure_market()
        t = await self._ex.fetch_ticker(self._ccxt_symbol)
        px = t.get("last") or t.get("close")
        if not px:
            raise ExchangeError(f"No ticker price for {self._ccxt_symbol}")
        return float(px)

    async def get_funding_rate(self, *, symbol: str) -> float:
        """Best-effort: X-Perps are futures, so fetch_funding_rate (swap-only) is N/A.
        Funding is display-only for the dashboard; fall back to 0.0. # DEMO: confirm source."""
        await self._ensure_market()
        try:
            t = await self._ex.fetch_ticker(self._ccxt_symbol)
            fr = (t.get("info") or {}).get("fundingRate")
            return float(fr) if fr not in (None, "") else 0.0
        except Exception:
            return 0.0

    async def ping(self) -> float:
        self._ensure_session()
        started = datetime.now(UTC)
        await self._ex.fetch_time()
        return (datetime.now(UTC) - started).total_seconds() * 1000

    # ── Account ───────────────────────────────────────────────────────

    async def get_balance(self, coin: str = "USDC") -> float:
        self._ensure_session()
        bal = await self._ex.fetch_balance()
        return float((bal.get(coin) or {}).get("free") or 0.0)

    async def get_portfolio_equity(
        self, *, symbols: list[str], quote_coin: str = "USDC",
    ) -> tuple[float, float, dict[str, float]]:
        """(total_equity, free_collateral, balances). OKX reports unified account equity
        in USD via info.totalEq (uPnL already included) — the right number for the
        kill-switch. Free collateral sums the stables; X-Perps settle in USDC but the
        caller passes the Bybit-shaped "USDT", so we ignore it. # DEMO: confirm fields."""
        self._ensure_session()
        bal = await self._ex.fetch_balance()
        total_eq = 0.0
        try:
            data = ((bal.get("info") or {}).get("data") or [{}])[0]
            total_eq = float(data.get("totalEq") or 0.0)
        except Exception:
            total_eq = 0.0
        free = 0.0
        balances: dict[str, float] = {}
        for coin in ("USDC", "USDT", "USD"):
            coin_free = float((bal.get(coin) or {}).get("free") or 0.0)
            if coin_free:
                balances[coin] = coin_free
                free += coin_free
        equity = total_eq or free
        return equity, free, balances

    async def get_symbol_rules(self, symbol: str) -> SpotSymbolRules:
        await self._ensure_market()
        assert self._rules is not None
        return self._rules

    # ── Trading ───────────────────────────────────────────────────────

    async def set_leverage(self, *, symbol: str, leverage: int) -> None:
        await self._ensure_market()
        try:
            await self._ex.set_leverage(leverage, self._ccxt_symbol, params={"mgnMode": "isolated"})
            logger.info("OKX {}: leverage {}x (isolated)", self._ccxt_symbol, leverage)
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            logger.warning("OKX set_leverage best-effort failed: {}", exc)   # # DEMO: confirm

    async def get_position(self, *, symbol: str) -> dict[str, Any]:
        await self._ensure_market()
        flat = {
            "symbol": symbol, "side": "flat", "size": 0.0, "entry_price": 0.0,
            "mark_price": 0.0, "liq_price": 0.0, "leverage": 0.0,
            "unrealized_pnl": 0.0, "position_value": 0.0, "margin": 0.0,
        }
        try:
            pos = await self._ex.fetch_position(self._ccxt_symbol)
        except Exception as exc:
            logger.debug("OKX fetch_position: {}", exc)
            return flat
        if not pos:
            return flat
        contracts = float(pos.get("contracts") or 0.0)
        if contracts <= 0:
            return flat
        return {
            "symbol": symbol,
            "side": pos.get("side") or "flat",                       # 'long' | 'short'
            "size": contracts * self._contract_size,                 # back to coin (ETH)
            "entry_price": float(pos.get("entryPrice") or 0.0),
            "mark_price": float(pos.get("markPrice") or 0.0),
            "liq_price": float(pos.get("liquidationPrice") or 0.0),
            "leverage": float(pos.get("leverage") or 0.0),
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0.0),
            "position_value": float(pos.get("notional") or 0.0),
            "margin": float(pos.get("initialMargin") or pos.get("collateral") or 0.0),
        }

    async def place_market_linear(
        self, *, symbol: str, side: str, qty: float, reduce_only: bool = False,
    ) -> str:
        await self._ensure_market()
        contracts = self._to_contracts(qty)
        if contracts < 1:
            raise ExchangeError(f"qty {qty} below 1 contract ({self._contract_size}) for {symbol}")
        params = {"reduceOnly": reduce_only, "tdMode": "isolated"}
        o = await self._ex.create_order(self._ccxt_symbol, "market", side.lower(), contracts, None, params)
        oid = str(o.get("id") or "")
        if not oid:
            raise ExchangeError(f"OKX market order rejected: {o}")
        return oid

    async def close_position_market(self, *, symbol: str, side: str, qty: float) -> str:
        await self._ensure_market()
        contracts = self._to_contracts(qty)
        if contracts < 1:
            raise ExchangeError(f"close qty {qty} below 1 contract for {symbol}")
        o = await self._ex.create_order(
            self._ccxt_symbol, "market", side.lower(), contracts, None,
            {"reduceOnly": True, "tdMode": "isolated"},
        )
        return str(o.get("id") or "")

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: float, price: float,
        orderLinkId: str | None = None, reduce_only: bool = False,
    ) -> str:
        await self._ensure_market()
        contracts = self._to_contracts(qty)
        if contracts < 1:
            raise ExchangeError(f"grid qty {qty} below 1 contract for {symbol}")
        params: dict[str, Any] = {"postOnly": True, "tdMode": "isolated"}
        if reduce_only:
            params["reduceOnly"] = True
        cl = self._clean_cl_ord_id(orderLinkId)
        if cl:
            params["clOrdId"] = cl
        o = await self._ex.create_order(self._ccxt_symbol, "limit", side.lower(), contracts, price, params)
        oid = o.get("id")
        if not oid:
            raise ExchangeError(f"OKX limit order rejected: {o}")
        return str(oid)

    async def get_open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open orders in the Bybit shape sync_orders expects (it reads 'orderId')."""
        await self._ensure_market()
        try:
            orders = await self._ex.fetch_open_orders(self._ccxt_symbol)
        except Exception as exc:
            logger.warning("OKX fetch_open_orders: {}", exc)
            return []
        return [
            {
                "orderId": str(o.get("id") or ""),
                "orderLinkId": str(o.get("clientOrderId") or ""),
                "side": "Buy" if o.get("side") == "buy" else "Sell",
                "price": float(o.get("price") or 0.0),
                "qty": float(o.get("amount") or 0.0) * self._contract_size,
            }
            for o in (orders or [])
        ]

    async def cancel_all_orders(self, *, symbol: str | None = None) -> None:
        # ccxt okx has no cancelAllOrders -> fetch open orders and batch-cancel them.
        await self._ensure_market()
        try:
            orders = await self._ex.fetch_open_orders(self._ccxt_symbol)
            ids = [o["id"] for o in orders if o.get("id")]
            if not ids:
                return
            try:
                await self._ex.cancel_orders(ids, self._ccxt_symbol)          # batch
            except Exception:
                for oid in ids:                                               # fallback: 1x1
                    try:
                        await self._ex.cancel_order(oid, self._ccxt_symbol)
                    except Exception as exc:
                        logger.warning("OKX cancel_order {}: {}", oid, exc)
        except Exception as exc:
            logger.warning("OKX cancel_all_orders: {}", exc)

    async def set_position_stop_loss(self, *, symbol: str, stop_price: float) -> None:
        """Best-effort exchange-side stop (backstop). The bot's primary stop is the
        live-price check in decide_trend. # DEMO: confirm OKX algo-order params."""
        await self._ensure_market()
        pos = await self.get_position(symbol=symbol)
        if pos["side"] == "flat":
            return
        contracts = self._to_contracts(pos["size"])
        if contracts < 1:
            return
        close_side = "sell" if pos["side"] == "long" else "buy"
        try:
            await self._ex.create_order(
                self._ccxt_symbol, "market", close_side, contracts, None,
                {"reduceOnly": True, "tdMode": "isolated", "stopLossPrice": float(stop_price)},
            )
            logger.info("OKX {}: stop-loss backstop @ {}", self._ccxt_symbol, stop_price)
        except Exception as exc:
            logger.warning("OKX set_position_stop_loss best-effort failed: {}", exc)

    # ── WebSocket (private fills via ccxt.pro) ─────────────────────────

    def set_failure_callback(self, callback: FailureCallback) -> None:
        self._failure_callback = callback

    async def start_websockets(
        self, *, market_callback: MarketCallback,
        private_callback: PrivateCallback | None = None, symbols: list[str] | None = None,
    ) -> None:
        """The futures bot only needs the PRIVATE order stream (grid fills); its market
        callback is a no-op (signals are timer-driven). So we only run watch_orders."""
        self._market_callback = market_callback
        self._private_callback = private_callback
        self._stop_ws.clear()
        if self.api_key and private_callback is not None:
            self._private_task = asyncio.create_task(self._run_private_ws(), name="okx-private-ws")

    async def stop_websockets(self) -> None:
        self._stop_ws.set()
        if self._private_task:
            self._private_task.cancel()
            await asyncio.gather(self._private_task, return_exceptions=True)
        try:
            await self._ex.close()
        except Exception:
            pass

    def _translate_order(self, o: dict[str, Any]) -> dict[str, Any] | None:
        """Map a ccxt OKX order update to the Bybit-shaped fill event handle_fill expects.
        Quantities are converted from contracts back to coin (ETH)."""
        status = o.get("status")
        filled = float(o.get("filled") or 0.0)
        if status == "closed":
            order_status = "Filled"
        elif filled > 0:
            order_status = "PartiallyFilled"
        else:
            return None
        info = o.get("info") or {}
        filled_coin = filled * self._contract_size
        return {
            "orderId": str(o.get("id") or ""),
            "orderLinkId": str(o.get("clientOrderId") or ""),
            "orderStatus": order_status,
            "execId": str(info.get("tradeId") or f"{o.get('id')}:{filled}"),
            "cumExecQty": filled_coin,
            "execQty": filled_coin,
            "avgPrice": float(o.get("average") or o.get("price") or 0.0),
            "price": float(o.get("price") or 0.0),
            "side": "Buy" if o.get("side") == "buy" else "Sell",
            "execFee": float((o.get("fee") or {}).get("cost") or 0.0),
            "closedPnl": float(info.get("pnl") or 0.0),
            "category": "linear", "symbol": self.symbol,
        }

    async def _run_private_ws(self) -> None:
        await self._ensure_market()
        retries = 0
        while not self._stop_ws.is_set():
            try:
                orders = await self._ex.watch_orders(self._ccxt_symbol)
                retries = 0
                for o in (orders or []):
                    ev = self._translate_order(o)
                    if ev and self._private_callback:
                        await self._private_callback({"topic": "order", "data": [ev]})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retries += 1
                logger.warning("OKX private WS error (attempt {}): {}", retries, exc)
                if retries > 3:
                    await self._on_ws_failure(f"okx_private_ws:{exc}")
                    return
                await asyncio.sleep(2 ** retries)

    async def _on_ws_failure(self, reason: str) -> None:
        logger.error("OKX WebSocket recovery failed: {}", reason)
        try:
            await self.cancel_all_orders(symbol=self.symbol)
        except Exception as exc:
            logger.error("OKX emergency cancel-all failed: {}", exc)
        if self._failure_callback:
            await self._failure_callback(reason)
