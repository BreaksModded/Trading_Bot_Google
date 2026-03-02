"""Order lifecycle manager for grid strategy execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from data.models import GridLevel, GridState, OrderSide, OrderStatus, TradeRecord
from loguru import logger


class ExchangeGateway(Protocol):
    """Minimal exchange interface consumed by OrderManager."""

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: float, price: float, orderLinkId: str | None = None,
    ) -> str: ...

    async def cancel_order(self, *, symbol: str, order_id: str) -> None: ...

    async def cancel_all_orders(self, *, symbol: str) -> None: ...

    async def get_open_orders(self, *, symbol: str) -> list[dict]: ...


@dataclass(slots=True)
class ManagedOrder:
    """In-memory representation of exchange order state."""

    order_id: str
    level_id: str
    symbol: str
    side: str
    price: float
    qty: float
    status: str = OrderStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class OrderManager:
    """Creates, tracks, and rebalances grid orders."""

    def __init__(
        self, exchange: ExchangeGateway, symbol: str,
        maker_fee_pct: float = 0.0001, max_open_orders: int = 20,
        default_spacing_pct: float = 0.006,
    ) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.maker_fee_pct = maker_fee_pct
        self.max_open_orders = max_open_orders
        self.default_spacing_pct = default_spacing_pct
        self._orders: dict[str, ManagedOrder] = {}
        self._buy_spacing_pct: float = 0.0
        self._sell_spacing_pct: float = 0.0
        self._lock = asyncio.Lock()
        self._confirmed_fill_ids: set[str] = set()
        self._processed_fill_keys: set[str] = set()
        
        # SG1: Stale grid tracking
        self.grid_anchor_price: float | None = None
        self.grid_created_at: datetime | None = None
        
        # R2: position tracking for realized PnL
        self._avg_cost: float = 0.0
        self._position_qty: float = 0.0
        # R8: failed inverse orders queued for retry
        self._pending_inverse_retries: list[dict] = []

    def set_spacing(self, buy_spacing_pct: float, sell_spacing_pct: float = 0.0) -> None:
        """Restore spacing from persisted state after restart/recovery."""
        if buy_spacing_pct > 0:
            self._buy_spacing_pct = buy_spacing_pct
            self._sell_spacing_pct = sell_spacing_pct if sell_spacing_pct > 0 else buy_spacing_pct

    @property
    def open_orders(self) -> list[ManagedOrder]:
        return [
            order for order in self._orders.values()
            if order.status == OrderStatus.PENDING
        ]

    async def place_grid_orders(
        self, levels: list[GridLevel], buy_spacing_pct: float, sell_spacing_pct: float
    ) -> list[ManagedOrder]:
        """Place the current grid levels and track resulting exchange order IDs."""
        async with self._lock:
            # R7: enforce max open orders limit
            current_open = len(self.open_orders)
            allowed = max(0, self.max_open_orders - current_open)
            if allowed < len(levels):
                logger.warning(
                    "{}: max_open_orders limit ({}) reached, placing {}/{} levels",
                    self.symbol, self.max_open_orders, allowed, len(levels),
                )
                levels = levels[:allowed]
            if not levels:
                return []

            self._buy_spacing_pct = buy_spacing_pct
            self._sell_spacing_pct = sell_spacing_pct
            managed: list[ManagedOrder] = []
            level_tasks = []
            level_link_ids = []
            
            existing_level_ids = {
                self._root_level_id(o.level_id) for o in self.open_orders
            }
            
            for level in levels:
                base_id = self._root_level_id(str(level.level_id))
                if base_id in existing_level_ids:
                    continue
                
                timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
                link_id = f"entry-{base_id}-{timestamp_ms}"[:36]
                level_link_ids.append(link_id)

                level_tasks.append(
                    self.exchange.place_limit_order(
                        symbol=self.symbol,
                        side=str(level.side),
                        qty=level.qty,
                        price=level.price,
                        orderLinkId=link_id,
                    )
                )

            results = await asyncio.gather(*level_tasks, return_exceptions=True)
            failed_count = 0
            for i, result in enumerate(results):
                level = levels[i]
                link_id = level_link_ids[i]
                
                if isinstance(result, Exception):
                    logger.error(f"Grid level {i} placement failed: {result}", exc_info=result)
                    failed_count += 1
                    continue

                order_id = str(result)
                managed_order = ManagedOrder(
                    order_id=order_id,
                    level_id=link_id,
                    symbol=self.symbol,
                    side=str(level.side),
                    price=level.price,
                    qty=level.qty,
                )
                self._orders[order_id] = managed_order
                managed.append(managed_order)

            if failed_count > 0:
                logger.warning("{}: Partial grid placement: {} levels failed, {} succeeded", self.symbol, failed_count, len(managed))
            return managed

    async def cancel_all(self) -> None:
        """Cancel all currently tracked exchange orders."""
        async with self._lock:
            await self.exchange.cancel_all_orders(symbol=self.symbol)
            for order in self._orders.values():
                order.status = OrderStatus.CANCELED

    async def cancel_entry_orders_only(self) -> int:
        """
        Cancel ONLY orders that were placed as grid entry (buy) orders,
        NOT inverse/exit (sell) orders created after a fill.
        This ensures we do not leave existing inventory unhedged.
        """
        cancelled_count = 0
        async with self._lock:
            # Entry orders do not have the 'inverse-' prefix in their level_id
            entry_orders = [
                o for o in self._orders.values() 
                if o.status == OrderStatus.PENDING and not o.level_id.startswith("inverse-")
            ]
            for order in entry_orders:
                try:
                    await self.exchange.cancel_order(symbol=self.symbol, order_id=order.order_id)
                    order.status = OrderStatus.CANCELED
                    cancelled_count += 1
                except Exception as exc:
                    logger.error("{}: Failed to cancel entry order {}: {}", self.symbol, order.order_id, exc)
        return cancelled_count

    def has_unhedged_inventory(self) -> bool:
        """
        Return True if the bot holds base asset for this symbol with no 
        corresponding pending sell order. Prevents staleness cancellations
        from exposing inventory risk.
        """
        if self._position_qty <= 1e-6:
            return False
            
        pending_sell_qty = sum(
            o.qty for o in self.open_orders 
            if o.side == OrderSide.SELL
        )
        
        # If pending sells cover the inventory, it's hedged. Allowing small float diffs.
        return pending_sell_qty < (self._position_qty * 0.99)

    async def sync_with_exchange(self) -> None:
        """Synchronize tracked orders with exchange open order list."""
        async with self._lock:
            exchange_orders = await self.exchange.get_open_orders(symbol=self.symbol)
            open_ids = {
                str(item.get("orderId") or item.get("order_id"))
                for item in exchange_orders
            }
            for order_id, order in self._orders.items():
                if order_id not in open_ids and order.status == OrderStatus.PENDING:
                    if order_id in self._confirmed_fill_ids:
                        order.status = OrderStatus.FILLED
                    else:
                        # FIX #4: REST Fallback check for missing WS fills
                        try:
                            history = await self.exchange.get_order_history(symbol=self.symbol, order_id=order_id)
                        except Exception as e:
                            logger.warning("{}: Error reconciling order {}: {}", self.symbol, order_id, e)
                            history = None
                            
                        if history:
                            status = str(history.get("orderStatus", "")).upper()
                            if status == "FILLED":
                                logger.info("{}: [REST Fallback] Recovered missing fill for {}", self.symbol, order_id)
                                order.status = OrderStatus.FILLED
                                self._confirmed_fill_ids.add(order_id)
                                await self.handle_fill(history)
                                continue
                            elif status in ("CANCELED", "CANCELLED", "DEACTIVATED", "REJECTED"):
                                order.status = OrderStatus.CANCELED
                                continue
                        
                        # If history fetch fails or status is ambiguous, log and defer to next cycle
                        # to prevent falsely abandoning a filled position
                        logger.warning("{}: Ambiguous order status for {} - keeping PENDING", self.symbol, order_id)

            # R8: retry any pending inverse orders
            retries = list(self._pending_inverse_retries)
            self._pending_inverse_retries.clear()
            for retry in retries:
                try:
                    root_id = self._root_level_id(retry["level_id"])
                    timestamp_ms = str(int(datetime.now(UTC).timestamp() * 1000))
                    # Prevent truncation of the timestamp by shortening the root_id
                    short_root = root_id[-18:] if len(root_id) > 18 else root_id
                    fresh_link_id = f"inv-{short_root}-{timestamp_ms}"
                    
                    inv_id = await self.exchange.place_limit_order(
                        symbol=self.symbol,
                        side=retry["side"],
                        qty=retry["qty"],
                        price=retry["price"],
                        orderLinkId=fresh_link_id,
                    )
                    self._orders[inv_id] = ManagedOrder(
                        order_id=inv_id,
                        level_id=fresh_link_id,
                        symbol=self.symbol,
                        side=retry["side"],
                        price=retry["price"],
                        qty=retry["qty"],
                    )
                except Exception as exc:
                    logger.error("{}: inverse retry failed: {}", self.symbol, exc)
                    self._pending_inverse_retries.append(retry)

    async def recover_from_crash(self) -> list[dict]:
        """Load currently open exchange orders to avoid orphan-state on restart."""
        async with self._lock:
            open_orders = await self.exchange.get_open_orders(symbol=self.symbol)
            for raw in open_orders:
                order_id = str(raw.get("orderId") or raw.get("order_id"))
                if order_id in self._orders:
                    continue
                
                # Protect against absorbing global stop losses or market conditionals
                order_type = str(raw.get("orderType", "")).upper()
                stop_order_type = str(raw.get("stopOrderType", "")).upper()
                if order_type != "LIMIT" or (stop_order_type and stop_order_type != "UNKNOWN"):
                    continue

                self._orders[order_id] = ManagedOrder(
                    order_id=order_id,
                    level_id=str(raw.get("orderLinkId") or f"recovered-{order_id}"),
                    symbol=self.symbol,
                    side=str(raw.get("side")),
                    price=float(raw.get("price") or 0.0),
                    qty=float(raw.get("qty") or raw.get("orderQty") or 0.0),
                )
            return open_orders

    async def handle_fill(self, fill_event: dict) -> TradeRecord | None:
        """Process an exchange fill event and place inverse order for grid rebalancing."""
        order_id = str(fill_event.get("orderId") or fill_event.get("order_id", ""))
        if not order_id:
            return None

        fill_key = self._build_fill_key(order_id=order_id, fill_event=fill_event)

        async with self._lock:
            if fill_key in self._processed_fill_keys:
                return None
            self._processed_fill_keys.add(fill_key)
            if len(self._processed_fill_keys) > 10_000:
                self._processed_fill_keys.clear()

            managed = self._orders.get(order_id)
            if managed is None:
                return None
            managed.status = OrderStatus.FILLED
            self._confirmed_fill_ids.add(order_id)

            side = str(fill_event.get("side", managed.side))
            price = float(
                fill_event.get("avgPrice") or fill_event.get("price") or managed.price
            )
            qty = float(
                fill_event.get("execQty") or fill_event.get("qty") or managed.qty
            )
            # M8: zero-qty guard — reject degenerate fills
            if qty <= 0:
                return None
            fee = float(
                fill_event.get("execFee") or (price * qty * self.maker_fee_pct)
            )

            # R2: update avg_cost and position tracking
            if side == OrderSide.BUY:
                total_cost = self._avg_cost * self._position_qty + price * qty
                self._position_qty += qty
                self._avg_cost = total_cost / self._position_qty if self._position_qty > 0 else 0.0
            elif side == OrderSide.SELL:
                self._position_qty = max(0.0, self._position_qty - qty)
                if self._position_qty == 0:
                    self._avg_cost = 0.0

            # Place inverse order for grid rebalancing
            inverse_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            if inverse_side == OrderSide.SELL:
                spacing = self._sell_spacing_pct if self._sell_spacing_pct > 0 else self.default_spacing_pct
                inverse_price = price * (1 + spacing)
            else:
                spacing = self._buy_spacing_pct if self._buy_spacing_pct > 0 else self.default_spacing_pct
                inverse_price = price * (1 - spacing)
                
                timestamp_ms = str(int(datetime.now(UTC).timestamp() * 1000))
                root_id = self._root_level_id(managed.level_id)
                short_root = root_id[-18:] if len(root_id) > 18 else root_id
                inv_link_id = f"inv-{short_root}-{timestamp_ms}"

                # R8: wrap in try/except to prevent fill loss on inverse failure
                try:
                    inverse_order_id = await self.exchange.place_limit_order(
                        symbol=self.symbol,
                        side=str(inverse_side),
                        qty=qty,
                        price=inverse_price,
                        orderLinkId=inv_link_id,
                    )
                    self._orders[inverse_order_id] = ManagedOrder(
                        order_id=inverse_order_id,
                        level_id=inv_link_id,
                        symbol=self.symbol,
                        side=str(inverse_side),
                        price=inverse_price,
                        qty=qty,
                    )
                except Exception as exc:
                    logger.error("{}: inverse order failed: {}", self.symbol, exc)
                    self._pending_inverse_retries.append({
                        "side": str(inverse_side),
                        "qty": qty,
                        "price": inverse_price,
                        "level_id": inv_link_id,
                    })

        # R2: realized PnL = (sell_price - avg_cost) * qty - fee
        pnl = 0.0
        if side == OrderSide.SELL and self._avg_cost > 0:
            pnl = (price - self._avg_cost) * qty - fee
        elif side == OrderSide.SELL:
            pnl = -fee  # no cost basis known, conservative
        return TradeRecord(
            timestamp=datetime.now(UTC),
            side=side,
            price=price,
            qty=qty,
            fee=fee,
            pnl=pnl,
            status=OrderStatus.FILLED,
            symbol=self.symbol,
            exchange_order_id=order_id,
            metadata=fill_event,
        )

    @staticmethod
    def _build_fill_key(order_id: str, fill_event: dict) -> str:
        """Build a unique key for fill deduplication using execId or composite."""
        exec_id = fill_event.get("execId") or fill_event.get("exec_id")
        if exec_id:
            return str(exec_id)
        qty = fill_event.get("execQty") or fill_event.get("cumExecQty") or fill_event.get("qty") or "0"
        price = fill_event.get("avgPrice") or fill_event.get("price") or "0"
        return f"{order_id}:{qty}:{price}"

    @staticmethod
    def _root_level_id(level_id: str) -> str:
        """Strip prefixes and timestamp suffix to get the original grid level ID."""
        normalized = str(level_id or "")
        for prefix in ("inverse-", "inv-", "entry-"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
            
        parts = normalized.split("-")
        if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) >= 13:
            normalized = "-".join(parts[:-1])
            
        return normalized or "level"

    def to_grid_state(self, buy_spacing_pct: float, sell_spacing_pct: float, trend_bias: str) -> GridState:
        """Serialize current pending orders to persistence model."""
        levels = [
            GridLevel(
                level_id=order.level_id,
                price=order.price,
                side=order.side,
                qty=order.qty,
                status=order.status,
            )
            for order in self.open_orders
        ]
        return GridState(
            symbol=self.symbol,
            spacing_pct=buy_spacing_pct,
            trend_bias=trend_bias,
            levels=levels,
            last_sync_time=datetime.now(UTC),
            grid_created_at=self.grid_created_at,
            grid_anchor_price=self.grid_anchor_price,
            pending_retries=list(self._pending_inverse_retries),
            buy_spacing_pct=buy_spacing_pct,
            sell_spacing_pct=sell_spacing_pct,
        )
