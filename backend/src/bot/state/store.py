"""
Persistent state store using SQLite.

Responsibilities:
  - Track open positions (contracts held per market)
  - Track order history
  - Track daily spend and P&L (for risk guard queries)
  - Survive restarts: on startup the bot reloads its positions from the DB

The store is the single source of truth for what the bot currently owns.
Strategies should NOT maintain their own position state — read it from here
via the Position object passed into on_snapshot() and on_fill().
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Generator, Optional

from bot.core.models import Fill, Order, OrderStatus, Position
from bot.logging.logger import get_logger

log = get_logger(__name__)


class StateStore:
    """
    SQLite-backed state store.

    Thread safety: not thread-safe. All calls must come from the same
    async event loop (the bot runner). Do not share across threads.
    """

    def __init__(self, db_path: str = "data/state.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the database connection and create tables if needed."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
        self._create_tables()
        log.info("state_store_opened", db_path=str(self._db_path))

    def close(self) -> None:
        """Flush and close the database connection."""
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None
            log.info("state_store_closed")

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for a DB transaction. Rolls back on exception."""
        assert self._conn is not None, "StateStore.open() must be called first"
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                ticker          TEXT PRIMARY KEY,
                yes_contracts   INTEGER NOT NULL DEFAULT 0,
                no_contracts    INTEGER NOT NULL DEFAULT 0,
                avg_yes_cost    TEXT NOT NULL DEFAULT '0',   -- Decimal as string
                avg_no_cost     TEXT NOT NULL DEFAULT '0',
                realized_pnl    TEXT NOT NULL DEFAULT '0',
                last_updated    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id            TEXT PRIMARY KEY,
                exchange_order_id   TEXT,
                ticker              TEXT NOT NULL,
                side                TEXT NOT NULL,
                contract_side       TEXT NOT NULL,
                quantity            INTEGER NOT NULL,
                limit_price         INTEGER,
                status              TEXT NOT NULL,
                filled_quantity     INTEGER NOT NULL DEFAULT 0,
                average_fill_price  TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                signal_ref          TEXT
            );

            CREATE TABLE IF NOT EXISTS fills (
                fill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id        TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                side            TEXT NOT NULL,
                contract_side   TEXT NOT NULL,
                quantity        INTEGER NOT NULL,
                price           INTEGER NOT NULL,
                fee             TEXT NOT NULL,
                filled_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_stats (
                stat_date       TEXT PRIMARY KEY,   -- ISO date YYYY-MM-DD
                total_spend_cents   INTEGER NOT NULL DEFAULT 0,
                total_pnl_cents     INTEGER NOT NULL DEFAULT 0
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def get_position(self, ticker: str) -> Position:
        """Return the current position for a market. Returns a flat position if none exists."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM positions WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row is None:
            return Position(ticker=ticker)
        return Position(
            ticker=row["ticker"],
            yes_contracts=row["yes_contracts"],
            no_contracts=row["no_contracts"],
            avg_yes_cost=Decimal(row["avg_yes_cost"]),
            avg_no_cost=Decimal(row["avg_no_cost"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            last_updated=datetime.fromisoformat(row["last_updated"]),
        )

    def get_all_positions(self) -> list[Position]:
        """Return all non-flat positions."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE yes_contracts != 0 OR no_contracts != 0"
        ).fetchall()
        return [
            Position(
                ticker=r["ticker"],
                yes_contracts=r["yes_contracts"],
                no_contracts=r["no_contracts"],
                avg_yes_cost=Decimal(r["avg_yes_cost"]),
                avg_no_cost=Decimal(r["avg_no_cost"]),
                realized_pnl=Decimal(r["realized_pnl"]),
                last_updated=datetime.fromisoformat(r["last_updated"]),
            )
            for r in rows
        ]

    def apply_fill(self, fill: Fill) -> Position:
        """
        Update position state based on a fill event.
        Returns the updated Position.

        Cost basis is updated using a weighted average.
        """
        pos = self.get_position(fill.ticker)
        now = datetime.utcnow().isoformat()

        if fill.contract_side.value == "yes":
            if fill.side.value == "buy":
                # Buying more YES: update weighted average cost
                total_contracts = pos.yes_contracts + fill.quantity
                new_avg = (
                    (pos.avg_yes_cost * pos.yes_contracts + Decimal(fill.price) * fill.quantity)
                    / total_contracts
                    if total_contracts > 0
                    else Decimal(fill.price)
                )
                new_yes = pos.yes_contracts + fill.quantity
                new_realized = pos.realized_pnl
                new_avg_yes = new_avg
                new_avg_no = pos.avg_no_cost
                new_no = pos.no_contracts
            else:
                # Selling YES: realize P&L
                realized_per_contract = Decimal(fill.price) - pos.avg_yes_cost
                new_realized = pos.realized_pnl + realized_per_contract * fill.quantity / 100
                new_yes = pos.yes_contracts - fill.quantity
                new_avg_yes = pos.avg_yes_cost
                new_avg_no = pos.avg_no_cost
                new_no = pos.no_contracts
        else:  # NO side
            if fill.side.value == "buy":
                total_contracts = pos.no_contracts + fill.quantity
                new_avg = (
                    (pos.avg_no_cost * pos.no_contracts + Decimal(fill.price) * fill.quantity)
                    / total_contracts
                    if total_contracts > 0
                    else Decimal(fill.price)
                )
                new_no = pos.no_contracts + fill.quantity
                new_realized = pos.realized_pnl
                new_avg_yes = pos.avg_yes_cost
                new_avg_no = new_avg
                new_yes = pos.yes_contracts
            else:
                realized_per_contract = Decimal(fill.price) - pos.avg_no_cost
                new_realized = pos.realized_pnl + realized_per_contract * fill.quantity / 100
                new_no = pos.no_contracts - fill.quantity
                new_avg_yes = pos.avg_yes_cost
                new_avg_no = pos.avg_no_cost
                new_yes = pos.yes_contracts

        updated_pos = Position(
            ticker=fill.ticker,
            yes_contracts=max(0, new_yes),
            no_contracts=max(0, new_no),
            avg_yes_cost=new_avg_yes,
            avg_no_cost=new_avg_no,
            realized_pnl=new_realized,
            last_updated=datetime.utcnow(),
        )

        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO positions (ticker, yes_contracts, no_contracts,
                    avg_yes_cost, avg_no_cost, realized_pnl, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    yes_contracts = excluded.yes_contracts,
                    no_contracts = excluded.no_contracts,
                    avg_yes_cost = excluded.avg_yes_cost,
                    avg_no_cost = excluded.avg_no_cost,
                    realized_pnl = excluded.realized_pnl,
                    last_updated = excluded.last_updated
            """, (
                fill.ticker,
                updated_pos.yes_contracts,
                updated_pos.no_contracts,
                str(updated_pos.avg_yes_cost),
                str(updated_pos.avg_no_cost),
                str(updated_pos.realized_pnl),
                now,
            ))

        return updated_pos

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def save_order(self, order: Order) -> None:
        """Insert or update an order."""
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO orders (order_id, exchange_order_id, ticker, side,
                    contract_side, quantity, limit_price, status, filled_quantity,
                    average_fill_price, created_at, updated_at, signal_ref)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    status = excluded.status,
                    filled_quantity = excluded.filled_quantity,
                    average_fill_price = excluded.average_fill_price,
                    updated_at = excluded.updated_at
            """, (
                order.order_id,
                order.exchange_order_id,
                order.ticker,
                order.side.value,
                order.contract_side.value,
                order.quantity,
                order.limit_price,
                order.status.value,
                order.filled_quantity,
                str(order.average_fill_price) if order.average_fill_price else None,
                order.created_at.isoformat(),
                order.updated_at.isoformat(),
                order.signal_ref,
            ))

    def save_fill(self, fill: Fill) -> None:
        """Persist a fill event."""
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO fills (order_id, ticker, side, contract_side,
                    quantity, price, fee, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fill.order_id,
                fill.ticker,
                fill.side.value,
                fill.contract_side.value,
                fill.quantity,
                fill.price,
                str(fill.fee),
                fill.filled_at.isoformat(),
            ))

    # ------------------------------------------------------------------
    # Daily stats (used by risk guards)
    # ------------------------------------------------------------------

    def get_daily_spend_cents(self, for_date: Optional[date] = None) -> int:
        """Total spend in cents for a given date (defaults to today UTC)."""
        assert self._conn is not None
        d = (for_date or date.today()).isoformat()
        row = self._conn.execute(
            "SELECT total_spend_cents FROM daily_stats WHERE stat_date = ?", (d,)
        ).fetchone()
        return row["total_spend_cents"] if row else 0

    def add_daily_spend(self, amount_cents: int, for_date: Optional[date] = None) -> None:
        """Record spend. Called by the runner after each fill."""
        d = (for_date or date.today()).isoformat()
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO daily_stats (stat_date, total_spend_cents)
                VALUES (?, ?)
                ON CONFLICT(stat_date) DO UPDATE SET
                    total_spend_cents = total_spend_cents + excluded.total_spend_cents
            """, (d, amount_cents))

    def get_daily_pnl_cents(self, for_date: Optional[date] = None) -> int:
        """Total realized P&L in cents for a given date."""
        assert self._conn is not None
        d = (for_date or date.today()).isoformat()
        row = self._conn.execute(
            "SELECT total_pnl_cents FROM daily_stats WHERE stat_date = ?", (d,)
        ).fetchone()
        return row["total_pnl_cents"] if row else 0

    def update_daily_pnl(self, delta_cents: int, for_date: Optional[date] = None) -> None:
        """Adjust daily P&L. Called when positions close."""
        d = (for_date or date.today()).isoformat()
        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO daily_stats (stat_date, total_pnl_cents)
                VALUES (?, ?)
                ON CONFLICT(stat_date) DO UPDATE SET
                    total_pnl_cents = total_pnl_cents + excluded.total_pnl_cents
            """, (d, delta_cents))
