"""
strategy.py -- Core scalping strategy engine.

Implements the full state machine for one 5-minute BTC market:

  IDLE
   |  entry conditions satisfied (Golden Zone + imbalance)
   ▼
  BUY_ORDER_PLACED
   |  fill notification received from UserEventConsumer
   ▼
  POSITION_FILLED
   |  immediately places take-profit sell
   ▼
  SELL_ORDER_PLACED --- price stop OR time stop --► LIQUIDATING --► CLOSED (loss)
   |
   +---------- TP sell fill ------------------------------------------------ CLOSED (win)

The strategy loop runs every POLL_INTERVAL seconds and dispatches to a
handler method based on the current PositionState. The loop is intentionally
kept tight (100 ms) so stop conditions are checked frequently.

All order placement is delegated to OrderExecutor so this file is completely
agnostic of whether we're live or in dry-run mode.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import config
import notifier
import trade_log
from order_executor import OrderExecutor, _round_to_tick
from state import PositionState, Position, StrategyState

logger = logging.getLogger(__name__)

# Enable ANSI color codes on Windows terminals.
os.system("")

_GREEN  = "\033[92m"
_RED    = "\033[91m"
_RESET  = "\033[0m"

# How often the strategy loop ticks (seconds).
POLL_INTERVAL = 0.1

# How often to emit a "still watching, reason for no-entry" log (seconds).
DIAG_LOG_INTERVAL = 10.0


class _SessionTracker:
    """Accumulates PnL across all concurrent market sessions."""
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.total_pnl: float = 0.0
        self.trades: int = 0
        self.wins: int = 0

    async def record(self, pnl: float) -> tuple[float, int, int]:
        async with self._lock:
            self.total_pnl = round(self.total_pnl + pnl, 4)
            self.trades += 1
            if pnl > 0:
                self.wins += 1
            return self.total_pnl, self.trades, self.wins


# Singleton shared across all ScalpingStrategy instances in the process.
session = _SessionTracker()


class ScalpingStrategy:
    """
    Drives the trade lifecycle for a single Polymarket 5-min BTC market.

    Constructor args:
        market   -- dict from market_finder: {token_id, condition_id, end_time, tick_size, slug}
        state    -- shared StrategyState for this market
        executor -- OrderExecutor (live or dry-run)
    """

    def __init__(
        self,
        market: dict,
        state: StrategyState,
        executor: OrderExecutor,
    ) -> None:
        self._market = market
        self._token_id: str = market["token_id"]
        self._tick_size: float = market["tick_size"]
        self._slug: str = market.get("slug", self._token_id[:12])

        self._state = state
        self._executor = executor
        self._running = True

        # Set of order IDs we have placed -- used to match incoming fills.
        self._our_buy_order_id: str = ""

        # Tracks the last time we emitted a "no-entry reason" diagnostic log.
        self._last_diag_at: float = 0.0

        logger.info("[Strategy:%s] Initialised. Tick=%.4f", self._slug, self._tick_size)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Fill callback (called by DryRunOrderExecutor or wired from main.py
    # to state.set_pending_fill so UserEventConsumer routes fills here)
    # ------------------------------------------------------------------

    async def handle_fill(
        self, order_id: str, fill_price: float, fill_size: float
    ) -> None:
        """
        Entry point for fill notifications.
        Called by DryRunOrderExecutor directly, or by main.py after
        UserEventConsumer has updated state.pending_fill.
        """
        await self._state.set_pending_fill(order_id, fill_price, fill_size)

    # ------------------------------------------------------------------
    # Main strategy loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Runs until the market expires, stop() is called, or the event loop
        is cancelled. Each tick dispatches to the appropriate phase handler.
        """
        logger.info("[Strategy:%s] Loop started.", self._slug)

        while self._running:
            # Exit once the market has resolved.
            if self._state.seconds_to_expiry() <= 0:
                logger.info("[Strategy:%s] Market expired -- stopping loop.", self._slug)
                break

            phase = await self._state.get_phase()

            try:
                if phase == PositionState.IDLE:
                    await self._evaluate_entry()

                elif phase == PositionState.BUY_ORDER_PLACED:
                    await self._monitor_buy_order()

                elif phase == PositionState.POSITION_FILLED:
                    await self._place_take_profit()

                elif phase == PositionState.SELL_ORDER_PLACED:
                    await self._monitor_position()

                elif phase in (PositionState.LIQUIDATING, PositionState.CLOSED):
                    # Terminal states -- nothing to do, loop will exit next tick
                    # once we handle any in-flight async operations.
                    pass

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[Strategy:%s] Unhandled error in phase %s: %s",
                    self._slug,
                    phase.value,
                    exc,
                    exc_info=True,
                )

            await asyncio.sleep(POLL_INTERVAL)

        logger.info("[Strategy:%s] Loop exited.", self._slug)

    # ------------------------------------------------------------------
    # Phase: IDLE -- evaluate entry conditions
    # ------------------------------------------------------------------

    async def _evaluate_entry(self) -> None:
        """
        Rules (ALL must be satisfied):
          1. Kill-switch not active.
          2. Enough time remains to trade (> SL_TIME_SECONDS + buffer).
          3. Order book is populated.
          4. Mid-price in the Golden Zone [0.35, 0.65].
          5. Top-5 order book imbalance ratio ≥ 1.20 (bid-heavy).
        """
        import time
        now = time.monotonic()
        diag = (now - self._last_diag_at) >= DIAG_LOG_INTERVAL  # rate-limit diagnostic logs

        # 1. Kill-switch guard.
        if await self._state.is_kill_switch_active():
            if diag:
                logger.warning("[Strategy:%s] SKIP -- kill switch active.", self._slug)
                self._last_diag_at = now
            return

        # 2. Minimum time buffer.
        remaining = self._state.seconds_to_expiry()
        min_remaining = config.SL_TIME_SECONDS + 10
        if remaining <= min_remaining:
            if diag:
                logger.debug(
                    "[Strategy:%s] SKIP -- too close to expiry (%.0fs <= %.0fs).",
                    self._slug, remaining, min_remaining,
                )
                self._last_diag_at = now
            return

        # 3. Get current order book.
        bids, asks = await self._state.get_order_book()
        if not bids or not asks:
            if diag:
                logger.debug(
                    "[Strategy:%s] SKIP -- order book empty (waiting for WebSocket snapshot).",
                    self._slug,
                )
                self._last_diag_at = now
            return

        best_bid = max(bids)
        best_ask = min(asks)
        mid_price = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

        # 4. Golden Zone filter.
        if not (config.MID_LOW <= mid_price <= config.MID_HIGH):
            if diag:
                logger.debug(
                    "[Strategy:%s] SKIP -- mid %.4f outside zone [%.2f,%.2f]  "
                    "bid=%.4f ask=%.4f remaining=%.0fs",
                    self._slug, mid_price,
                    config.MID_LOW, config.MID_HIGH,
                    best_bid, best_ask, remaining,
                )
                self._last_diag_at = now
            return

        # 5. Imbalance filter.
        imbalance_ratio = _compute_imbalance(bids, asks, config.IMBALANCE_LEVELS)
        if imbalance_ratio < config.IMBALANCE_MIN_RATIO:
            if diag:
                logger.debug(
                    "[Strategy:%s] SKIP -- imbalance %.3f < %.2f  "
                    "mid=%.4f bid=%.4f ask=%.4f  remaining=%.0fs",
                    self._slug, imbalance_ratio, config.IMBALANCE_MIN_RATIO,
                    mid_price, best_bid, best_ask, remaining,
                )
                self._last_diag_at = now
            return

        # 6. Compute entry price.
        # Enter at best_ask (aggressive / taker) so we fill immediately in the
        # direction of the imbalance signal, rather than waiting for an adverse
        # dip that contradicts the bullish signal.
        buy_price = _round_to_tick(best_ask, self._tick_size)

        # Guard: price must stay within valid [0, 1] range.
        if buy_price <= 0 or buy_price >= 1.0:
            logger.warning(
                "[Strategy:%s] SKIP -- computed buy price %.4f out of range  mid=%.4f",
                self._slug, buy_price, mid_price,
            )
            return

        # 7. Size: how many shares can we buy for POSITION_SIZE_USDC at this price?
        shares = round(config.POSITION_SIZE_USDC / buy_price, 2)
        if shares <= 0:
            return

        logger.info(
            "[Strategy:%s] Entry signal -- mid=%.4f imbalance=%.3f "
            "buy_price=%.4f shares=%.2f remaining=%.0fs",
            self._slug,
            mid_price,
            imbalance_ratio,
            buy_price,
            shares,
            remaining,
        )

        # 8. Place the limit buy order.
        order_id = await self._executor.place_limit_buy(
            self._token_id, buy_price, shares, self._tick_size
        )

        # 9. Record in state and transition.
        async with self._state._lock:
            self._state.position = Position(
                token_id=self._token_id,
                tick_size=self._tick_size,
                buy_order_id=order_id,
                imbalance_at_entry=imbalance_ratio,
                mid_at_entry=mid_price,
                secs_to_expiry_at_entry=remaining,
            )
            self._state.phase = PositionState.BUY_ORDER_PLACED

        self._our_buy_order_id = order_id
        logger.info(
            "[Strategy:%s] BUY order placed -- id=%s price=%.4f",
            self._slug,
            order_id,
            buy_price,
        )

    # ------------------------------------------------------------------
    # Phase: BUY_ORDER_PLACED -- wait for fill
    # ------------------------------------------------------------------

    async def _monitor_buy_order(self) -> None:
        """
        Polls the pending_fill slot in state. If the fill matches our buy
        order, record it and move to POSITION_FILLED.

        Also cancels the buy order if entry conditions are no longer valid
        (market moved out of Golden Zone or imbalance flipped).
        """
        # Check for a fill notification.
        fill = await self._state.consume_pending_fill()
        if fill:
            order_id, fill_price, fill_size = fill
            if order_id == self._our_buy_order_id:
                async with self._state._lock:
                    pos = self._state.position
                    if pos:
                        pos.fill_price = fill_price
                        pos.fill_size = fill_size
                        pos.entry_time = datetime.now(tz=timezone.utc)
                    self._state.phase = PositionState.POSITION_FILLED

                logger.info(
                    "[Strategy:%s] BUY FILLED -- price=%.4f size=%.2f",
                    self._slug,
                    fill_price,
                    fill_size,
                )
                return

        # Safety: cancel stale buy if market conditions have deteriorated.
        bids, asks = await self._state.get_order_book()
        if bids and asks:
            best_bid = max(bids)
            best_ask = min(asks)
            mid = (best_bid + best_ask) / 2.0
            imbalance = _compute_imbalance(bids, asks, config.IMBALANCE_LEVELS)

            if not (config.MID_LOW <= mid <= config.MID_HIGH) or imbalance < 1.0:
                logger.debug(
                    "[Strategy:%s] Conditions deteriorated (mid=%.4f imbalance=%.3f). "
                    "Cancelling pending buy %s.",
                    self._slug,
                    mid,
                    imbalance,
                    self._our_buy_order_id,
                )
                await self._executor.cancel_order(self._our_buy_order_id)
                async with self._state._lock:
                    self._state.position = None
                    self._state.phase = PositionState.IDLE
                self._our_buy_order_id = ""

    # ------------------------------------------------------------------
    # Phase: POSITION_FILLED -- immediately place take-profit sell
    # ------------------------------------------------------------------

    async def _place_take_profit(self) -> None:
        """Place a post-only limit sell exactly TP_OFFSET above fill price."""
        async with self._state._lock:
            pos = self._state.position

        if not pos or pos.fill_price <= 0:
            logger.error("[Strategy:%s] Tried to place TP but position is invalid.", self._slug)
            await self._state.transition(PositionState.IDLE)
            return

        tp_price = _round_to_tick(pos.fill_price + config.TP_OFFSET, self._tick_size)
        tp_price = min(tp_price, 0.99)  # Hard cap: can't sell above 0.99 on binary markets.

        logger.debug(
            "[Strategy:%s] Placing TP sell -- fill=%.4f tp=%.4f size=%.2f",
            self._slug,
            pos.fill_price,
            tp_price,
            pos.fill_size,
        )

        sell_id = await self._executor.place_limit_sell(
            self._token_id, tp_price, pos.fill_size, self._tick_size
        )

        async with self._state._lock:
            self._state.position.sell_order_id = sell_id
            self._state.position.tp_price = tp_price
            self._state.phase = PositionState.SELL_ORDER_PLACED

        logger.debug("[Strategy:%s] TP sell placed -- id=%s", self._slug, sell_id)

        remaining = self._state.seconds_to_expiry()
        sl_price = _round_to_tick(pos.fill_price - config.SL_PRICE_OFFSET, self._tick_size)
        asyncio.create_task(notifier.send(
            f"*BET PLACED* -- {self._slug}\n"
            f"Buy: `{pos.fill_price:.4f}` | Shares: `{pos.fill_size:.2f}`\n"
            f"TP: `{tp_price:.4f}` | SL: `{sl_price:.4f}`\n"
            f"Size: `${config.POSITION_SIZE_USDC:.2f}` | Expires in: `{remaining:.0f}s`"
        ))

    # ------------------------------------------------------------------
    # Phase: SELL_ORDER_PLACED -- monitor stops and TP fill
    # ------------------------------------------------------------------

    async def _monitor_position(self) -> None:
        """
        On every tick while SELL_ORDER_PLACED:

        1. Check if the TP sell has been filled (pending_fill contains our sell id).
        2. Check price-based stop: best_bid ≤ fill_price - SL_PRICE_OFFSET.
        3. Check time-based stop: seconds_to_expiry ≤ SL_TIME_SECONDS.

        Whichever triggers first calls _execute_stop(). If TP fills first,
        record a win and close cleanly.
        """
        async with self._state._lock:
            pos = self._state.position

        if not pos:
            await self._state.transition(PositionState.IDLE)
            return

        # 1. Check for TP fill.
        fill = await self._state.consume_pending_fill()
        if fill:
            order_id, fill_price, fill_size = fill
            if order_id == pos.sell_order_id:
                pnl = (fill_price - pos.fill_price) * fill_size
                session_pnl, n_trades, n_wins = await session.record(pnl)
                print(
                    f"{_GREEN}[Strategy:{self._slug}] WIN  "
                    f"sell={fill_price:.4f} buy={pos.fill_price:.4f} "
                    f"pnl=+${pnl:.4f}  |  "
                    f"session: ${session_pnl:+.4f} ({n_wins}/{n_trades} wins){_RESET}",
                    flush=True,
                )
                asyncio.create_task(notifier.send(
                    f"*WIN* -- {self._slug}\n"
                    f"Buy: `{pos.fill_price:.4f}` -> TP: `{fill_price:.4f}`\n"
                    f"PnL: `+${pnl:.4f}`\n"
                    f"Session: `${session_pnl:+.4f}` ({n_wins}/{n_trades} wins)"
                ))
                trade_log.record(
                    market_slug=self._slug, token_id=self._token_id,
                    fill_price=pos.fill_price, shares=pos.fill_size,
                    position_usdc=config.POSITION_SIZE_USDC,
                    tp_price=pos.tp_price,
                    sl_price=round(pos.fill_price - config.SL_PRICE_OFFSET, 4),
                    exit_price=fill_price, exit_reason="tp", pnl=pnl,
                    session_pnl=session_pnl, entry_time=pos.entry_time,
                    dry_run=config.DRY_RUN,
                    imbalance_at_entry=pos.imbalance_at_entry,
                    mid_at_entry=pos.mid_at_entry,
                    secs_to_expiry_at_entry=pos.secs_to_expiry_at_entry,
                )
                async with self._state._lock:
                    if self._state.position:
                        self._state.position.realized_pnl = pnl
                    self._state.phase = PositionState.CLOSED

                await self._state.record_win()
                return
            else:
                # Fill is for something else -- put it back (shouldn't happen,
                # but guard against race conditions with multiple markets).
                await self._state.set_pending_fill(*fill)

        # 2. Price-based TP detection (works in dry-run and as a live backup when
        #    the WebSocket fill notification arrives late or is missed).
        #    If the market's best_bid has crossed our limit sell price, the order
        #    should already be matched.
        bids, _ = await self._state.get_order_book()
        if bids and pos.tp_price > 0 and max(bids) >= pos.tp_price:
            tp_fill_price = pos.tp_price
            pnl = (tp_fill_price - pos.fill_price) * pos.fill_size
            session_pnl, n_trades, n_wins = await session.record(pnl)
            print(
                f"{_GREEN}[Strategy:{self._slug}] WIN (TP price crossed)  "
                f"sell={tp_fill_price:.4f} buy={pos.fill_price:.4f} "
                f"pnl=+${pnl:.4f}  |  "
                f"session: ${session_pnl:+.4f} ({n_wins}/{n_trades} wins){_RESET}",
                flush=True,
            )
            asyncio.create_task(notifier.send(
                f"*WIN* -- {self._slug}\n"
                f"Buy: `{pos.fill_price:.4f}` -> TP: `{tp_fill_price:.4f}`\n"
                f"PnL: `+${pnl:.4f}`\n"
                f"Session: `${session_pnl:+.4f}` ({n_wins}/{n_trades} wins)"
            ))
            trade_log.record(
                market_slug=self._slug, token_id=self._token_id,
                fill_price=pos.fill_price, shares=pos.fill_size,
                position_usdc=config.POSITION_SIZE_USDC,
                tp_price=pos.tp_price,
                sl_price=round(pos.fill_price - config.SL_PRICE_OFFSET, 4),
                exit_price=tp_fill_price, exit_reason="tp", pnl=pnl,
                session_pnl=session_pnl, entry_time=pos.entry_time,
                dry_run=config.DRY_RUN,
                imbalance_at_entry=pos.imbalance_at_entry,
                mid_at_entry=pos.mid_at_entry,
                secs_to_expiry_at_entry=pos.secs_to_expiry_at_entry,
            )
            async with self._state._lock:
                if self._state.position:
                    self._state.position.realized_pnl = pnl
                self._state.phase = PositionState.CLOSED
            await self._state.record_win()
            return

        # 3. Price-based stop-loss.
        if bids:
            best_bid = max(bids)
            sl_trigger = pos.fill_price - config.SL_PRICE_OFFSET
            if best_bid <= sl_trigger:
                logger.warning(
                    "[Strategy:%s] PRICE STOP triggered -- best_bid=%.4f sl_level=%.4f",
                    self._slug,
                    best_bid,
                    sl_trigger,
                )
                await self._execute_stop("price", pos)
                return

        # 4. Time-based stop-loss.
        remaining = self._state.seconds_to_expiry()
        if remaining <= config.SL_TIME_SECONDS:
            logger.warning(
                "[Strategy:%s] TIME STOP triggered -- %.1fs remaining",
                self._slug,
                remaining,
            )
            await self._execute_stop("time", pos)
            return

    # ------------------------------------------------------------------
    # Stop-loss execution (shared path for price and time triggers)
    # ------------------------------------------------------------------

    async def _execute_stop(self, reason: str, pos: Position) -> None:
        """
        1. Transition to LIQUIDATING to prevent re-entry.
        2. Cancel the open TP sell order.
        3. Place a market sell for the full position size.
        4. Record the loss and check the kill switch.
        5. Transition to CLOSED.
        """
        await self._state.transition(PositionState.LIQUIDATING)

        # Cancel open take-profit sell.
        if pos.sell_order_id:
            logger.info(
                "[Strategy:%s] Cancelling TP sell %s before stop.", self._slug, pos.sell_order_id
            )
            await self._executor.cancel_order(pos.sell_order_id)

        # Market sell to exit immediately.
        stop_id = await self._executor.place_market_sell(
            self._token_id, pos.fill_size
        )

        # Estimate loss for logging (market sell gets best available bid).
        bids, _ = await self._state.get_order_book()
        est_exit_price = max(bids) if bids else pos.fill_price - config.SL_PRICE_OFFSET
        est_pnl = (est_exit_price - pos.fill_price) * pos.fill_size

        session_pnl, n_trades, n_wins = await session.record(est_pnl)
        print(
            f"{_RED}[Strategy:{self._slug}] LOSS ({reason})  "
            f"exit={est_exit_price:.4f} buy={pos.fill_price:.4f} "
            f"pnl=${est_pnl:.4f}  |  "
            f"session: ${session_pnl:+.4f} ({n_wins}/{n_trades} wins){_RESET}",
            flush=True,
        )
        asyncio.create_task(notifier.send(
            f"*LOSS ({reason})* -- {self._slug}\n"
            f"Buy: `{pos.fill_price:.4f}` -> Exit: `{est_exit_price:.4f}`\n"
            f"PnL: `${est_pnl:.4f}`\n"
            f"Session: `${session_pnl:+.4f}` ({n_wins}/{n_trades} wins)"
        ))
        trade_log.record(
            market_slug=self._slug, token_id=self._token_id,
            fill_price=pos.fill_price, shares=pos.fill_size,
            position_usdc=config.POSITION_SIZE_USDC,
            tp_price=pos.tp_price,
            sl_price=round(pos.fill_price - config.SL_PRICE_OFFSET, 4),
            exit_price=est_exit_price, exit_reason=reason, pnl=est_pnl,
            session_pnl=session_pnl, entry_time=pos.entry_time,
            dry_run=config.DRY_RUN,
            imbalance_at_entry=pos.imbalance_at_entry,
            mid_at_entry=pos.mid_at_entry,
            secs_to_expiry_at_entry=pos.secs_to_expiry_at_entry,
        )

        async with self._state._lock:
            if self._state.position:
                self._state.position.realized_pnl = est_pnl
            self._state.phase = PositionState.CLOSED

        await self._state.record_loss()


# ---------------------------------------------------------------------------
# Utility: order book imbalance
# ---------------------------------------------------------------------------

def _compute_imbalance(
    bids: dict[float, float],
    asks: dict[float, float],
    levels: int = 5,
) -> float:
    """
    Returns bid_volume / ask_volume for the top `levels` price levels.

    A ratio > 1.0 means more buying pressure; > 1.20 is required to enter.
    Returns inf when ask side is empty (extreme bid dominance).
    """
    top_bids = sorted(bids.items(), reverse=True)[:levels]
    top_asks = sorted(asks.items())[:levels]

    bid_vol = sum(size for _, size in top_bids)
    ask_vol = sum(size for _, size in top_asks)

    if ask_vol == 0:
        return float("inf")
    return bid_vol / ask_vol
