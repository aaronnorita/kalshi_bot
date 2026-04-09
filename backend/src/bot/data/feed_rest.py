"""
REST polling data feed.

Polls the Kalshi REST API on a fixed interval for each configured ticker.
Emits one MarketSnapshot per ticker per poll cycle.

Tradeoff vs WebSocket:
  + Simpler, stateless, easier to debug
  + Works even if the WS connection drops
  - Higher latency (limited by poll_interval_seconds)
  - More API requests (counts against rate limits)

Use this feed during development and backtesting. Switch to KalshiWsFeed
when you need sub-second latency for market making strategies.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from bot.config.settings import DataFeedConfig, KalshiConfig
from bot.core.interfaces import DataFeed
from bot.core.models import MarketSnapshot
from bot.data.http_client import KalshiHttpClient
from bot.data.parser import parse_market_snapshot
from bot.logging.logger import get_logger

log = get_logger(__name__)


class KalshiRestFeed(DataFeed):
    """
    Polls Kalshi REST API for order book snapshots.

    For each poll cycle, fetches market metadata + orderbook for every
    configured ticker, then emits snapshots one by one.
    """

    def __init__(self, kalshi_cfg: KalshiConfig, feed_cfg: DataFeedConfig) -> None:
        self._tickers = feed_cfg.tickers
        self._interval = feed_cfg.poll_interval_seconds
        self._client = KalshiHttpClient(
            base_url=kalshi_cfg.base_url,
            api_key=kalshi_cfg.api_key,
            api_secret=kalshi_cfg.api_secret,
            timeout_seconds=kalshi_cfg.request_timeout_seconds,
            max_retries=kalshi_cfg.max_retries,
            retry_backoff_seconds=kalshi_cfg.retry_backoff_seconds,
        )
        self._running = False

    async def start(self) -> None:
        await self._client.start()
        self._running = True
        log.info("rest_feed_started", tickers=self._tickers, interval=self._interval)

    async def stop(self) -> None:
        self._running = False
        await self._client.stop()
        log.info("rest_feed_stopped")

    async def stream(self) -> AsyncIterator[MarketSnapshot]:
        """
        Yields snapshots in a continuous loop until stop() is called.
        Each poll cycle fetches all tickers and yields them sequentially.
        """
        while self._running:
            cycle_start = asyncio.get_event_loop().time()

            for ticker in self._tickers:
                if not self._running:
                    return
                snapshot = await self._fetch_snapshot(ticker)
                if snapshot is not None:
                    yield snapshot

            # Sleep for the remainder of the interval
            elapsed = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, self._interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _fetch_snapshot(self, ticker: str) -> MarketSnapshot | None:
        """Fetch market + orderbook for one ticker. Returns None on error."""
        try:
            market_data, orderbook_data = await asyncio.gather(
                self._client.get_market(ticker),
                self._client.get_orderbook(ticker),
            )
            snapshot = parse_market_snapshot(market_data, orderbook_data)
            log.debug("snapshot_fetched", ticker=ticker, yes_mid=snapshot.order_book.yes_mid)
            return snapshot
        except Exception as exc:
            log.error("snapshot_fetch_failed", ticker=ticker, error=str(exc))
            return None
