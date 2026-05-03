"""
v2_smoke.py -- Post-cutover sanity check script.
Verifies V2 SDK version, API connectivity, authentication, and V2 market data fetching.
"""

import importlib.metadata
import logging
import os
import sys

# Append parent directory to path so we can import project modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from main import _build_clob_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("v2_smoke")

def run_smoke_test() -> None:
    logger.info("=== Polymarket V2 Smoke Test ===")
    
    try:
        version = importlib.metadata.version('py_clob_client_v2')
        logger.info(f"py_clob_client_v2 version: {version}")
    except importlib.metadata.PackageNotFoundError:
        logger.error("py_clob_client_v2 is not installed! Please run 'pip install py-clob-client-v2'.")
        return

    logger.info(f"Connecting to CLOB Host: {config.CLOB_HOST}")
    
    try:
        # This will internally derive/validate keys and make a cheap authenticated call (get_open_orders)
        client = _build_clob_client()
        logger.info("Authentication successful. L2 Credentials are valid.")
    except Exception as exc:
        logger.error(f"Failed to authenticate with V2 CLOB: {exc}")
        return

    # A known condition ID to verify data structures
    condition_id = "0xaf5e903876ad42de97e1cf02c2ef8484df69bcfc5541b96a400116557d1e504e"
    try:
        info = client.get_clob_market_info(condition_id)
        logger.info(f"Market Info fetched successfully: tick_size={info.get('mts')}, fee_rate={info.get('fd', {}).get('r')}")
    except Exception as exc:
        logger.error(f"Failed to fetch market info from V2: {exc}")
        return

if __name__ == "__main__":
    run_smoke_test()