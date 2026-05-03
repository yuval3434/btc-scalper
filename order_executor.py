"""
order_executor.py -- Abstract order execution interface with live and dry-run
implementations.

All strategy code calls OrderExecutor methods exclusively -- the strategy
never imports py_clob_client_v2 directly. This makes dry-run testing trivial
(swap one object at construction time) and keeps the strategy logic clean.

LiveOrderExecutor wraps py_clob_client_v2.ClobClient (Polymarket V2 API,
migrated 2026-04-28):
  - Limit orders use OrderType.GTC which maps to maker/post-only semantics
    on Polymarket. If the order would immediately cross the spread it is
    rejected -- desired behaviour, prevents taker fees.
  - Market (stop-loss) orders use OrderType.FAK (Fill-and-Kill) which
    executes immediately against available liquidity.

DryRunOrderExecutor:
  - Generates synthetic order IDs and logs all operations.
  - Simulates a fill notification 500 ms after a buy order is placed so
    the full downstream logic (take-profit placement, stop monitoring) can
    be exercised in dry-run sessions.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, Optional

import config

logger = logging.getLogger(__name__)

# Callback type: (order_id, fill_price, fill_size) -> None
FillCallback = Callable[[str, float, float], Awaitable[None]]


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _round_to_tick(price: float, tick_size: float) -> float:
    """
    Round price to the nearest valid tick increment.

    Uses integer arithmetic internally to avoid floating-point drift.
    e.g. round_to_tick(0.537, 0.01) -> 0.54
    """
    if tick_size <= 0:
        return round(price, 4)
    ticks = round(price / tick_size)
    return round(ticks * tick_size, 10)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class OrderExecutor(ABC):
    """
    Common interface for order operations.
    All methods are async to allow both live (network I/O) and simulated
    (asyncio.sleep) implementations to coexist.
    """

    @abstractmethod
    async def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
        user_usdc_balance: Optional[float] = None,
    ) -> str:
        """Submit a post-only GTC limit buy. Returns order_id."""

    @abstractmethod
    async def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
    ) -> str:
        """Submit a post-only GTC limit sell. Returns order_id."""

    @abstractmethod
    async def place_market_sell(
        self,
        token_id: str,
        size: float,
    ) -> str:
        """Submit an immediate FAK market sell (for stop-loss execution). Returns order_id."""

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True if cancellation was accepted."""


# ---------------------------------------------------------------------------
# Live implementation
# ---------------------------------------------------------------------------

class LiveOrderExecutor(OrderExecutor):
    """
    Wraps py_clob_client_v2.ClobClient for real order submission on Polymarket.

    The ClobClient instance must already be fully authenticated (L1 key +
    L2 creds passed at construction) before this executor is constructed.

    V2 API uses a single create_and_post_order() call instead of the old
    two-step create_order() + post_order() pattern.
    """

    def __init__(self, clob_client: "ClobClient") -> None:  # type: ignore[name-defined]
        self._client = clob_client

    async def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
        user_usdc_balance: Optional[float] = None,
    ) -> str:
        from decimal import ROUND_DOWN, Decimal

        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        snapped_price = _round_to_tick(price, tick_size)

        # For FAK (market) BUY orders, the V2 API expects the USDC amount to
        # spend, NOT a price+size pair.  This avoids the
        # "invalid amounts ... max 2 decimals / 4 decimals" error
        # that arises when price*size produces more than 2 d.p. in float math.
        usdc_amount = float(
            (Decimal(str(snapped_price)) * Decimal(str(size))).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
        )

        logger.info(
            "[LIVE] Market BUY  token=...%s  price=%.4f  usdc=%.2f",
            token_id[-6:], snapped_price, usdc_amount,
        )

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=usdc_amount,   # USDC to spend — API resolves share qty
            side=Side.BUY,
            order_type=OrderType.FAK,
            user_usdc_balance=user_usdc_balance or 0,
        )
        options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=False)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._client.create_and_post_market_order(
                order_args=order_args,
                options=options,
                order_type=OrderType.FAK,
            ),
        )
        order_id = resp.get("orderID") or resp.get("id") or str(resp)
        logger.info("[LIVE] BUY order submitted -- id=%s", order_id)
        return order_id

    async def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
    ) -> str:
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        snapped_price = _round_to_tick(price, tick_size)
        logger.info(
            "[LIVE] Limit SELL  token=...%s  price=%.4f  size=%.2f",
            token_id[-6:], snapped_price, size,
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=snapped_price,
            size=size,
            side=Side.SELL,
        )
        options = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=False)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._client.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=OrderType.GTC,
            ),
        )
        order_id = resp.get("orderID") or resp.get("id") or str(resp)
        logger.info("[LIVE] SELL order accepted -- id=%s", order_id)
        return order_id

    async def place_market_sell(
        self,
        token_id: str,
        size: float,
    ) -> str:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side

        logger.info("[LIVE] Market SELL (stop)  token=...%s  size=%.2f", token_id[-6:], size)

        # V2 market orders use MarketOrderArgs; size in shares (not USDC).
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=Side.SELL,
            order_type=OrderType.FAK,
        )
        # tick_size "0.01" is the standard for binary markets.
        options = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self._client.create_and_post_market_order(
                order_args=order_args,
                options=options,
                order_type=OrderType.FAK,
            ),
        )
        order_id = resp.get("orderID") or resp.get("id") or str(resp)
        logger.info("[LIVE] Market SELL submitted -- id=%s", order_id)
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        logger.info("[LIVE] Cancelling order id=%s", order_id)
        loop = asyncio.get_running_loop()

        # The V2 SDK cancel_order method expects an order object/dict, not a string.
        class CancelTarget(dict):
            def __init__(self, oid):
                super().__init__(orderID=oid, id=oid)
                self.orderID = oid
                self.id = oid

        try:
            await loop.run_in_executor(None, lambda: self._client.cancel_order(CancelTarget(order_id)))
            logger.info("[LIVE] Order %s cancelled.", order_id)
            return True
        except Exception as exc:
            logger.error("[LIVE] Cancel failed for %s: %s", order_id, exc)
            return False


