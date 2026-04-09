"""
Live Kalshi execution engine.

Submits real orders to the Kalshi exchange via REST API.
Polls for fill status — a future upgrade can subscribe to the WS fill stream.

WARNING: This engine sends real orders with real money.
         Only use when execution.mode = "live" in config.yaml.
         You should have run extensive paper trading and backtesting first.

Fill detection strategy:
  After submitting an order, a background task polls GET /orders/{id} every
  poll_interval seconds until the order is terminal (filled, cancelled, or
  rejected). When a fill is detected, a Fill event is synthesized and emitted.

  Tradeoff: polling is simple but has latency proportional to poll_interval.
  For low-latency strategies, replace with a WebSocket fill subscription.
  TODO: add WS fill stream subscription for sub-second fill detection.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator

from bot.core.interfaces import ExecutionEngine
from bot.core.models import (
    ContractSide,
    Fill,
    Order,
    OrderStatus,
    Side,
)
from bot.data.http_client import KalshiHttpClient
from bot.logging.logger import get_logger

log = get_logger(__name__)

_FILL_POLL_INTERVAL_SECONDS = 2.0
_MAX_FILL_POLL_ATTEMPTS = 30  # Give up after 60 seconds


class KalshiEngine(ExecutionEngine):
    """
    Live execution engine using the Kalshi REST API.

    This is a stub with the full structure in place. Before going live:
      1. Verify _build_order_payload matches Kalshi's current API spec
      2. Verify _parse_fill correctly maps the API response to a Fill
      3. Test with very small position sizes first
    """

    def __init__(self, http_client: KalshiHttpClient) -> None:
        self._client = http_client
        self._open_orders: dict[str, Order] = {}   # local_order_id → Order
        self._exchange_id_map: dict[str, str] = {}  # local → exchange order id
        self._fill_queue: asyncio.Queue[Fill] = asyncio.Queue()
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("kalshi_engine_started")

    async def stop(self) -> None:
        self._running = False
        # Best-effort cancel all open orders on shutdown
        for order_id, order in list(self._open_orders.items()):
            if order.exchange_order_id:
                try:
                    await self._client.cancel_order(order.exchange_order_id)
                    log.info("kalshi_engine_cancelled_on_stop", order_id=order_id)
                except Exception as exc:
                    log.error("kalshi_engine_cancel_failed", order_id=order_id, error=str(exc))
        log.info("kalshi_engine_stopped")

    # ------------------------------------------------------------------
    # ExecutionEngine interface
    # ------------------------------------------------------------------

    async def submit_order(self, order: Order) -> Order:
        """Submit order to Kalshi. Returns order with exchange_order_id populated."""
        payload = self._build_order_payload(order)

        log.info(
            "kalshi_order_submitting",
            order_id=order.order_id,
            ticker=order.ticker,
            side=order.side.value,
            contract_side=order.contract_side.value,
            quantity=order.quantity,
            limit_price=order.limit_price,
        )

        response = await self._client.create_order(payload)
        order.exchange_order_id = response.get("order_id") or response.get("id")
        order.status = OrderStatus.OPEN
        order.updated_at = datetime.utcnow()

        self._open_orders[order.order_id] = order
        if order.exchange_order_id:
            self._exchange_id_map[order.order_id] = order.exchange_order_id

        # Background task: poll for fills
        asyncio.create_task(self._poll_for_fills(order))

        log.info(
            "kalshi_order_submitted",
            order_id=order.order_id,
            exchange_order_id=order.exchange_order_id,
        )
        return order

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an open order by local order_id."""
        order = self._open_orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        exchange_id = order.exchange_order_id
        if exchange_id is None:
            raise ValueError(f"Order {order_id} has no exchange_order_id yet")

        await self._client.cancel_order(exchange_id)
        order.status = OrderStatus.CANCELLED
        order.updated_at = datetime.utcnow()
        self._open_orders.pop(order_id, None)

        log.info("kalshi_order_cancelled", order_id=order_id, exchange_order_id=exchange_id)
        return order

    async def stream_fills(self) -> AsyncIterator[Fill]:
        """Yield fill events as they are detected by the polling tasks."""
        while self._running:
            try:
                fill = await asyncio.wait_for(self._fill_queue.get(), timeout=0.5)
                yield fill
            except asyncio.TimeoutError:
                continue

    async def get_open_orders(self) -> list[Order]:
        return list(self._open_orders.values())

    # ------------------------------------------------------------------
    # Fill polling
    # ------------------------------------------------------------------

    async def _poll_for_fills(self, order: Order) -> None:
        """Poll the exchange until the order is terminal, then emit fills."""
        exchange_id = order.exchange_order_id
        if not exchange_id:
            return

        for attempt in range(_MAX_FILL_POLL_ATTEMPTS):
            await asyncio.sleep(_FILL_POLL_INTERVAL_SECONDS)

            try:
                response = await self._client.get_order(exchange_id)
            except Exception as exc:
                log.warning("fill_poll_failed", exchange_order_id=exchange_id, error=str(exc))
                continue

            new_filled_qty = response.get("filled_yes_count", 0) + response.get("filled_no_count", 0)
            status_str = response.get("status", "")
            new_status = _parse_order_status(status_str)

            # Detect new fill quantity since last poll
            prev_filled = order.filled_quantity
            if new_filled_qty > prev_filled:
                delta = new_filled_qty - prev_filled
                fill_price = int(response.get("average_yes_cents", order.limit_price or 50))
                fill = Fill(
                    order_id=order.order_id,
                    ticker=order.ticker,
                    side=order.side,
                    contract_side=order.contract_side,
                    quantity=delta,
                    price=fill_price,
                    fee=_estimate_fee(delta, fill_price),
                    filled_at=datetime.utcnow(),
                )
                await self._fill_queue.put(fill)
                order.filled_quantity = new_filled_qty
                log.info(
                    "kalshi_fill_detected",
                    order_id=order.order_id,
                    delta=delta,
                    price=fill_price,
                )

            order.status = new_status
            order.updated_at = datetime.utcnow()

            if order.is_terminal:
                self._open_orders.pop(order.order_id, None)
                log.info("kalshi_order_terminal", order_id=order.order_id, status=new_status.value)
                return

        log.warning(
            "fill_poll_exhausted",
            order_id=order.order_id,
            exchange_order_id=exchange_id,
            attempts=_MAX_FILL_POLL_ATTEMPTS,
        )

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_order_payload(order: Order) -> dict:
        """
        Build the JSON payload for POST /orders.

        Kalshi order payload reference:
          {
            "ticker": "MARKET-TICKER",
            "action": "buy",          # "buy" or "sell"
            "side": "yes",            # "yes" or "no"
            "count": 5,               # number of contracts
            "type": "limit",          # "limit" or "market"
            "yes_price": 63,          # limit price in cents (for YES side)
            "client_order_id": "..."  # optional idempotency key
          }

        TODO: verify this payload shape against the current Kalshi API docs.
              The field names (particularly yes_price vs no_price) may differ.
        """
        payload: dict = {
            "ticker": order.ticker,
            "action": order.side.value,
            "side": order.contract_side.value,
            "count": order.quantity,
            "client_order_id": order.order_id,
        }

        if order.limit_price is not None:
            payload["type"] = "limit"
            # Kalshi uses yes_price for YES contracts, no_price for NO contracts
            if order.contract_side == ContractSide.YES:
                payload["yes_price"] = order.limit_price
            else:
                payload["no_price"] = order.limit_price
        else:
            payload["type"] = "market"

        return payload


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_order_status(status_str: str) -> OrderStatus:
    """Map Kalshi's status strings to our OrderStatus enum."""
    mapping = {
        "resting": OrderStatus.OPEN,
        "pending": OrderStatus.PENDING,
        "executed": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
    }
    return mapping.get(status_str.lower(), OrderStatus.OPEN)


def _estimate_fee(quantity: int, price: int) -> Decimal:
    """
    Estimate Kalshi exchange fee.
    TODO: update with actual Kalshi fee schedule.
    """
    return Decimal("0.07") * quantity
