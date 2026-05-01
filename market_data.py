"""
market_data.py -- Real-time order book consumer via Polymarket Market Channel.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and
subscribes to `book` + `price_change` events for a single token_id.

The resulting bids/asks dicts are written to StrategyState under a lock
so the strategy loop always reads a consistent snapshot.

WebSocket message contract (Polymarket Market Channel):
  • "book"         -- full order book snapshot after a trade
  • "price_change" -- incremental level update (size == "0" means remove level)
  • PONG           -- heartbeat response from server

Reconnection strategy:
  Exponential backoff with per-attempt jitter. Each reconnect resets the
  order book to empty so stale data is never used -- the first `book` event
  after reconnect repopulates it.
"""

import asyncio
import json
import logging
import random
from typing import Callable

import websockets
import websockets.exceptions

import config
from state import StrategyState

logger = logging.getLogger(__name__)


class MarketDataConsumer:
    """
    Maintains a live, incrementally-updated order book for one token.

    Usage:
        consumer = MarketDataConsumer(token_id, state)
        asyncio.create_task(consumer.run())
    """

    def __init__(self, token_id: str, state: StrategyState) -> None:
        self._token_id = token_id
        self._state = state
        self._running = True
        self._last_logged_bid: float = 0.0
        self._last_logged_ask: float = 0.0

        # Local order book (bids: price -> size, asks: price -> size).
        # These are rebuilt on every `book` event and patched on `price_change`.
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

        self._raw_log_remaining = 0  # set > 0 temporarily to diagnose WS format issues

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Main reconnect loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Outer reconnect loop. Runs forever until stop() is called.
        On each disconnect, waits an exponentially growing delay (with jitter)
        before reconnecting, then resets local state so a fresh snapshot is
        received.
        """
        attempt = 0
        while self._running:
            try:
                await self._connect_and_consume()
                attempt = 0  # successful session -- reset backoff
            except asyncio.CancelledError:
                logger.info("[MarketData:%s] Task cancelled.", self._token_id[:8])
                return
            except Exception as exc:
                if not self._running:
                    return
                delay = _backoff(attempt)
                logger.warning(
                    "[MarketData:%s] Disconnected (%s). Reconnecting in %.1fs (attempt %d).",
                    self._token_id[:8],
                    exc,
                    delay,
                    attempt + 1,
                )
                # Clear stale order book so the strategy doesn't act on old data.
                self._bids = {}
                self._asks = {}
                await self._state.update_order_book({}, {})
                await asyncio.sleep(delay)
                attempt += 1

    # ------------------------------------------------------------------
    # Single WebSocket session
    # ------------------------------------------------------------------

    async def _connect_and_consume(self) -> None:
        """
        Opens one WebSocket connection, sends the subscription message,
        then processes inbound messages until the connection closes.
        A parallel heartbeat task keeps the connection alive.
        """
        logger.info("[MarketData:%s] Connecting to %s", self._token_id[:8], config.MARKET_WS)

        async with websockets.connect(
            config.MARKET_WS,
            ping_interval=None,   # we send PING manually to control timing
            ping_timeout=None,
            close_timeout=5,
        ) as ws:
            # Send subscription request.
            sub_msg = json.dumps(
                {
                    "assets_ids": [self._token_id],
                    "type": "market",
                    "custom_feature_enabled": True,   # enables market_resolved events
                }
            )
            await ws.send(sub_msg)
            logger.info("[MarketData:%s] Subscribed to order book.", self._token_id[:8])

            # Heartbeat runs concurrently for the lifetime of this connection.
            hb_task = asyncio.create_task(self._heartbeat_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_message(raw)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Sends a PING frame every WS_HEARTBEAT_INTERVAL seconds.
        Polymarket closes the connection if no heartbeat is received for ~15 s.
        """
        while True:
            await asyncio.sleep(config.WS_HEARTBEAT_INTERVAL)
            try:
                await ws.ping()
            except websockets.exceptions.ConnectionClosed:
                return

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode()

        # Server heartbeat response -- no action needed.
        if raw in ("PONG", ""):
            return

        # Log first N raw frames verbatim so format issues are immediately visible.
        if self._raw_log_remaining > 0:
            logger.info(
                "[MarketData:%s] RAW frame #%d: %s",
                self._token_id[:8],
                6 - self._raw_log_remaining,
                raw[:500],
            )
            self._raw_log_remaining -= 1

        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.info("[MarketData:%s] Non-JSON frame: %r", self._token_id[:8], raw[:200])
            return

        # The server may batch multiple events in one frame (list) or send one dict.
        if isinstance(messages, dict):
            messages = [messages]

        for msg in messages:
            event_type = msg.get("event_type") or msg.get("type", "")

            # Book snapshot: identified by presence of "bids"/"asks" keys.
            # Polymarket does NOT set event_type on the initial snapshot.
            if "bids" in msg and "asks" in msg:
                await self._handle_book_snapshot(msg)
            elif event_type == "price_change" or "price_changes" in msg:
                await self._handle_price_change(msg)
            elif event_type:
                logger.debug(
                    "[MarketData:%s] Unhandled event_type=%r keys=%s",
                    self._token_id[:8],
                    event_type,
                    list(msg.keys())[:10],
                )

    # ------------------------------------------------------------------
    # Order book maintenance
    # ------------------------------------------------------------------

    async def _handle_book_snapshot(self, msg: dict) -> None:
        """
        Full order book snapshot -- replaces local state entirely.
        Expected format:
            { "event_type": "book",
              "asset_id": "...",
              "bids": [{"price": "0.50", "size": "100"}, ...],
              "asks": [{"price": "0.51", "size": "80"}, ...] }
        """
        self._bids = {
            float(level["price"]): float(level["size"])
            for level in msg.get("bids", [])
            if float(level.get("size", 0)) > 0
        }
        self._asks = {
            float(level["price"]): float(level["size"])
            for level in msg.get("asks", [])
            if float(level.get("size", 0)) > 0
        }
        await self._state.update_order_book(dict(self._bids), dict(self._asks))

        best_bid = max(self._bids) if self._bids else 0.0
        best_ask = min(self._asks) if self._asks else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0

        # Log only on first snapshot so we know WS data is arriving.
        if self._last_logged_bid == 0.0:
            logger.info(
                "[MarketData:%s] Book ready  best_bid=%.4f best_ask=%.4f mid=%.4f",
                self._token_id[:8], best_bid, best_ask, mid,
            )
            self._last_logged_bid = best_bid
            self._last_logged_ask = best_ask
        else:
            logger.debug(
                "[MarketData:%s] Book update  bid=%.4f ask=%.4f mid=%.4f",
                self._token_id[:8], best_bid, best_ask, mid,
            )

    async def _handle_price_change(self, msg: dict) -> None:
        """
        Incremental order book update from Polymarket.

        Actual wire format (confirmed from live data):
            {
              "market": "0x...",
              "price_changes": [
                { "asset_id": "<token_id>", "price": "0.50", "size": "100",
                  "side": "BUY", "best_bid": "0.50", "best_ask": "0.51" },
                ...
              ],
              "event_type": "price_change",
              "timestamp": "..."
            }

        Both tokens (Yes + No) appear in the same message; we filter to ours.
        side="BUY" -> bid level change; side="SELL" -> ask level change.
        size="0" -> remove that price level.
        """
        changes = (
            msg.get("price_changes")           # actual Polymarket key
            or msg.get("changes")              # fallback / older format
            or (([msg]) if "price" in msg and "side" in msg else [])  # flat format
        )

        changed = False
        for change in changes:
            # Skip changes that belong to the paired (No/Down) token.
            asset_id = change.get("asset_id", "")
            if asset_id and asset_id != self._token_id:
                continue

            try:
                side = str(change.get("side", "")).upper()
                price = float(change["price"])
                size = float(change["size"])
            except (KeyError, ValueError, TypeError):
                continue

            book = self._bids if side == "BUY" else self._asks

            if size == 0:
                book.pop(price, None)
            else:
                book[price] = size
            changed = True

        if changed:
            await self._state.update_order_book(dict(self._bids), dict(self._asks))


# ---------------------------------------------------------------------------
# Shared utility
# ---------------------------------------------------------------------------

def _backoff(attempt: int) -> float:
    """
    Exponential backoff with 0–25 % random jitter to prevent thundering herd.
    Returns delay in seconds, capped at WS_RECONNECT_MAX.
    """
    base = min(
        config.WS_RECONNECT_INITIAL * (2 ** attempt),
        config.WS_RECONNECT_MAX,
    )
    return base + random.uniform(0, base * 0.25)