# ---------------------------------------------------------------------------
# Dry-run implementation
# ---------------------------------------------------------------------------

class DryRunOrderExecutor(OrderExecutor):
    """
    Simulates order execution without touching the live API.

    Generates sequential fake order IDs and logs every operation.
    When a buy is placed, schedules a background task that fires after
    DRY_RUN_FILL_DELAY seconds to simulate a fill notification -- this
    exercises the full take-profit / stop-loss downstream path.
    """

    # Seconds to wait before simulating a buy fill.
    DRY_RUN_FILL_DELAY: float = 0.5

    def __init__(self, fill_callback: Optional[FillCallback] = None) -> None:
        """
        fill_callback: async callable(order_id, fill_price, fill_size) that
        the strategy registers so simulated fills reach the strategy loop.
        """
        self._fill_callback = fill_callback
        self._counter = 0

    def register_fill_callback(self, cb: FillCallback) -> None:
        """Allow late binding of the callback (before trading begins)."""
        self._fill_callback = cb

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"DRY_{prefix}_{self._counter:04d}"

    async def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
        user_usdc_balance: Optional[float] = None,
    ) -> str:
        snapped = _round_to_tick(price, tick_size)
        order_id = self._next_id("BUY")
        logger.debug(
            "[DRY RUN] Limit BUY  token=...%s  price=%.4f  size=%.2f  id=%s",
            token_id[-6:],
            snapped,
            size,
            order_id,
        )

        if self._fill_callback:
            asyncio.create_task(
                self._simulate_fill(order_id, snapped, size),
                name=f"dry_fill_{order_id}",
            )

        return order_id

    async def _simulate_fill(
        self, order_id: str, fill_price: float, fill_size: float
    ) -> None:
        await asyncio.sleep(self.DRY_RUN_FILL_DELAY)
        logger.debug(
            "[DRY RUN] Simulated FILL -- order=%s price=%.4f size=%.2f",
            order_id,
            fill_price,
            fill_size,
        )
        if self._fill_callback:
            await self._fill_callback(order_id, fill_price, fill_size)

    async def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: float,
        tick_size: float,
    ) -> str:
        snapped = _round_to_tick(price, tick_size)
        order_id = self._next_id("SELL")
        logger.debug(
            "[DRY RUN] Limit SELL (TP) token=...%s  price=%.4f  size=%.2f  id=%s",
            token_id[-6:],
            snapped,
            size,
            order_id,
        )
        return order_id

    async def place_market_sell(
        self,
        token_id: str,
        size: float,
    ) -> str:
        order_id = self._next_id("MKTSL")
        logger.debug(
            "[DRY RUN] Market SELL (stop) token=...%s  size=%.2f  id=%s",
            token_id[-6:],
            size,
            order_id,
        )
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        logger.debug("[DRY RUN] Cancel order id=%s", order_id)
        return True
