"""
Paper trading execution engine.

Simulates order fills locally without touching the exchange.
This is Phase 1 — use this until you have high confidence in your strategy.

Fill simulation logic:
  - Market orders: fill immediately at best available price + slippage
  - Limit orders: fill only if the book has liquidity at or better than limit price
  - Partial fills: if available quantity < order quantity, fill what's available
  - If no liquidity: order stays OPEN (resting), re-checked on next snapshot

The paper engine needs the current order book to simulate fills realistically.
Call notify_snapshot() on each new MarketSnapshot so the engine can attempt
to fill any resting orders.

Slippage model:
  Configurable via paper_slippage_cents in config. Added to buy prices,
  subtracted from sell prices.

  TODO: You may want to replace this with a more sophisticated market
  impact model once you have real fill data to calibrate against.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator

from bot.core.interfaces import ExecutionEngine
from bot.core.models import Fill, MarketSnapshot, Order, OrderStatus, Side
from bot.logging.logger import get_logger

log = get_logger(__name__)


class PaperEngine(ExecutionEngine):
    """
    Simulated execution engine for paper trading.

    Usage in the runner:
        engine = PaperEngine(fill_delay=0.1, slippage_cents=1)
        await engine.start()
        # Pass snapshots in as they arrive
        engine.notify_snapshot(snapshot)
        # Consume fills
        async for fill in engine.stream_fills():
            ...
    """

    def __init__(
        self,
        fill_delay_seconds: float = 0.1,
        slippage_cents: int = 1,
    ) -> None:
        self._fill_delay = fill_delay_seconds
        self._slippage = slippage_cents
        # Keyed by order_id
        self._open_orders: dict[str, Order] = {}
        self._fill_queue: asyncio.Queue[Fill] = asyncio.Queue()
        # Latest snapshot per ticker, for resting order evaluation
        self._latest_snapshots: dict[str, MarketSnapshot] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("paper_engine_started", slippage_cents=self._slippage)

    async def stop(self) -> None:
        self._running = False
        remaining = len(self._open_orders)
        if remaining:
            log.warning("paper_engine_stopped_with_open_orders", count=remaining)

    # ------------------------------------------------------------------
    # ExecutionEngine interface
    # ------------------------------------------------------------------

    async def submit_order(self, order: Order) -> Order:
        """
        Accept an order. Attempt an immediate fill; if not possible, leave it resting.
        Returns the order in its new state (FILLED, PARTIALLY_FILLED, or OPEN).
        """
        log.info(
            "paper_order_submitted",
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side.value,
            contract_side=order.contract_side.value,
            quantity=order.quantity,
            limit_price=order.limit_price,
        )

        # Attempt immediate fill against the latest snapshot
        snapshot = self._latest_snapshots.get(order.ticker)
        if snapshot:
            order = await self._attempt_fill(order, snapshot)

        # If not fully filled, store as resting order
        if not order.is_terminal:
            order.status = OrderStatus.OPEN
            self._open_orders[order.order_id] = order

        return order

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel a resting order."""
        order = self._open_orders.pop(order_id, None)
        if order is None:
            raise KeyError(f"Order {order_id} not found in paper engine")
        order.status = OrderStatus.CANCELLED
        order.updated_at = datetime.utcnow()
        log.info("paper_order_cancelled", order_id=order_id, ticker=order.ticker)
        return order

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield fills as they are generated."""
        while self._running:
            try:
                fill = await asyncio.wait_for(self._fill_queue.get(), timeout=0.5)
                yield fill
            except asyncio.TimeoutError:
                continue

    async def get_open_orders(self) -> list[Order]:
        return list(self._open_orders.values())

    # ------------------------------------------------------------------
    # Snapshot notification (called by the runner on every new snapshot)
    # ------------------------------------------------------------------

    def notify_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        Inform the paper engine of the latest market state.
        Re-evaluates any resting orders for this ticker.
        """
        self._latest_snapshots[snapshot.ticker] = snapshot
        # Fire-and-forget: schedule fill attempts for resting orders
        asyncio.create_task(self._evaluate_resting_orders(snapshot))

    async def _evaluate_resting_orders(self, snapshot: MarketSnapshot) -> None:
        """Attempt to fill all resting orders for a given ticker."""
        resting = [
            o for o in list(self._open_orders.values())
            if o.ticker == snapshot.ticker
        ]
        for order in resting:
            updated = await self._attempt_fill(order, snapshot)
            if updated.is_terminal:
                self._open_orders.pop(order.order_id, None)
            else:
                self._open_orders[order.order_id] = updated

    # ------------------------------------------------------------------
    # Fill simulation
    # ------------------------------------------------------------------

    async def _attempt_fill(self, order: Order, snapshot: MarketSnapshot) -> Order:
        """
        Try to fill order against the current order book snapshot.
        Returns the order with updated status and fill fields.
        """
        fill_price, available_qty = self._get_fill_price_and_qty(order, snapshot)

        if fill_price is None or available_qty == 0:
            # No liquidity — order rests
            return order

        fill_qty = min(order.remaining_quantity, available_qty)
        fill_price_with_slippage = self._apply_slippage(fill_price, order.side)

        # Simulate fill delay
        if self._fill_delay > 0:
            await asyncio.sleep(self._fill_delay)

        fill = Fill(
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side,
            contract_side=order.contract_side,
            quantity=fill_qty,
            price=fill_price_with_slippage,
            fee=self._compute_fee(fill_qty, fill_price_with_slippage),
            filled_at=datetime.utcnow(),
        )

        # Update order state
        order.filled_quantity += fill_qty
        order.updated_at = datetime.utcnow()

        prev_avg = order.average_fill_price or Decimal(0)
        prev_qty = order.filled_quantity - fill_qty
        order.average_fill_price = (
            (prev_avg * prev_qty + Decimal(fill_price_with_slippage) * fill_qty)
            / order.filled_quantity
        )

        if order.filled_quantity >= order.quantity:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        log.info(
            "paper_fill",
            order_id=order.order_id,
            ticker=order.ticker,
            fill_qty=fill_qty,
            fill_price=fill_price_with_slippage,
            status=order.status.value,
        )

        await self._fill_queue.put(fill)
        return order

    def _get_fill_price_and_qty(
        self, order: Order, snapshot: MarketSnapshot
    ) -> tuple[int | None, int]:
        """
        Determine the fill price and available quantity given the current book.

        For a BUY order, we look at the ask side (what sellers are offering).
        For a SELL order, we look at the bid side (what buyers are bidding).

        Returns (fill_price, available_qty). Returns (None, 0) if no fill possible.
        """
        book = snapshot.order_book
        is_buy = order.side == Side.BUY

        if order.contract_side.value == "yes":
            levels = book.yes_asks if is_buy else book.yes_bids
        else:
            levels = book.no_asks if is_buy else book.no_bids

        if not levels:
            return None, 0

        best = levels[0]  # Lowest ask for buys, highest bid for sells

        if order.limit_price is None:
            # Market order: fill at best available
            return best.price, best.quantity

        # Limit order: check if the limit is satisfiable
        if is_buy and best.price <= order.limit_price:
            return best.price, best.quantity
        elif not is_buy and best.price >= order.limit_price:
            return best.price, best.quantity

        return None, 0  # Limit not reachable

    def _apply_slippage(self, price: int, side: Side) -> int:
        """
        Apply slippage to a fill price.
        Buys pay more, sells receive less.
        Clamps to valid range [1, 99].
        """
        if side == Side.BUY:
            return min(99, price + self._slippage)
        else:
            return max(1, price - self._slippage)

    @staticmethod
    def _compute_fee(quantity: int, price: int) -> Decimal:
        """
        Estimate exchange fee.

        Kalshi charges a percentage of the contract value (typically ~7 cents
        per contract on a $1 contract). This is a placeholder.

        TODO: Update with Kalshi's actual fee schedule once you have it.
              Fee structures can vary by market type and your account tier.
        """
        # Placeholder: $0.07 per contract (7 cents)
        return Decimal("0.07") * quantity
