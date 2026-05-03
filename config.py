"""
config.py — Central configuration for the BTC 5-min scalping bot.

All values are loaded from environment variables (or .env file).
Strategy parameters are intentionally grouped and commented so they
can be tuned without touching any trading logic.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Authentication — L1 (order signing) and L2 (API request auth)
# ---------------------------------------------------------------------------
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")
API_KEY: str = os.getenv("API_KEY", "")
API_SECRET: str = os.getenv("API_SECRET", "")
API_PASSPHRASE: str = os.getenv("API_PASSPHRASE", "")
CLOB_FORCE_DERIVE_CREDS: bool = os.getenv("CLOB_FORCE_DERIVE_CREDS", "").lower() in (
    "true",
    "1",
    "yes",
)

# Optional explicit CLOB order signature type:
#   0 = direct EOA wallet
#   1 = Polymarket email/Magic proxy wallet
#   2 = browser wallet proxy / Gnosis Safe
#   3 = EIP-1271 contract wallet (V2 only)
SIGNATURE_TYPE_RAW: str = os.getenv("SIGNATURE_TYPE", "").strip()
SIGNATURE_TYPE: int | None = int(SIGNATURE_TYPE_RAW) if SIGNATURE_TYPE_RAW else None
POLY_BUILDER_CODE: str = os.getenv("POLY_BUILDER_CODE", "").strip()

# ---------------------------------------------------------------------------
# Telegram notifications (optional)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
CLOB_HOST: str = os.getenv(
    "POLYMARKET_CLOB_HOST",
    "https://clob.polymarket.com",
).rstrip("/")
GAMMA_API: str = "https://gamma-api.polymarket.com"
MARKET_WS: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
CHAIN_ID: int = 137  # Polygon mainnet

# ---------------------------------------------------------------------------
# Operational mode
# ---------------------------------------------------------------------------
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

WRAP_ON_START: bool = os.getenv("WRAP_ON_START", "0") in ("1", "true", "yes")
POLYGON_RPC: str = os.getenv("POLYGON_RPC", "https://polygon-rpc.com")
# Retrieve this from https://docs.polymarket.com/concepts/pusd
COLLATERAL_ONRAMP_ADDRESS: str = os.getenv("COLLATERAL_ONRAMP_ADDRESS", "")

# Max number of active market slots to trade simultaneously.
MAX_CONCURRENT_MARKETS: int = int(os.getenv("MAX_CONCURRENT_MARKETS", "3"))

# Dollar amount of USDC to allocate per trade.
POSITION_SIZE_USDC: float = float(os.getenv("POSITION_SIZE_USDC", "10.0"))

# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------

# Golden Zone: only evaluate markets whose mid-price falls within this band.
# Backtested optimal: 0.25/0.75 (Sharpe 1.71 vs 0.92 for original 0.35/0.65).
MID_LOW: float = 0.25
MID_HIGH: float = 0.75

# Not used for live entry (bot now buys at best_ask immediately); kept for reference.
BUY_OFFSET: float = 0.03

# Take-profit sell is placed this far ABOVE fill price.
# R:R = TP_OFFSET / SL_PRICE_OFFSET = 0.12 / 0.08 = 1.5 : 1
# At 55% win rate (observed in paper trading) EV ≈ +5% per trade.
TP_OFFSET: float = 0.12

# Price-based stop-loss: exit if best bid drops this far BELOW fill price.
# 0.08 is wide enough to survive normal 5-min binary market noise without
# triggering on every transient dip (0.05 was too tight — win rate dropped to 33%).
SL_PRICE_OFFSET: float = 0.08

# Time-based stop-loss: exit if less than this many seconds remain.
SL_TIME_SECONDS: int = 45

# Order book imbalance threshold (bid vol / ask vol for top IMBALANCE_LEVELS).
# Raised to 1.30 to only enter on clear bid-side pressure, reducing false signals.
IMBALANCE_MIN_RATIO: float = 1.30
IMBALANCE_LEVELS: int = 5

# ---------------------------------------------------------------------------
# Risk / kill-switch parameters
# ---------------------------------------------------------------------------
MAX_CONSECUTIVE_LOSSES: int = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))

# How long (seconds) to suspend trading after hitting the loss limit.
KILL_SWITCH_PAUSE_SECONDS: int = int(os.getenv("KILL_SWITCH_PAUSE_SECONDS", "3600"))

# ---------------------------------------------------------------------------
# WebSocket reconnection
# ---------------------------------------------------------------------------
WS_RECONNECT_INITIAL: float = 1.0   # seconds before first retry
WS_RECONNECT_MAX: float = 30.0      # ceiling on backoff delay
WS_HEARTBEAT_INTERVAL: float = 10.0 # PING cadence (server requires < 15 s)

# ---------------------------------------------------------------------------
# Market discovery
# ---------------------------------------------------------------------------
# How often (seconds) to re-query Gamma API for newly opened 5-min markets.
MARKET_POLL_INTERVAL: int = 30

# A 5-min market is "active enough" to trade if it resolves within this window.
MARKET_MAX_REMAINING_SECONDS: int = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
