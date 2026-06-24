"""Order lifecycle for the neutral futures grid.

Much simpler than the spot ``OrderManager`` it replaces: the exchange is the
source of truth for the position (no inventory reconstruction, no avg_cost
bookkeeping, no hedge/dust logic — the very things that desynced and caused
thousands of balance errors in the spot bot). This manager only:

  * places the grid ladder,
  * on each fill, places the partner order one spacing step away (the flip),
  * reconciles tracked orders with the exchange,
  * exposes the authoritative position and a cancel-all for the stop-loss.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from loguru import logger

from core.grid import GridPlan, partner_order
from data.models import FuturesPosition, TradeRecord


@dataclass(slots=True)
class ManagedOrder:
    """In-memory view of a resting grid order."""

    order_id: str
    link_id: str
    side: str  # "Buy" | "Sell"
    price: float
    qty: float
    status: str = "pending"  # pending | filled | cancelled | closed
    is_partner: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class FuturesPositionManager:
    """Places and maintains a neutral grid on one linear-perpetual symbol."""

    def __init__(self, *, exchange, symbol: str, tick_size, qty_step, min_qty) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.tick_size = tick_size
        self.qty_step = qty_step
        self.min_qty = min_qty
        self._orders: dict[str, ManagedOrder] = {}
        self._lock = asyncio.Lock()
        self._processed_fills: set[str] = set()
        self._spacing_pct: float = 0.0
        self._qty: float = 0.0

    # ── State ─────────────────────────────────────────────────────────

    @property
    def open_orders(self) -> list[ManagedOrder]:
        return [o for o in self._orders.values() if o.status == "pending"]

    def has_open_orders(self) -> bool:
        return any(o.status == "pending" for o in self._orders.values())

    async def get_position(self) -> FuturesPosition:
        """Authoritative position straight from the exchange."""
        raw = await self.exchange.get_position(symbol=self.symbol)
        return FuturesPosition(
            symbol=self.symbol,
            side=raw["side"],
            size=raw["size"],
            entry_price=raw["entry_price"],
            mark_price=raw["mark_price"],
            liq_price=raw["liq_price"],
            leverage=raw["leverage"],
            unrealized_pnl=raw["unrealized_pnl"],
            position_value=raw["position_value"],
            margin=raw["margin"],
            updated_at=datetime.now(UTC),
        )

    # ── Grid placement ────────────────────────────────────────────────

    async def place_grid(self, plan: GridPlan) -> int:
        """Place every rung of the grid plan as a PostOnly limit order."""
        self._spacing_pct = plan.spacing_pct
        self._qty = plan.qty_per_level
        ts = int(datetime.now(UTC).timestamp() * 1000)

        specs: list[tuple] = []
        tasks = []
        for lv in plan.levels:
            link = f"grid-{lv.level_id}-{ts}"[:36]
            specs.append((lv, link))
            tasks.append(
                self.exchange.place_limit_order(
                    symbol=self.symbol, side=lv.side, qty=lv.qty, price=lv.price,
                    orderLinkId=link,
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)

        placed = 0
        async with self._lock:
            for (lv, link), res in zip(specs, results):
                if isinstance(res, Exception):
                    logger.warning(
                        "{}: grid {} @ {:.4f} failed: {}", self.symbol, lv.side, lv.price, res,
                    )
                    continue
                oid = str(res)
                self._orders[oid] = ManagedOrder(oid, link, lv.side, lv.price, lv.qty)
                placed += 1
        logger.info(
            "{}: placed {}/{} grid orders (spacing {:.3f}%, qty {})",
            self.symbol, placed, len(plan.levels), plan.spacing_pct * 100, plan.qty_per_level,
        )
        return placed

    # ── Fill handling: the flip ───────────────────────────────────────

    async def handle_fill(self, fill_event: dict) -> TradeRecord | None:
        """Process a private order-stream fill; place the partner on the flip side."""
        order_id = str(fill_event.get("orderId") or "")
        if not order_id:
            return None
        status = str(fill_event.get("orderStatus") or "")
        if status not in ("Filled", "PartiallyFilled"):
            return None
        exec_id = str(fill_event.get("execId") or f"{order_id}:{fill_event.get('cumExecQty')}")

        partner_spec = None
        async with self._lock:
            if exec_id in self._processed_fills:
                return None
            self._processed_fills.add(exec_id)
            if len(self._processed_fills) > 10_000:
                self._processed_fills.clear()

            managed = self._orders.get(order_id)
            if managed is None:
                return None  # not one of ours

            cum = float(fill_event.get("cumExecQty") or 0.0)
            is_full = status == "Filled" or cum >= managed.qty * 0.999
            if is_full:
                managed.status = "filled"
                fill_price = float(
                    fill_event.get("avgPrice") or fill_event.get("price") or managed.price
                )
                partner_spec = partner_order(
                    filled_side=managed.side, filled_price=fill_price,
                    spacing_pct=self._spacing_pct, qty=managed.qty, tick_size=self.tick_size,
                )

        # Place the partner OUTSIDE the lock (network I/O).
        if partner_spec is not None:
            try:
                ts = int(datetime.now(UTC).timestamp() * 1000)
                link = f"grid-tp-{ts}"[:36]
                pid = await self.exchange.place_limit_order(
                    symbol=self.symbol, side=partner_spec.side, qty=partner_spec.qty,
                    price=partner_spec.price, orderLinkId=link,
                )
                async with self._lock:
                    self._orders[str(pid)] = ManagedOrder(
                        str(pid), link, partner_spec.side, partner_spec.price,
                        partner_spec.qty, is_partner=True,
                    )
                logger.info(
                    "{}: filled {} @ {:.4f} -> partner {} @ {:.4f}",
                    self.symbol, managed.side, managed.price, partner_spec.side, partner_spec.price,
                )
            except Exception as exc:
                logger.error("{}: partner placement failed: {}", self.symbol, exc)

        side = str(fill_event.get("side") or "?")
        price = float(fill_event.get("avgPrice") or fill_event.get("price") or 0.0)
        qty = float(fill_event.get("execQty") or fill_event.get("cumExecQty") or 0.0)
        fee = float(fill_event.get("execFee") or 0.0)
        if qty <= 0:
            return None
        # Realized PnL is reconciled separately from Bybit's closed-pnl endpoint
        # (accounting phase); MTM equity is the primary truth.
        return TradeRecord(
            timestamp=datetime.now(UTC), side=side, price=price, qty=qty,
            fee=fee, pnl=0.0, status="filled", symbol=self.symbol,
            order_type="Limit", exchange_order_id=order_id, metadata=fill_event,
        )

    # ── Reconciliation & teardown ─────────────────────────────────────

    async def sync_orders(self) -> None:
        """Safety net: mark tracked orders that vanished from the exchange.

        WS fills are the primary path for the flip; this only cleans state.
        NOTE: a fill missed by the WS will not spawn its partner here — the
        periodic grid rebuild on recenter heals such gaps. Hardening (order-
        history reconciliation) is a follow-up.
        """
        try:
            exch_orders = await self.exchange.get_open_orders(symbol=self.symbol)
        except Exception as exc:
            logger.error("{}: sync get_open_orders failed: {}", self.symbol, exc)
            return
        open_ids = {str(o.get("orderId")) for o in exch_orders}
        async with self._lock:
            for oid, o in self._orders.items():
                if o.status == "pending" and oid not in open_ids:
                    o.status = "closed"

    async def cancel_all(self) -> None:
        """Cancel every resting order for this symbol (used by the stop-loss)."""
        try:
            await self.exchange.cancel_all_orders(symbol=self.symbol)
        except Exception as exc:
            logger.error("{}: cancel_all failed: {}", self.symbol, exc)
        async with self._lock:
            for o in self._orders.values():
                if o.status == "pending":
                    o.status = "cancelled"

    def reset(self) -> None:
        """Forget all tracked orders (after a full cancel + flatten)."""
        self._orders.clear()
