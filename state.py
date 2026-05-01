"""
state.py -- Shared mutable state for the scalping bot.

All state is protected by asyncio.Lock so concurrent tasks (order book
consumer, fill listener, strategy loop) cannot produce a partial read/write.

Key design choice: a single lock per StrategyState instance. Because we run
one strategy per market, there is no cross-market contention.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import config

logger = logging.getLogger(__name__)


class PositionState(Enum):
    """
    Lifecycle of a single trade attempt on one market.

    Transitions (happy path):
        IDLE -> BUY_ORDER_PLACED -> POSITION_FILLED -> SELL_ORDER_PLACED -> CLOSED

    Stop-loss path (either trigger):
        SELL_ORDER_PLACED -> LIQUIDATING -> CLOSED
    """
    IDLE = "idle"
    BUY_ORDER_PLACED = "buy_order_placed"
    POSITION_FILLED = "position_filled"
    SELL_ORDER_PLACED = "sell_order_placed"
    LIQUIDATING = "liquidating"
    CLOSED = "closed"


@dataclass
class Position:
    """Snapshot of an open trade."""
    token_id: str
    tick_size: float

    # Set when the buy limit order is submitted.
    buy_order_id: str = ""

    # Set when the buy order fills (from UserEventConsumer callback).
    fill_price: float = 0.0
    fill_size: float = 0.0
    entry_time: Optional[datetime] = None

    # Context captured at entry -- used for trade log analysis.
    imbalance_at_entry: float = 0.0
    mid_at_entry: float = 0.0
    secs_to_expiry_at_entry: float = 0.0

    # Set after the take-profit sell order is placed.
    sell_order_id: str = ""
    tp_price: float = 0.0

    # Running PnL for logging (set on close).
    realized_pnl: float = 0.0


class StrategyState:
    """
    Central state container for one market's strategy instance.

    Shared between:
      - MarketDataConsumer  (writes order_book)
      - UserEventConsumer   (fires fill callbacks which write position)
      - ScalpingStrategy    (reads everything, writes phase transitions)

    All public mutating methods acquire self._lock before modifying state,
    so callers never need to manage locking themselves.
    """

    def __init__(self, market_end_time: datetime) -> None:
        self._lock = asyncio.Lock()

        # Current strategy phase.
        self.phase: PositionState = PositionState.IDLE

        # Open position (None when IDLE or CLOSED).
        self.position: Optional[Position] = None

        # Order book snapshot -- {price_float: size_float}.
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

        # When this market expires (UTC).
        self.market_end_time: datetime = market_end_time

        # Kill-switch counters.
        self.consecutive_losses: int = 0
        self.kill_switch_until: Optional[datetime] = None

        # Fill event: strategy loop sets this to None, UserEventConsumer
        # sets it to a (order_id, fill_price, fill_size) tuple when a fill
        # arrives. The strategy loop drains it and acts accordingly.
        self.pending_fill: Optional[tuple[str, float, float]] = None

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------

    async def update_order_book(
        self,
        bids: dict[float, float],
        asks: dict[float, float],
    ) -> None:
        async with self._lock:
            self.bids = bids
            self.asks = asks

    async def get_order_book(self) -> tuple[dict[float, float], dict[float, float]]:
        async with self._lock:
            return dict(self.bids), dict(self.asks)

    # ------------------------------------------------------------------
    # Phase transitions
    # ------------------------------------------------------------------

    async def transition(self, new_phase: PositionState) -> None:
        async with self._lock:
            logger.info("Phase transition: %s -> %s", self.phase.value, new_phase.value)
            self.phase = new_phase

    async def get_phase(self) -> PositionState:
        async with self._lock:
            return self.phase

    # ------------------------------------------------------------------
    # Fill notification (written by UserEventConsumer, read by strategy)
    # ------------------------------------------------------------------

    async def set_pending_fill(
        self, order_id: str, fill_price: float, fill_size: float
    ) -> None:
        async with self._lock:
            self.pending_fill = (order_id, fill_price, fill_size)

    async def consume_pending_fill(self) -> Optional[tuple[str, float, float]]:
        """Returns and clears the pending fill in one atomic step."""
        async with self._lock:
            fill = self.pending_fill
            self.pending_fill = None
            return fill

    # ------------------------------------------------------------------
    # Kill-switch / consecutive loss tracking
    # ------------------------------------------------------------------

    async def is_kill_switch_active(self) -> bool:
        async with self._lock:
            if self.kill_switch_until is None:
                return False
            now = datetime.now(tz=timezone.utc)
            active = now < self.kill_switch_until
            if not active:
                # Auto-expire once the pause window has passed.
                self.kill_switch_until = None
                self.consecutive_losses = 0
            return active

    async def record_loss(self) -> None:
        async with self._lock:
            self.consecutive_losses += 1
            logger.warning(
                "Stop-loss triggered. Consecutive losses: %d / %d",
                self.consecutive_losses,
                config.MAX_CONSECUTIVE_LOSSES,
            )
            if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
                from datetime import timedelta
                self.kill_switch_until = datetime.now(tz=timezone.utc) + timedelta(
                    seconds=config.KILL_SWITCH_PAUSE_SECONDS
                )
                logger.error(
                    "[KILL SWITCH ACTIVE] %d consecutive losses. "
                    "Trading suspended until %s.",
                    self.consecutive_losses,
                    self.kill_switch_until.isoformat(),
                )

    async def record_win(self) -> None:
        async with self._lock:
            self.consecutive_losses = 0

    async def reset_kill_switch(self) -> None:
        """Manual reset -- call from a management interface or REPL."""
        async with self._lock:
            self.consecutive_losses = 0
            self.kill_switch_until = None
            logger.info("Kill switch manually reset.")

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def seconds_to_expiry(self) -> float:
        """Returns remaining seconds until this market resolves (can be negative)."""
        now = datetime.now(tz=timezone.utc)
        # Ensure market_end_time is timezone-aware for comparison.
        end = self.market_end_time
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return (end - now).total_seconds()
