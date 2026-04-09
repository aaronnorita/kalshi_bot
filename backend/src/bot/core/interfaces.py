"""
Abstract interfaces (protocols / ABCs) for the three main extension points:

  DataFeed       — produces MarketSnapshots
  ExecutionEngine — submits Orders and reports Fills
  StrategyBase   — consumes MarketSnapshots, produces Signals

Everything else in the framework depends on these interfaces, not on
concrete implementations. This makes it trivial to:
  - Swap PaperEngine → KalshiEngine without touching the runner
  - Test strategies with a fake data feed
  - Add a second exchange by implementing DataFeed + ExecutionEngine
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from bot.core.models import Fill, MarketSnapshot, Order, Position, Signal


# ---------------------------------------------------------------------------
# DataFeed
# ---------------------------------------------------------------------------


class DataFeed(ABC):
    """
    Produces a stream of MarketSnapshots for the markets this bot watches.

    Implementations:
      - KalshiRestFeed: polls the Kalshi REST API on an interval
      - KalshiWsFeed:   subscribes to Kalshi WebSocket orderbook updates
      - BacktestFeed:   replays historical snapshots from disk (for backtesting)
      - MockFeed:       emits synthetic snapshots (for unit tests)

    Design note: using AsyncIterator here means the runner loop is a simple
    `async for snapshot in feed:` — the feed implementation decides whether
    that's polling, websocket push, or reading from a file.
    """

    @abstractmethod
    async def stream(self) -> AsyncIterator[MarketSnapshot]:
        """
        Yields MarketSnapshots indefinitely (or until the feed is exhausted).
        Implementations should handle reconnection internally.
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize connections, authenticate, etc."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Tear down connections gracefully."""
        ...


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------


class ExecutionEngine(ABC):
    """
    Submits orders and reports back fills.

    Implementations:
      - PaperEngine:  simulates fills locally (Phase 1)
      - KalshiEngine: submits real orders to Kalshi REST API (Phase 6+)

    Design note: submit_order is synchronous-style (returns the Order object
    immediately with PENDING status). Fills arrive asynchronously via
    stream_fills(). This mirrors how real exchanges work and avoids blocking
    the main loop waiting for a fill.
    """

    @abstractmethod
    async def submit_order(self, order: Order) -> Order:
        """
        Submit an order. Returns immediately with the order in PENDING state.
        The order's exchange_order_id will be populated once acknowledged.
        """
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel a pending or open order by local order_id."""
        ...

    @abstractmethod
    async def stream_fills(self) -> AsyncIterator[Fill]:
        """
        Yields Fill events as they occur (async generator).
        For PaperEngine, fills are synthetic. For KalshiEngine, these come
        from the exchange WebSocket or polling.
        """
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]:
        """Return all currently open/pending orders."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize (authenticate, open connections, etc.)"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully close connections and cancel open orders if configured."""
        ...


# ---------------------------------------------------------------------------
# StrategyBase
# ---------------------------------------------------------------------------


class StrategyBase(ABC):
    """
    Base class for all trading strategies.

    YOU implement the subclass. The framework calls the hook methods below.
    Keep all your math, modeling, and signal logic inside these hooks.
    The framework guarantees they are called with correctly-typed inputs.

    Lifecycle:
        on_start()
          → on_snapshot()  [called for every new MarketSnapshot]
          → on_fill()      [called for every Fill event]
          → on_stop()

    The strategy MUST NOT:
      - Call the exchange API directly
      - Read from the state store directly (use the snapshot + fill callbacks)
      - Sleep or block (keep callbacks fast; heavy compute → background task)

    The strategy MUST return a Signal from on_snapshot(). Return
    Signal(action=SignalAction.HOLD, ticker=snapshot.ticker) to do nothing.
    """

    strategy_id: str  # Set by the runner from config

    @abstractmethod
    async def on_start(self) -> None:
        """
        Called once before the bot starts processing snapshots.
        Use this to load models, warm up indicators, etc.

        TODO: load your pre-trained models, calibration parameters, etc.
        """
        ...

    @abstractmethod
    async def on_snapshot(self, snapshot: MarketSnapshot, position: Position) -> Signal:
        """
        Called for every new market snapshot.

        Args:
            snapshot: Current market state (order book, last price, etc.)
            position: Your current open position in this market

        Returns:
            Signal describing what action to take (or HOLD to do nothing)

        TODO: THIS IS WHERE YOUR QUANTITATIVE LOGIC GOES.
              Read the snapshot, compute your signal, return it.
              Do not call the exchange here — just return a Signal.
        """
        ...

    @abstractmethod
    async def on_fill(self, fill: Fill, position: Position) -> None:
        """
        Called when one of your orders is (partially or fully) filled.

        Use this to update any internal state your strategy tracks
        (e.g. inventory risk, P&L tracking, signal cooldowns).

        TODO: update your strategy's internal state based on the fill.
        """
        ...

    @abstractmethod
    async def on_stop(self) -> None:
        """
        Called when the bot is shutting down.
        Use this to flush state, save model checkpoints, etc.

        TODO: persist any state you need to survive a restart.
        """
        ...
