"""
WebSocket data feed.

Subscribes to Kalshi's WebSocket orderbook stream for all configured tickers.
Emits a MarketSnapshot on every delta update received from the exchange.

Tradeoff vs REST feed:
  + Much lower latency (push vs poll)
  + Single persistent connection vs N requests per poll cycle
  - More complex: must maintain book state, handle reconnects
  - Requires handling the initial snapshot + subsequent delta messages

This feed is the right choice for strategies that need to react quickly
to book changes (e.g. market making, momentum scalping).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import aiohttp

from bot.config.settings import DataFeedConfig, KalshiConfig
from bot.core.interfaces import DataFeed
from bot.core.models import MarketSnapshot, OrderBook
from bot.data.http_client import KalshiHttpClient
from bot.data.parser import parse_market_snapshot, parse_ws_orderbook_delta
from bot.logging.logger import get_logger

log = get_logger(__name__)

_RECONNECT_DELAY_SECONDS = 5.0
_SUBSCRIBE_MSG_TYPE = "subscribe"
_ORDERBOOK_SNAPSHOT_TYPE = "orderbook_snapshot"
_ORDERBOOK_DELTA_TYPE = "orderbook_delta"


class KalshiWsFeed(DataFeed):
    """
    WebSocket feed that maintains a live order book per ticker.

    On connect:
      1. Subscribes to orderbook channel for all configured tickers
      2. Receives a full snapshot for each ticker
      3. Receives incremental delta messages and applies them

    On disconnect:
      - Automatically reconnects after _RECONNECT_DELAY_SECONDS
      - Re-fetches REST snapshots to resync book state before re-subscribing
    """

    def __init__(self, kalshi_cfg: KalshiConfig, feed_cfg: DataFeedConfig) -> None:
        self._tickers = feed_cfg.tickers
        self._ws_url = kalshi_cfg.ws_url
        self._kalshi_cfg = kalshi_cfg
        self._http_client = KalshiHttpClient(
            base_url=kalshi_cfg.base_url,
            api_key=kalshi_cfg.api_key,
            api_secret=kalshi_cfg.api_secret,
        )
        # In-memory book state and market metadata, keyed by ticker
        self._books: dict[str, OrderBook] = {}
        self._market_meta: dict[str, dict] = {}
        self._running = False
        self._snapshot_queue: asyncio.Queue[MarketSnapshot] = asyncio.Queue()

    async def start(self) -> None:
        await self._http_client.start()
        self._running = True
        # Pre-fetch market metadata (title, close_time) via REST
        for ticker in self._tickers:
            try:
                self._market_meta[ticker] = await self._http_client.get_market(ticker)
            except Exception as exc:
                log.warning("ws_feed_meta_prefetch_failed", ticker=ticker, error=str(exc))
        log.info("ws_feed_started", tickers=self._tickers)

    async def stop(self) -> None:
        self._running = False
        await self._http_client.stop()
        log.info("ws_feed_stopped")

    async def stream(self) -> AsyncIterator[MarketSnapshot]:
        """
        Yields snapshots as they arrive from the WebSocket.
        The internal connection loop runs as a background task and populates
        a queue that this generator drains.
        """
        connection_task = asyncio.create_task(self._connection_loop())
        try:
            while self._running:
                try:
                    snapshot = await asyncio.wait_for(self._snapshot_queue.get(), timeout=1.0)
                    yield snapshot
                except asyncio.TimeoutError:
                    continue
        finally:
            connection_task.cancel()
            try:
                await connection_task
            except asyncio.CancelledError:
                pass

    async def _connection_loop(self) -> None:
        """Maintains the WebSocket connection, reconnecting on failure."""
        while self._running:
            try:
                await self._connect_and_consume()
            except Exception as exc:
                log.error("ws_connection_error", error=str(exc))
            if self._running:
                log.info("ws_reconnecting", delay=_RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    async def _connect_and_consume(self) -> None:
        """Open a WebSocket connection, subscribe, and process messages."""
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self._ws_url) as ws:
                log.info("ws_connected", url=self._ws_url)
                await self._subscribe(ws)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(json.loads(msg.data))
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.error("ws_error", error=str(ws.exception()))
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        log.warning("ws_closed")
                        break

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send subscription message for all configured tickers."""
        msg = {
            "id": 1,
            "cmd": _SUBSCRIBE_MSG_TYPE,
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": self._tickers,
            },
        }
        await ws.send_json(msg)
        log.info("ws_subscribed", tickers=self._tickers)

    async def _handle_message(self, msg: dict) -> None:
        """Route an incoming WebSocket message to the appropriate handler."""
        msg_type = msg.get("type", "")
        ticker = msg.get("msg", {}).get("market_ticker", "")

        if not ticker or ticker not in self._tickers:
            return

        if msg_type == _ORDERBOOK_SNAPSHOT_TYPE:
            book = self._apply_snapshot(ticker, msg["msg"])
        elif msg_type == _ORDERBOOK_DELTA_TYPE:
            book = self._apply_delta(ticker, msg["msg"])
        else:
            return

        meta = self._market_meta.get(ticker, {"ticker": ticker, "title": ticker})
        snapshot = parse_market_snapshot(meta, {"yes": [], "no": []})
        # Override the parsed book with our maintained state
        snapshot_with_book = MarketSnapshot(
            ticker=snapshot.ticker,
            title=snapshot.title,
            order_book=book,
            timestamp=snapshot.timestamp,
            close_time=snapshot.close_time,
            volume=snapshot.volume,
            open_interest=snapshot.open_interest,
            last_price=msg.get("msg", {}).get("last_price"),
        )
        await self._snapshot_queue.put(snapshot_with_book)

    def _apply_snapshot(self, ticker: str, msg: dict) -> OrderBook:
        """Replace the in-memory book with a full snapshot."""
        from bot.data.parser import parse_orderbook
        book = parse_orderbook(ticker, ticker, msg)
        self._books[ticker] = book
        log.debug("ws_book_snapshot", ticker=ticker)
        return book

    def _apply_delta(self, ticker: str, msg: dict) -> OrderBook:
        """Apply an incremental delta to the in-memory book."""
        current = self._books.get(ticker, OrderBook())
        updated = parse_ws_orderbook_delta(ticker, ticker, msg, current)
        self._books[ticker] = updated
        return updated
