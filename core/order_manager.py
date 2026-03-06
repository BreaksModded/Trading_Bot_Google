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

    async def get_order_history(self, *, symbol: str, order_id: str) -> dict | None: ...

    async def place_market_order(self, *, symbol: str, side: str, qty: float) -> str: ...

    async def get_orderbook(self, *, symbol: str, limit: int = 5) -> dict: ...


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
        self._position_untracked: bool = False
        # R8: failed inverse orders queued for retry
        self._pending_inverse_retries: list[dict] = []
        
        # BUG 4: Buffer for unmatched WebSocket fills
        self._unmatched_ws_fills: dict[str, dict] = {}
        self._unmatched_ws_timestamps: dict[str, float] = {}

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

    def get_open_buy_count(self) -> int:
        """Count open entry buy orders (excludes inverse/exit sells)."""
        return len([
            o for o in self._orders.values()
            if o.status == OrderStatus.PENDING
            and o.side == OrderSide.BUY
            and not o.level_id.startswith("inverse-")
            and not o.level_id.startswith("inv-")
        ])

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
            queued_levels: list[GridLevel] = []

            existing_level_ids = {
                self._root_level_id(o.level_id) for o in self.open_orders
            }

            for level in levels:
                base_id = self._root_level_id(str(level.level_id))
                if base_id in existing_level_ids:
                    continue

                # Spot guard: skip SELL entry orders when there's no inventory to sell.
                # SELL levels above market price are generated by the strategy for bearish/
                # neutral regimes, but in spot trading you can't short — you can only sell
                # what you own. Inverse SELL orders (after a BUY fill) are handled separately
                # by handle_fill / sync_with_exchange and are not affected by this check.
                if str(level.side).upper() == "SELL":
                    if self._position_qty < level.qty * 0.9:
                        logger.debug(
                            "{}: Skipping SELL entry level at {:.4f} — insufficient inventory "
                            "(position={:.6f} < required={:.6f})",
                            self.symbol, level.price, self._position_qty, level.qty,
                        )
                        continue

                timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
                link_id = f"entry-{base_id}-{timestamp_ms}"[:36]
                level_link_ids.append(link_id)
                queued_levels.append(level)

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
                level = queued_levels[i]
                link_id = level_link_ids[i]
                
                if isinstance(result, Exception):
                    logger.opt(exception=result).error("Grid level {} placement failed: {}", i, result)
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
            
            early_fills_to_process = []
            for m_order in managed:
                if m_order.order_id in self._unmatched_ws_fills:
                    buffered_fill = self._unmatched_ws_fills.pop(m_order.order_id)
                    self._unmatched_ws_timestamps.pop(m_order.order_id, None)
                    early_fills_to_process.append(buffered_fill)
                    
        # Bug 4: Execute handler outside of lock context to prevent deadlock
        for buffered_fill in early_fills_to_process:
            order_id = buffered_fill.get("orderId") or buffered_fill.get("order_id", "")
            logger.info("{}: Processing buffered early fill for {}", self.symbol, order_id)
            await self.handle_fill(buffered_fill)

        return managed

    async def cancel_all(self) -> None:
        """Cancel all currently tracked exchange orders."""
        async with self._lock:
            await self.exchange.cancel_all_orders(symbol=self.symbol)
            for order in self._orders.values():
                order.status = OrderStatus.CANCELED

    async def cancel_entry_orders_only(self, abort_on_error: bool = False) -> int:
        """
        Cancel ONLY orders that were placed as grid entry (buy) orders,
        NOT inverse/exit (sell) orders created after a fill.
        This ensures we do not leave existing inventory unhedged.

        When abort_on_error=True (Phase H refresh), non-retriable cancel
        failures will raise to let the caller abort the entire refresh.
        Orders that filled during the cancel window are handled gracefully.
        """
        cancelled_count = 0
        async with self._lock:
            # Entry orders do not have the 'inverse-' or 'inv-' prefix in their level_id
            entry_orders = [
                o for o in self._orders.values()
                if o.status == OrderStatus.PENDING
                and not o.level_id.startswith("inverse-")
                and not o.level_id.startswith("inv-")
            ]
            for order in entry_orders:
                try:
                    await self.exchange.cancel_order(symbol=self.symbol, order_id=order.order_id)
                    order.status = OrderStatus.CANCELED
                    cancelled_count += 1
                except Exception as exc:
                    exc_str = str(exc).lower()
                    # Phase H: Handle race condition — order filled during cancel
                    if "filled" in exc_str or "not found" in exc_str or "not exist" in exc_str:
                        order.status = OrderStatus.FILLED
                        self._confirmed_fill_ids.add(order.order_id)
                        logger.info(
                            "{}: Order {} filled/gone during refresh cancel (race condition handled)",
                            self.symbol, order.order_id,
                        )
                    elif abort_on_error:
                        logger.error("{}: Cancel failed during refresh, aborting: {}", self.symbol, exc)
                        raise
                    else:
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
            
            # Bug 1 Fix: Accumulate missing fills to process outside the lock
            missing_fills = []
            
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
                                missing_fills.append(history)
                                continue
                            elif status in ("CANCELED", "CANCELLED", "DEACTIVATED", "REJECTED"):
                                order.status = OrderStatus.CANCELED
                                continue
                        
                        # If history fetch fails or status is ambiguous, log and defer to next cycle
                        # to prevent falsely abandoning a filled position
                        logger.warning("{}: Ambiguous order status for {} - keeping PENDING", self.symbol, order_id)

            # R8: retry any pending inverse orders
            MAX_INVERSE_RETRIES = 10
            retries = list(self._pending_inverse_retries)
            self._pending_inverse_retries.clear()
            for retry in retries:
                retry_count = retry.get("retry_count", 0)

                # Abandon after too many failures to avoid infinite loops
                if retry_count >= MAX_INVERSE_RETRIES:
                    logger.error(
                        "{}: inverse retry ABANDONED after {} attempts "
                        "(side={} qty={} price={}). Manual check required.",
                        self.symbol, retry_count,
                        retry["side"], retry["qty"], retry["price"],
                    )
                    continue

                # For SELL retries: cap qty to actual position to avoid fee-rounding 170131
                # and adjust price to current market ask if market has moved above target.
                effective_qty = retry["qty"]
                effective_price = retry["price"]
                if retry["side"].upper() == "SELL":
                    position = getattr(self, "_position_qty", 0.0) or 0.0
                    if position < retry["qty"] * 0.95:
                        # Position too small — asset likely sold or lost, skip entirely
                        logger.warning(
                            "{}: inverse SELL retry skipped — position {:.6f} < required {:.6f}. "
                            "Asset may have been sold or locked by a stop order.",
                            self.symbol, position, retry["qty"],
                        )
                        continue
                    if position < retry["qty"]:
                        # Small fee-rounding gap: cap to available position
                        logger.info(
                            "{}: inverse SELL retry qty adjusted {:.6f} → {:.6f} (fee rounding).",
                            self.symbol, retry["qty"], position,
                        )
                        effective_qty = position

                    # Adjust sell price to current ask if market has moved above original target.
                    # Selling at a higher price = more profit than the original grid target.
                    try:
                        ob = await self.exchange.get_orderbook(symbol=self.symbol, limit=1)
                        best_ask = ob.get("best_ask", 0.0)
                        if best_ask > 0 and best_ask > retry["price"]:
                            logger.info(
                                "{}: inverse SELL retry price adjusted {:.4f} → {:.4f} "
                                "(market ask above original target — higher profit).",
                                self.symbol, retry["price"], best_ask,
                            )
                            effective_price = best_ask
                    except Exception as ob_exc:
                        logger.warning(
                            "{}: orderbook fetch failed during SELL retry, using original price: {}",
                            self.symbol, ob_exc,
                        )

                try:
                    root_id = self._root_level_id(retry["level_id"])
                    timestamp_ms = str(int(datetime.now(UTC).timestamp() * 1000))
                    # Prevent truncation of the timestamp by shortening the root_id
                    short_root = root_id[-18:] if len(root_id) > 18 else root_id
                    fresh_link_id = f"inv-{short_root}-{timestamp_ms}"

                    inv_id = await self.exchange.place_limit_order(
                        symbol=self.symbol,
                        side=retry["side"],
                        qty=effective_qty,
                        price=effective_price,
                        orderLinkId=fresh_link_id,
                    )
                    self._orders[inv_id] = ManagedOrder(
                        order_id=inv_id,
                        level_id=fresh_link_id,
                        symbol=self.symbol,
                        side=retry["side"],
                        price=effective_price,
                        qty=effective_qty,
                    )
                except Exception as exc:
                    logger.error("{}: inverse retry failed (attempt {}/{}): {}", self.symbol, retry_count + 1, MAX_INVERSE_RETRIES, exc)
                    retry["retry_count"] = retry_count + 1
                    self._pending_inverse_retries.append(retry)

        # Bug 1 Fix: Execute handlers outside of lock context
        for history in missing_fills:
            await self.handle_fill(history)

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

                # Bug A Fix: populate ManagedOrder from raw exchange data
                self._orders[order_id] = ManagedOrder(
                    order_id=order_id,
                    level_id=str(
                        raw.get("orderLinkId") or
                        raw.get("clientOrderId") or
                        order_id
                    ),
                    symbol=str(raw.get("symbol", self.symbol)),
                    side=str(raw.get("side", "Buy")).title(),
                    price=float(raw.get("price") or 0.0),
                    qty=float(raw.get("qty") or raw.get("leavesQty") or 0.0),
                )

            # Bug B Fix: collect buffered fills INSIDE lock, process OUTSIDE
            # (same pattern as sync_with_exchange and place_grid_orders)
            early_fills = []
            for order_id in self._orders:
                if order_id in self._unmatched_ws_fills:
                    early_fills.append(self._unmatched_ws_fills.pop(order_id))
                    self._unmatched_ws_timestamps.pop(order_id, None)

        # Bug B Fix: handle_fill acquires self._lock — must be called outside
        for fill in early_fills:
            await self.handle_fill(fill)

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
                self._unmatched_ws_fills[order_id] = fill_event
                now_ts = datetime.now(UTC).timestamp()
                self._unmatched_ws_timestamps[order_id] = now_ts
                
                # Clean up any buffer entries older than 60s
                stale_keys = [k for k, t in self._unmatched_ws_timestamps.items() if now_ts - t > 60.0]
                for k in stale_keys:
                    self._unmatched_ws_fills.pop(k, None)
                    self._unmatched_ws_timestamps.pop(k, None)
                    
                logger.debug("{}: Fill buffered (order not yet registered): {}", self.symbol, order_id)
                return None
            
            # Bug 4 Fix: If order is found, ensure we clear the buffer for it
            self._unmatched_ws_fills.pop(order_id, None)
            self._unmatched_ws_timestamps.pop(order_id, None)
            
            managed.status = OrderStatus.FILLED
            self._confirmed_fill_ids.add(order_id)
            if len(self._confirmed_fill_ids) > 10_000:
                self._confirmed_fill_ids.clear()

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

            # BUG 2 FIX: Calculate net sellable qty if spot BUY fee is deducted from base asset
            fee_currency = str(fill_event.get("feeCurrency") or fill_event.get("feeAsset") or "").upper()
            base_asset = self.symbol.replace("USDC", "").replace("USDT", "").upper()

            sellable_qty = qty
            if side == OrderSide.BUY:
                if fee_currency == base_asset or fee_currency == "":
                    # BUG FIX: If we used the fallback calculation (price * qty * fee_pct),
                    # it was in quote currency. If we are deducting from base asset, 
                    # the amount must be in base units.
                    if not fill_event.get("execFee"):
                        fee = qty * self.maker_fee_pct
                    
                    sellable_qty = max(0.0, qty - fee)
                    logger.debug("{}: Base fee deducted. execQty={}, fee={}, sellable_qty={}", self.symbol, qty, fee, sellable_qty)

            # R2: capture cost basis BEFORE position update (fix: avg_cost was being
            # reset to 0 before PnL calculation, causing all SELLs to show pnl=-fee)
            cost_basis = self._avg_cost

            # R2: update avg_cost and position tracking
            if side == OrderSide.BUY:
                if not self._position_untracked:
                    total_cost = self._avg_cost * self._position_qty + price * sellable_qty
                    self._position_qty += sellable_qty
                    self._avg_cost = total_cost / self._position_qty if self._position_qty > 0 else 0.0
                else:
                    self._position_qty += sellable_qty
            elif side == OrderSide.SELL:
                self._position_qty = max(0.0, self._position_qty - qty)
                if self._position_qty == 0:
                    self._avg_cost = 0.0
                    self._position_untracked = False

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
            inverse_qty = sellable_qty if side == OrderSide.BUY else qty
            try:
                inverse_order_id = await self.exchange.place_limit_order(
                    symbol=self.symbol,
                    side=str(inverse_side),
                    qty=inverse_qty,
                    price=inverse_price,
                    orderLinkId=inv_link_id,
                )
                self._orders[inverse_order_id] = ManagedOrder(
                    order_id=inverse_order_id,
                    level_id=inv_link_id,
                    symbol=self.symbol,
                    side=str(inverse_side),
                    price=inverse_price,
                    qty=inverse_qty,
                )
            except Exception as exc:
                logger.error("{}: inverse order failed: {}", self.symbol, exc)
                self._pending_inverse_retries.append({
                    "side": str(inverse_side),
                    "qty": inverse_qty,
                    "price": inverse_price,
                    "level_id": inv_link_id,
                })

        # R2: realized PnL = (sell_price - avg_cost) * qty - fee
        pnl = 0.0
        if side == OrderSide.SELL:
            if self._position_untracked:
                pnl = 0.0
                logger.info("{}: [PnL] Untracked — cost basis unknown. Sale volume {:.4f}", self.symbol, qty)
            elif cost_basis > 0:
                pnl = (price - cost_basis) * qty - fee
                self._check_fee_efficiency(pnl + fee, fee)
            else:
                pnl = -fee  # no cost basis known, conservative
                self._check_fee_efficiency(0.0, fee)
            
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

    def _check_fee_efficiency(self, gross_pnl: float, fee_round_trip: float) -> None:
        """Alert if gross PnL is too close to fees (less than 3x)."""
        if fee_round_trip <= 0:
            return
        
        # We need round_trip fees (approx 2x the sell side fee)
        estimated_round_trip = fee_round_trip * 2.0
        if estimated_round_trip <= 0:
            return
            
        ratio = gross_pnl / estimated_round_trip
        min_efficiency_ratio = 3.0
        
        if ratio > 0 and ratio < min_efficiency_ratio:
            logger.warning(
                "{}: low fee efficiency — gross PnL {:.4f} is only {:.1f}× the round-trip fee {:.4f}. "
                "Consider increasing spacing.",
                self.symbol, gross_pnl, ratio, estimated_round_trip,
            )

    async def check_stale_inverse_orders(
        self, exit_hours: float, exit_above_cost_pct: float
    ) -> bool:
        """
        Identify old inverse SELL orders and recolocate them with a small
        discount over cost basis to guarantee an exit from trapped inventory.
        Returns True if any exit action was executed.
        """
        now = datetime.now(UTC)
        executed_any = False
        
        # Snapshot the orders to avoid dict size change during iteration
        orders_to_check = [
            order for order in self._orders.values()
            if order.status == OrderStatus.PENDING 
            and order.side == OrderSide.SELL
            and (order.level_id.startswith("inverse-") or order.level_id.startswith("inv-")
                 or order.level_id.startswith("hedge-"))
        ]
        
        if not orders_to_check:
            return False
            
        for order in orders_to_check:
            age_hours = (now - order.created_at).total_seconds() / 3600.0
            if age_hours < exit_hours:
                continue
                
            # If cost basis is near 0, we can't safely calculate the exit price
            if self._avg_cost <= 1e-6:
                continue
                
            exit_price = self._avg_cost * (1 + exit_above_cost_pct)
            # Make sure we're actually reducing the price
            if exit_price >= order.price:
                continue
                
            logger.warning(
                "{}: inventory exit triggered for order {} — "
                "age {:.1f}h > {}h limit. Original sell: {:.4f}, "
                "Exit price: {:.4f} ({:.1%} above cost basis)",
                self.symbol, order.order_id, age_hours, exit_hours, 
                order.price, exit_price, exit_above_cost_pct
            )
            
            # Use lock to safely cancel and place
            async with self._lock:
                try:
                    await self.exchange.cancel_order(symbol=self.symbol, order_id=order.order_id)
                    order.status = OrderStatus.CANCELED
                    
                    timestamp_ms = str(int(datetime.now(UTC).timestamp() * 1000))
                    root_id = self._root_level_id(order.level_id)
                    short_root = root_id[-18:] if len(root_id) > 18 else root_id
                    exit_link_id = f"exit-{short_root}-{timestamp_ms}"
                    
                    new_order_id = await self.exchange.place_limit_order(
                        symbol=self.symbol,
                        side=str(OrderSide.SELL),
                        qty=order.qty,
                        price=exit_price,
                        orderLinkId=exit_link_id,
                    )
                    self._orders[new_order_id] = ManagedOrder(
                        order_id=new_order_id,
                        level_id=exit_link_id,
                        symbol=self.symbol,
                        side=str(OrderSide.SELL),
                        price=exit_price,
                        qty=order.qty,
                    )
                    executed_any = True
                except Exception as exc:
                    logger.error("{}: Failed to execute inventory exit for {}: {}", self.symbol, order.order_id, exc)

        return executed_any

    async def check_inventory_stop_loss(
        self, current_price: float, stop_pct: float
    ) -> bool:
        """Cancel all open orders and sell inventory at market if price drops below avg_cost × (1 - stop_pct).

        Returns True if the stop-loss was triggered and market sell executed, False otherwise.
        No-op if position_qty == 0 or avg_cost == 0.
        """
        if self._position_qty <= 0 or self._avg_cost <= 0:
            return False

        stop_threshold = self._avg_cost * (1 - stop_pct)
        if current_price >= stop_threshold:
            return False

        logger.warning(
            "{}: INVENTORY STOP-LOSS triggered — price {:.4f} < threshold {:.4f} "
            "({:.1%} below avg_cost {:.4f})",
            self.symbol, current_price, stop_threshold, stop_pct, self._avg_cost,
        )

        # Cancel all open orders first
        try:
            await self.exchange.cancel_all_orders(symbol=self.symbol)
        except Exception as exc:
            logger.error("{}: cancel_all_orders failed during stop-loss: {}", self.symbol, exc)

        # Sell all inventory at market
        try:
            await self.exchange.place_market_order(
                symbol=self.symbol,
                side=str(OrderSide.SELL),
                qty=self._position_qty,
            )
            logger.warning(
                "{}: market sell {:.6f} placed (stop-loss at {:.4f})",
                self.symbol, self._position_qty, current_price,
            )
        except Exception as exc:
            logger.error("{}: market sell failed during stop-loss: {}", self.symbol, exc)
            return False

        return True

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
            avg_cost=self._avg_cost,
            position_qty=self._position_qty,
        )
