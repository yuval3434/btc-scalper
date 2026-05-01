"""
user_events.py -- Authenticated User Channel WebSocket consumer.

Subscribes to wss://ws-subscriptions-clob.polymarket.com/ws/user and
listens for `trade` and `order` events specific to our account.

Key responsibilities:
  1. Detect fill events (status == "MATCHED") and notify the strategy via
     state.set_pending_fill() so position management logic can proceed.
  2. Track order cancellation confirmations for auditing.
  3. Maintain the WebSocket connection with heartbeats and reconnect on drop.

Authentication: L2 credentials (apiKey / secret / passphrase) are embedded
in the subscription message. The server validates these before forwarding
account-specific events.

IMPORTANT: The User Channel delivers trade confirmations at the "MATCHED"
stage, meaning the order is matched on the off-chain orderbook but not yet
settled on-chain. For our latency-sensitive stop-loss logic this is
sufficient -- we do not need to wait for MINED/CONFIRMED.
"""

import asyncio
import json
import logging
import random

import websockets
import websockets.exceptions

import config
from state import StrategyState
from market_data import _backoff  # reuse the shared backoff utility

logger = logging.getLogger(__name__)


class UserEventConsumer:
    """
    Listens on the authenticated User Channel for fill and order events
    associated with a single condition_id (market).

    Usage:
        consumer = UserEventConsumer(condition_id, state, api_key, secret, passphrase)
        asyncio.create_task(consumer.run())
    """

    def __init__(
        self,
        condition_id: str,
        state: StrategyState,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
    ) -> None:
        self._condition_id = condition_id
        self._state = state
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._running = True

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Reconnect loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Outer reconnect loop with exponential backoff.
        Mirrors the pattern used in MarketDataConsumer.
        """
        attempt = 0
        while self._running:
            try:
                await self._connect_and_consume()
                attempt = 0
            except asyncio.CancelledError:
                logger.info("[UserEvents:%s] Task cancelled.", self._condition_id[:8])
                return
            except Exception as exc:
                if not self._running:
                    return
                delay = _backoff(attempt)
                logger.warning(
                    "[UserEvents:%s] Disconnected (%s). Reconnecting in %.1fs (attempt %d).",
                    self._condition_id[:8],
                    exc,
                    delay,
                    attempt + 1,
                )
                await asyncio.sleep(delay)
                attempt += 1

    # ------------------------------------------------------------------
    # Single WebSocket session
    # ------------------------------------------------------------------

    async def _connect_and_consume(self) -> None:
        logger.info(
            "[UserEvents:%s] Connecting to %s", self._condition_id[:8], config.USER_WS
        )

        async with websockets.connect(
            config.USER_WS,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
        ) as ws:
            # Authenticated subscription message.
            # The `markets` field is a list of condition_ids (not token_ids).
            sub_msg = json.dumps(
                {
                    "markets": [self._condition_id],
                    "type": "user",
                    "auth": {
                        "apiKey": self._api_key,
                        "secret": self._api_secret,
                        "passphrase": self._api_passphrase,
                    },
                }
            )
            await ws.send(sub_msg)
            logger.info(
                "[UserEvents:%s] Subscribed (key=...%s).",
                self._condition_id[:8],
                self._api_key[-4:] if self._api_key else "????",
            )

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

        if raw in ("PONG", ""):
            return

        try:
            messages = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(
                "[UserEvents:%s] Non-JSON frame: %s", self._condition_id[:8], raw[:80]
            )
            return

        if isinstance(messages, dict):
            messages = [messages]

        for msg in messages:
            event_type = msg.get("event_type") or msg.get("type", "")

            if event_type == "trade":
                await self._handle_trade(msg)
            elif event_type == "order":
                await self._handle_order(msg)

    # ------------------------------------------------------------------
    # Trade fill handler
    # ------------------------------------------------------------------

    async def _handle_trade(self, msg: dict) -> None:
        """
        Trade lifecycle: MATCHED -> MINED -> CONFIRMED (or FAILED).

        We act on MATCHED because:
          - It's the earliest confirmation available over WebSocket.
          - Stop-loss time sensitivity makes waiting for MINED impractical.
          - If the trade subsequently FAILS (extremely rare), we're in a
            mismatch state -- acceptable risk for a scalping strategy.
        """
        status = str(msg.get("status", "")).upper()
        if status != "MATCHED":
            return

        # The server may embed a list of fills; handle both formats.
        fills = msg.get("fills") or [msg]
        for fill in fills:
            order_id = str(fill.get("orderId") or fill.get("order_id") or "")
            fill_price = float(fill.get("price") or fill.get("matchPrice") or 0)
            fill_size = float(fill.get("size") or fill.get("originalSize") or 0)

            if not order_id or fill_price <= 0 or fill_size <= 0:
                logger.debug(
                    "[UserEvents:%s] Skipping malformed fill: %s",
                    self._condition_id[:8],
                    fill,
                )
                continue

            logger.info(
                "[UserEvents:%s] FILL -- order=%s price=%.4f size=%.2f",
                self._condition_id[:8],
                order_id,
                fill_price,
                fill_size,
            )
            # Post the fill into shared state; strategy loop will pick it up.
            await self._state.set_pending_fill(order_id, fill_price, fill_size)

    # ------------------------------------------------------------------
    # Order status handler
    # ------------------------------------------------------------------

    async def _handle_order(self, msg: dict) -> None:
        """
        Log order status transitions (placement, partial fill, cancellation).
        We don't drive strategy logic from here -- the strategy drives itself
        via state transitions and the pending_fill handshake.
        """
        order_id = msg.get("id") or msg.get("orderId", "")
        status = msg.get("status", "")
        logger.debug(
            "[UserEvents:%s] Order event -- id=%s status=%s",
            self._condition_id[:8],
            order_id,
            status,
        )
