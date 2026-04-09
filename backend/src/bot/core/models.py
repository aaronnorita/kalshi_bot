"""
Core domain models for the Kalshi trading bot.

Kalshi-specific terminology:
  - Market: a prediction market event (e.g. "Will BTC close above $50k on Jan 1?")
  - Ticker: unique string identifier for a market (e.g. "BTC-ABOVE-50K-JAN-1")
  - Price: integer 0–100 representing cents (probability × 100)
  - ContractSide: YES or NO — which side of the binary outcome you're trading
  - Contract: one unit of a position; resolves to $1 (win) or $0 (loss)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Side(str, Enum):
    """Direction of an order or position."""
    BUY = "buy"
    SELL = "sell"


class ContractSide(str, Enum):
    """Which contract side — YES or NO — is being traded."""
    YES = "yes"
    NO = "no"


class OrderStatus(str, Enum):
    PENDING = "pending"       # Submitted but not yet acknowledged
    OPEN = "open"             # Resting on the book
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"         # Fully executed
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalAction(str, Enum):
    BUY_YES = "buy_yes"       # Buy YES contracts
    BUY_NO = "buy_no"         # Buy NO contracts
    SELL_YES = "sell_yes"     # Sell YES contracts
    SELL_NO = "sell_no"       # Sell NO contracts
    HOLD = "hold"             # Do nothing


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderBookLevel:
    """A single price level in the order book."""
    price: int           # 0–100 cents
    quantity: int        # Number of contracts available at this price


@dataclass(frozen=True)
class OrderBook:
    """
    Snapshot of the order book for one side (YES or NO) of a market.

    yes_bids / yes_asks: best bids/asks for YES contracts (price in 0–100)
    no_bids / no_asks:  best bids/asks for NO contracts

    Note: on Kalshi, YES price + NO price ≈ 100 (they're complementary).
    The spread between bid and ask is where market makers earn their edge.
    """
    yes_bids: list[OrderBookLevel] = field(default_factory=list)  # Highest first
    yes_asks: list[OrderBookLevel] = field(default_factory=list)  # Lowest first
    no_bids: list[OrderBookLevel] = field(default_factory=list)
    no_asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_yes_bid(self) -> Optional[OrderBookLevel]:
        return self.yes_bids[0] if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Optional[OrderBookLevel]:
        return self.yes_asks[0] if self.yes_asks else None

    @property
    def best_no_bid(self) -> Optional[OrderBookLevel]:
        return self.no_bids[0] if self.no_bids else None

    @property
    def best_no_ask(self) -> Optional[OrderBookLevel]:
        return self.no_asks[0] if self.no_asks else None

    @property
    def yes_mid(self) -> Optional[float]:
        """Midpoint of the YES market. Returns None if either side is empty."""
        if self.best_yes_bid and self.best_yes_ask:
            return (self.best_yes_bid.price + self.best_yes_ask.price) / 2
        return None

    @property
    def yes_spread(self) -> Optional[int]:
        """Bid-ask spread in cents for YES contracts."""
        if self.best_yes_bid and self.best_yes_ask:
            return self.best_yes_ask.price - self.best_yes_bid.price
        return None


# ---------------------------------------------------------------------------
# Market snapshot — primary input to your strategy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Immutable snapshot of a market at a point in time.
    This is the primary data structure your strategy receives.

    All timestamps are UTC.
    """
    ticker: str
    title: str                   # Human-readable market question
    order_book: OrderBook
    timestamp: datetime          # When this snapshot was captured
    close_time: Optional[datetime]  # When the market resolves (if known)

    # Volume / open interest (None if unavailable from API)
    volume: Optional[int] = None        # Total contracts traded today
    open_interest: Optional[int] = None # Outstanding open positions

    # Last traded price for YES contracts (None if no trades yet)
    last_price: Optional[int] = None

    @property
    def seconds_to_close(self) -> Optional[float]:
        """Seconds remaining until market resolution."""
        if self.close_time is None:
            return None
        return (self.close_time - self.timestamp).total_seconds()


# ---------------------------------------------------------------------------
# Signal — output from your strategy, input to the execution engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    """
    Trading signal produced by a strategy.

    The strategy populates this; the risk guard inspects it; the execution
    engine acts on it.

    Fields:
        action:     What to do (buy YES, buy NO, sell YES, sell NO, hold)
        ticker:     Which market to trade
        quantity:   Number of contracts (must be > 0 for non-HOLD signals)
        limit_price: Max price to pay (cents, 0–100). None = market order.
        confidence: TODO — your strategy should populate this (0.0–1.0).
                    Infrastructure uses it only for logging; risk guards may
                    optionally gate on minimum confidence.
        reason:     Human-readable explanation for logging/debugging
        strategy_id: Which strategy instance generated this signal
    """
    action: SignalAction
    ticker: str
    quantity: int = 0
    limit_price: Optional[int] = None
    confidence: Optional[float] = None  # TODO: set in your strategy
    reason: str = ""
    strategy_id: str = ""
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def is_actionable(self) -> bool:
        """Returns True if this signal should result in an order."""
        return self.action != SignalAction.HOLD and self.quantity > 0


# ---------------------------------------------------------------------------
# Order — submitted to the exchange
# ---------------------------------------------------------------------------


@dataclass
class Order:
    """
    Represents a single order sent to the exchange (or paper engine).

    order_id is generated locally; exchange_order_id is assigned by Kalshi
    after acknowledgement.
    """
    ticker: str
    side: Side
    contract_side: ContractSide  # YES or NO contracts
    quantity: int
    limit_price: Optional[int]   # None = market order
    status: OrderStatus = OrderStatus.PENDING
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    exchange_order_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    filled_quantity: int = 0
    average_fill_price: Optional[Decimal] = None
    signal_ref: Optional[str] = None  # Links back to the Signal that created this

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        )


# ---------------------------------------------------------------------------
# Fill — confirmed execution of (part of) an order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fill:
    """A confirmed execution event — one order can have multiple partial fills."""
    order_id: str
    ticker: str
    side: Side
    contract_side: ContractSide
    quantity: int
    price: int          # Actual fill price in cents
    fee: Decimal        # Exchange fee in dollars
    filled_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Position — current holdings in a market
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """
    Current open position in a single market.

    net_yes_contracts > 0 means you hold YES contracts (long YES).
    net_yes_contracts < 0 means you hold NO contracts (effectively short YES).

    We track YES and NO separately because you can hold both sides
    simultaneously (though that's usually a mistake unless hedging).
    """
    ticker: str
    yes_contracts: int = 0    # Contracts held on the YES side
    no_contracts: int = 0     # Contracts held on the NO side
    avg_yes_cost: Decimal = Decimal("0")   # Average cost basis (cents)
    avg_no_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")  # Closed P&L in dollars
    last_updated: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_flat(self) -> bool:
        return self.yes_contracts == 0 and self.no_contracts == 0

    def unrealized_pnl(self, current_yes_price: int) -> Decimal:
        """
        Estimate unrealized P&L given current YES mid price (0–100 cents).

        TODO: You may want to replace this with a more sophisticated mark-to-
        market calculation that accounts for bid-ask spread, time value, etc.
        """
        yes_value = Decimal(self.yes_contracts) * Decimal(current_yes_price) / 100
        yes_cost = Decimal(self.yes_contracts) * self.avg_yes_cost / 100
        no_implied_price = 100 - current_yes_price
        no_value = Decimal(self.no_contracts) * Decimal(no_implied_price) / 100
        no_cost = Decimal(self.no_contracts) * self.avg_no_cost / 100
        return (yes_value - yes_cost) + (no_value - no_cost)
