"""
e2e_test_v2.py -- End-to-End Test for Polymarket V2 API.
Run this script to verify fee handling, WS stability, and order placement.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp
from py_clob_client_v2 import (
    MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions, Side
)
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

import config
import wrap_usdce
from main import _build_clob_client
from market_data import MarketDataConsumer
from state import StrategyState

logger = logging.getLogger("e2e_test")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 1. Selected one of the fee-enabled condition IDs
CONDITION_ID = "0xaf5e903876ad42de97e1cf02c2ef8484df69bcfc5541b96a400116557d1e504e"

async def run_e2e() -> None:
    logger.info("=== Polymarket V2 E2E Test ===")
    
    # Step 1: Wrap USDC.e to pUSD via Collateral Onramp
    logger.info("\n--- Step 1: Wrap USDC.e to pUSD ---")
    if config.WRAP_ON_START:
        wrap_usdce.wrap_usdce_if_needed()
    else:
        logger.info("WRAP_ON_START is disabled. Skipping wrap (assuming pUSD is already funded).")

    # Authenticate client
    client = _build_clob_client()
    
    # Resolve Condition ID to Token ID via Gamma API
    logger.info("\n--- Resolving Condition ID to Token ID ---")
    async with aiohttp.ClientSession() as session:
        gamma_url = f"{config.GAMMA_API}/markets?condition_ids={CONDITION_ID}"
        async with session.get(gamma_url) as resp:
            markets = await resp.json()
            if not markets:
                logger.error("Market not found in Gamma API. Exiting.")
                return
            market = markets[0]
            tokens = json.loads(market["clobTokenIds"])
            token_id = tokens[0]  # Resolving to Yes Token
            logger.info(f"Resolved Token ID: {token_id}")

    # Step 2: Fetch market info
    logger.info("\n--- Step 2: Fetch Market Info ---")
    info = client.get_clob_market_info(CONDITION_ID)
    logger.info(f"Market Info: {info}")

    # Step 3: Place limit order outside spread
    logger.info("\n--- Step 3: Place Limit Order (Outside Spread) ---")
    price = 0.01  # Safe out-of-the-money price to prevent fill
    size = 10.0
    options = PartialCreateOrderOptions(tick_size="0.01")
    
    args = OrderArgs(price=price, size=size, side=Side.BUY, token_id=token_id)
    order = client.create_and_post_order(args, options=options, order_type=OrderType.GTC)
    logger.info(f"V2 Order Payload Accepted! Response: {order}")
    
    order_id = order.get("orderID") or order.get("id")
    
    # Step 4: Cancel the order
    logger.info("\n--- Step 4: Cancel Limit Order ---")
    if order_id:
        # The V2 SDK cancel_order method expects an order object/dict, not a string.
        class CancelTarget(dict):
            def __init__(self, oid):
                super().__init__(orderID=oid, id=oid)
                self.orderID = oid
                self.id = oid
        cancel_resp = client.cancel_order(CancelTarget(order_id))
        logger.info(f"Cancel Response: {cancel_resp}")
    else:
        logger.warning("Could not extract order_id to cancel!")

    # Step 5: Place a tiny GTC order near spread
    logger.info("\n--- Step 5: Place Tiny GTC Order ---")
    book = client.get_order_book(token_id)
    best_bid = float(book.bids[0].price) if book.bids else 0.50
    
    args2 = OrderArgs(price=best_bid, size=5.0, side=Side.BUY, token_id=token_id)
    order2 = client.create_and_post_order(args2, options=options, order_type=OrderType.GTC)
    logger.info(f"Fillable Maker Order: {order2}")
    
    # Step 6: Verify builder attribution
    logger.info("\n--- Step 6: Builder Attribution ---")
    if config.POLY_BUILDER_CODE:
        logger.info(f"Builder code '{config.POLY_BUILDER_CODE}' used. Verify placement at https://builders.polymarket.com")
    else:
        logger.info("No POLY_BUILDER_CODE set.")

    # Step 7: Stream WebSocket for 30s
    logger.info("\n--- Step 7: Stream WebSocket for 30s ---")
    state = StrategyState(datetime.now(timezone.utc))
    consumer = MarketDataConsumer(token_id, state)
    
    ws_task = asyncio.create_task(consumer.run())
    await asyncio.sleep(30)
    consumer.stop()
    await ws_task
    logger.info("WebSocket streaming completed gracefully without exceptions.")
    
    # Step 8: Market order with fee check
    logger.info("\n--- Step 8: Market Order (FAK) with userUSDCBalance ---")
    bal_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    bal_info = client.get_balance_allowance(params=bal_params)
    user_balance = float(bal_info["balance"]) / 1e6
    logger.info(f"Available pUSD Balance Fetched: {user_balance}")
    
    args_mkt = MarketOrderArgs(
        amount=2.0,  # 2 shares
        side=Side.BUY, 
        token_id=token_id, 
        user_usdc_balance=user_balance
    )
    mkt_order = client.create_and_post_market_order(args_mkt, options=options, order_type=OrderType.FAK)
    logger.info(f"Fee-adjusted Market Order Executed: {mkt_order}")

if __name__ == "__main__":
    asyncio.run(run_e2e())