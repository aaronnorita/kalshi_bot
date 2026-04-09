"""
Parse raw Kalshi API responses into domain models.

Kalshi REST API reference shapes used here:
  GET /markets/{ticker}/orderbook  → { "orderbook": { "yes": [[price, qty], ...], "no": [...] } }
  GET /markets/{ticker}            → { "market": { "ticker", "title", "close_time", ... } }

All parsing is isolated here so the feed implementations stay thin.
If Kalshi changes their API shape, you only change this file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.core.models import MarketSnapshot, OrderBook, OrderBookLevel


def _parse_levels(raw: list[list[int]]) -> list[OrderBookLevel]:
    """Convert [[price, qty], ...] to a list of OrderBookLevel."""
    return [OrderBookLevel(price=row[0], quantity=row[1]) for row in raw]


def parse_orderbook(ticker: str, title: str, raw: dict[str, Any]) -> OrderBook:
    """
    Parse the 'orderbook' sub-object from Kalshi's orderbook endpoint.

    Expected raw shape:
        {
            "yes": [[price, qty], ...],   # bids for YES (highest first)
            "no":  [[price, qty], ...]    # bids for NO  (highest first)
        }

    Kalshi only provides one side of the book per contract type (the bids).
    The asks for YES are implicitly the NO bids, and vice versa:
        YES ask price ≈ 100 - NO bid price

    We populate both bid and ask sides using this relationship.
    """
    yes_bids_raw: list[list[int]] = raw.get("yes", [])
    no_bids_raw: list[list[int]] = raw.get("no", [])

    yes_bids = sorted(_parse_levels(yes_bids_raw), key=lambda l: -l.price)
    no_bids = sorted(_parse_levels(no_bids_raw), key=lambda l: -l.price)

    # Derive implicit ask prices: YES ask = 100 - best NO bid (and vice versa)
    yes_asks = sorted(
        [OrderBookLevel(price=100 - lvl.price, quantity=lvl.quantity) for lvl in no_bids],
        key=lambda l: l.price,
    )
    no_asks = sorted(
        [OrderBookLevel(price=100 - lvl.price, quantity=lvl.quantity) for lvl in yes_bids],
        key=lambda l: l.price,
    )

    return OrderBook(
        yes_bids=yes_bids,
        yes_asks=yes_asks,
        no_bids=no_bids,
        no_asks=no_asks,
    )


def parse_market_snapshot(market_data: dict[str, Any], orderbook_data: dict[str, Any]) -> MarketSnapshot:
    """
    Combine market metadata + orderbook data into a MarketSnapshot.

    market_data: the 'market' object from GET /markets/{ticker}
    orderbook_data: the 'orderbook' object from GET /markets/{ticker}/orderbook
    """
    ticker: str = market_data["ticker"]
    title: str = market_data.get("title", ticker)

    close_time: datetime | None = None
    if raw_close := market_data.get("close_time"):
        close_time = datetime.fromisoformat(raw_close.replace("Z", "+00:00"))

    order_book = parse_orderbook(ticker, title, orderbook_data)

    return MarketSnapshot(
        ticker=ticker,
        title=title,
        order_book=order_book,
        timestamp=datetime.now(tz=timezone.utc),
        close_time=close_time,
        volume=market_data.get("volume"),
        open_interest=market_data.get("open_interest"),
        last_price=market_data.get("last_price"),
    )


def parse_ws_orderbook_delta(
    ticker: str,
    title: str,
    msg: dict[str, Any],
    current_book: OrderBook,
) -> OrderBook:
    """
    Apply a WebSocket delta message to the current order book.

    Kalshi WS sends incremental updates. A quantity of 0 means remove that level.
    msg shape: { "yes": [[price, qty], ...], "no": [[price, qty], ...] }

    Returns a new OrderBook with the delta applied.
    """
    # Build mutable dicts: price -> qty
    yes_bid_map = {lvl.price: lvl.quantity for lvl in current_book.yes_bids}
    no_bid_map = {lvl.price: lvl.quantity for lvl in current_book.no_bids}

    for price, qty in msg.get("yes", []):
        if qty == 0:
            yes_bid_map.pop(price, None)
        else:
            yes_bid_map[price] = qty

    for price, qty in msg.get("no", []):
        if qty == 0:
            no_bid_map.pop(price, None)
        else:
            no_bid_map[price] = qty

    yes_bids = sorted(
        [OrderBookLevel(price=p, quantity=q) for p, q in yes_bid_map.items()],
        key=lambda l: -l.price,
    )
    no_bids = sorted(
        [OrderBookLevel(price=p, quantity=q) for p, q in no_bid_map.items()],
        key=lambda l: -l.price,
    )
    yes_asks = sorted(
        [OrderBookLevel(price=100 - lvl.price, quantity=lvl.quantity) for lvl in no_bids],
        key=lambda l: l.price,
    )
    no_asks = sorted(
        [OrderBookLevel(price=100 - lvl.price, quantity=lvl.quantity) for lvl in yes_bids],
        key=lambda l: l.price,
    )

    return OrderBook(yes_bids=yes_bids, yes_asks=yes_asks, no_bids=no_bids, no_asks=no_asks)
