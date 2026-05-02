"""
main.py -- Entry point for the Polymarket BTC 5-min scalping bot.

Responsibilities:
  1. Configure logging.
  2. Initialise the CLOB client (skipped in DRY_RUN mode).
  3. Run the market discovery loop -- queries Gamma API every MARKET_POLL_INTERVAL
     seconds and spawns a trading session for each new 5-min BTC market found.
  4. For each active market, launch three concurrent asyncio Tasks:
       a. MarketDataConsumer  -- real-time order book via Market Channel WS
       b. UserEventConsumer   -- fill notifications via User Channel WS
       c. ScalpingStrategy    -- strategy loop driving the state machine
  5. Handle SIGINT/SIGTERM gracefully: cancel all open orders before exiting.

Concurrency model:
  All tasks share the asyncio event loop (single thread). No threading.
  State is protected by asyncio.Lock inside StrategyState.
  Tasks communicate only through StrategyState -- no shared queues or globals.

Integration note:
  To embed this in an "Early Bird" system, import `run_bot()` and call it
  with `asyncio.run(run_bot())` from your orchestrator, or schedule it as
  an asyncio Task alongside other strategies.
"""

import asyncio
import logging
import signal
import sys
from typing import Optional

import aiohttp

import config
from market_finder import find_active_btc_5min_markets
from market_data import MarketDataConsumer
from user_events import UserEventConsumer
from order_executor import DryRunOrderExecutor, LiveOrderExecutor, OrderExecutor
from state import StrategyState
from strategy import ScalpingStrategy

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")


def _market_expired(market: dict) -> bool:
    """Returns True if the market's end_time is in the past."""
    from datetime import datetime, timezone as _tz
    end = market["end_time"]
    if end.tzinfo is None:
        end = end.replace(tzinfo=_tz.utc)
    return datetime.now(tz=_tz.utc) >= end


# ---------------------------------------------------------------------------
# Per-market session container
# ---------------------------------------------------------------------------

class MarketSession:
    """
    Holds all objects and asyncio Tasks for one active market.
    Provides a clean shutdown path (cancel + await all tasks).
    """

    def __init__(
        self,
        market: dict,
        strategy: ScalpingStrategy,
        market_data: MarketDataConsumer,
        user_events: Optional[UserEventConsumer],
    ) -> None:
        self.market = market
        self.strategy = strategy
        self.market_data = market_data
        self.user_events = user_events
        self.tasks: list[asyncio.Task] = []

    def launch(self) -> None:
        """Create and schedule all concurrent tasks for this market."""
        self.tasks.append(
            asyncio.create_task(
                self.market_data.run(),
                name=f"book_{self.market['token_id'][:8]}",
            )
        )
        if self.user_events:
            self.tasks.append(
                asyncio.create_task(
                    self.user_events.run(),
                    name=f"user_{self.market['condition_id'][:8]}",
                )
            )
        self.tasks.append(
            asyncio.create_task(
                self.strategy.run(),
                name=f"strat_{self.market['token_id'][:8]}",
            )
        )

    async def shutdown(self) -> None:
        """Stop all consumers and cancel tasks."""
        self.strategy.stop()
        self.market_data.stop()
        if self.user_events:
            self.user_events.stop()

        for task in self.tasks:
            task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)
        logger.info(
            "Session for %s shut down.", self.market.get("slug", self.market["token_id"][:8])
        )


# ---------------------------------------------------------------------------
# CLOB client factory
# ---------------------------------------------------------------------------

