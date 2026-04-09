"""
Mock data feed for testing and backtesting.

Emits a pre-supplied list of MarketSnapshots with a configurable delay.
Use this in unit tests to drive your strategy without any network calls:

    snapshots = [
        MarketSnapshot(ticker="TEST-1", ...),
        MarketSnapshot(ticker="TEST-1", ...),
    ]
    feed = MockFeed(snapshots, delay=0.0)
    async for snapshot in feed.stream():
        signal = await strategy.on_snapshot(snapshot, position)
        ...

Also useful for replaying historical data (backtesting): load snapshots
from a file and feed them through here.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from bot.core.interfaces import DataFeed
from bot.core.models import MarketSnapshot
from bot.logging.logger import get_logger

log = get_logger(__name__)


class MockFeed(DataFeed):
    """
    Emits a fixed sequence of snapshots, then stops.

    Args:
        snapshots:    Ordered list of snapshots to emit.
        delay:        Seconds to wait between emitting each snapshot.
                      Use 0.0 for tests; a small value to simulate real timing.
        loop:         If True, repeat the sequence indefinitely (useful for
                      stress-testing the runner loop).
    """

    def __init__(
        self,
        snapshots: list[MarketSnapshot],
        delay: float = 0.0,
        loop: bool = False,
    ) -> None:
        self._snapshots = snapshots
        self._delay = delay
        self._loop = loop
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("mock_feed_started", count=len(self._snapshots), loop=self._loop)

    async def stop(self) -> None:
        self._running = False

    async def stream(self) -> AsyncIterator[MarketSnapshot]:
        while self._running:
            for snapshot in self._snapshots:
                if not self._running:
                    return
                if self._delay > 0:
                    await asyncio.sleep(self._delay)
                yield snapshot
            if not self._loop:
                return