def _build_clob_client():
    """
    Constructs and authenticates a py_clob_client_v2.ClobClient.
    Raises RuntimeError if credentials are missing.

    NOTE: Polymarket migrated to V2 on 2026-04-28. The new SDK package is
    `py_clob_client_v2`.  Key API changes vs the old SDK:
      - `ApiCreds` → imported from `py_clob_client_v2`
      - Credentials passed as `creds=` at construction time (no set_api_creds)
      - `create_or_derive_api_creds` → `create_or_derive_api_key`

    This bot uses a Polymarket proxy wallet setup:
      - PRIVATE_KEY = Magic wallet (signs orders)
      - FUNDER_ADDRESS = Polymarket proxy wallet (holds pUSD)
      - signature_type = 1 (POLY_PROXY) so the CLOB resolves the proxy relationship
    """
    from py_clob_client_v2 import ApiCreds, ClobClient
    from py_clob_client_v2.order_builder.builder import SignatureTypeV2

    # Proxy wallet: Magic wallet signs on behalf of the Polymarket proxy wallet.
    sig_type = SignatureTypeV2.POLY_PROXY if config.FUNDER_ADDRESS else SignatureTypeV2.EOA

    if not config.PRIVATE_KEY:
        raise RuntimeError("PRIVATE_KEY is required for live trading.")

    if config.API_KEY and config.API_SECRET and config.API_PASSPHRASE:
        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            creds=creds,
            signature_type=sig_type,
            funder=config.FUNDER_ADDRESS or None,
        )
        logger.info("CLOB client (V2) authenticated with existing L2 credentials.")
    else:
        # Step 1: L1-only client to derive API credentials.
        l1_client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            signature_type=sig_type,
            funder=config.FUNDER_ADDRESS or None,
        )
        creds = l1_client.create_or_derive_api_key()
        # Step 2: Fully-authenticated V2 client.
        client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            creds=creds,
            signature_type=sig_type,
            funder=config.FUNDER_ADDRESS or None,
        )
        logger.info(
            "L2 credentials derived from private key (V2). "
            "Save ALL THREE values in .env to avoid re-derivation:\n"
            "  API_KEY=%s\n"
            "  API_SECRET=%s\n"
            "  API_PASSPHRASE=%s",
            creds.api_key,
            creds.api_secret,
            creds.api_passphrase,
        )

    return client


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def _build_session(
    market: dict,
    executor: OrderExecutor,
    dry_run: bool,
) -> MarketSession:
    """
    Wires together all objects for one market and returns an unstarted session.
    """
    state = StrategyState(market_end_time=market["end_time"])
    strategy = ScalpingStrategy(market=market, state=state, executor=executor)

    # Wire the fill callback: both live and dry-run paths feed into this.
    # DryRunOrderExecutor fires fills internally; LiveOrderExecutor relies on
    # UserEventConsumer -> state.set_pending_fill() (already wired via state).
    if dry_run and isinstance(executor, DryRunOrderExecutor):
        executor.register_fill_callback(strategy.handle_fill)

    market_data = MarketDataConsumer(
        token_id=market["token_id"],
        state=state,
    )

    user_events: Optional[UserEventConsumer] = None
    if not dry_run:
        user_events = UserEventConsumer(
            condition_id=market["condition_id"],
            state=state,
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )

    return MarketSession(
        market=market,
        strategy=strategy,
        market_data=market_data,
        user_events=user_events,
    )


# ---------------------------------------------------------------------------
# Main bot loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    """
    Top-level coroutine. Runs the market discovery loop indefinitely.

    To integrate into an existing engine, import and await this coroutine:
        import asyncio
        from btc_scalper.main import run_bot
        asyncio.run(run_bot())
    """
    logger.info(
        "=== Polymarket BTC 5-min Scalping Bot ===  DRY_RUN=%s", config.DRY_RUN
    )

    # Build executor once (shared across sessions when in dry-run mode,
    # but each session needs its own because DryRunOrderExecutor holds a
    # fill callback per-strategy). We build one per session below.

    clob_client = None
    if not config.DRY_RUN:
        clob_client = _build_clob_client()

    active_sessions: dict[str, MarketSession] = {}  # keyed by token_id

    # Install shutdown handler so SIGINT/SIGTERM cleans up orders.
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received.")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # add_signal_handler is not available on Windows -- fall back to
            # KeyboardInterrupt handling at the bottom of this function.
            pass

    async with aiohttp.ClientSession() as http_session:
        while not shutdown_event.is_set():
            # -------------------------------------------------------
            # Market discovery
            # -------------------------------------------------------
            try:
                found_markets = await find_active_btc_5min_markets(http_session)
            except Exception as exc:
                logger.error("Market discovery failed: %s", exc)
                found_markets = []

            # Prune sessions for markets that have already expired.
            expired = [
                tid
                for tid, sess in active_sessions.items()
                if _market_expired(sess.market)
            ]
            for tid in expired:
                sess = active_sessions.pop(tid)
                await sess.shutdown()

            # Start sessions for newly discovered markets (up to MAX_CONCURRENT_MARKETS).
            slots_available = config.MAX_CONCURRENT_MARKETS - len(active_sessions)
            for market in found_markets:
                if slots_available <= 0:
                    break
                token_id = market["token_id"]
                if token_id in active_sessions:
                    continue  # already running

                from datetime import datetime, timezone as _tz
                end = market["end_time"]
                if end.tzinfo is None:
                    end = end.replace(tzinfo=_tz.utc)
                remaining_s = (end - datetime.now(tz=_tz.utc)).total_seconds()
                logger.info(
                    "Starting session for market: %s (ends in %.0fs)",
                    market["slug"],
                    remaining_s,
                )

                # Build a fresh executor per session (needed for fill callback wiring).
                if config.DRY_RUN:
                    executor: OrderExecutor = DryRunOrderExecutor()
                else:
                    executor = LiveOrderExecutor(clob_client)

                session = _build_session(market, executor, config.DRY_RUN)
                session.launch()
                active_sessions[token_id] = session
                slots_available -= 1

            # Wait until next discovery cycle or shutdown.
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=config.MARKET_POLL_INTERVAL,
                )
            except asyncio.TimeoutError:
                pass  # Normal -- just poll again.

    # -----------------------------------------------------------------------
    # Graceful shutdown
    # -----------------------------------------------------------------------
    logger.info("Shutting down %d active sessions...", len(active_sessions))
    for sess in active_sessions.values():
        await sess.shutdown()
    logger.info("Bot stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        # Fallback for Windows where signal handlers may not work.
        logger.info("Interrupted by user.")
