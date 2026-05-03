"""
approve_allowance.py -- One-time script to approve the Polymarket V2 exchange
contract to spend pUSD from your wallet.

Run this ONCE on the VPS before starting the bot:
    python approve_allowance.py

You only need to run it again if you change wallets.
"""

import os
from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
from py_clob_client_v2.order_utils import SignatureTypeV2

load_dotenv()

CLOB_HOST       = "https://clob.polymarket.com"
CHAIN_ID        = 137
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS  = os.getenv("FUNDER_ADDRESS", "") or None
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
API_PASSPHRASE  = os.getenv("API_PASSPHRASE", "")

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

print("Connecting to Polymarket V2 CLOB...")
print(f"Funder address: {FUNDER_ADDRESS or '(same as signer)'}")

creds = ApiCreds(
    api_key=API_KEY,
    api_secret=API_SECRET,
    api_passphrase=API_PASSPHRASE,
) if API_KEY else None

client = ClobClient(
    host=CLOB_HOST,
    chain_id=CHAIN_ID,
    key=PRIVATE_KEY,
    creds=creds,
    signature_type=SignatureTypeV2.POLY_PROXY if FUNDER_ADDRESS else SignatureTypeV2.EOA,
    funder=FUNDER_ADDRESS,   # <-- critical: approves allowance for funder's pUSD
)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)

print("Checking current pUSD balance / allowance...")
bal = client.get_balance_allowance(params=params)
print("Current:", bal)

print("\nSending approval transaction (sets max allowance for pUSD)...")
result = client.update_balance_allowance(params=params)
print("Done:", result)

print("\nVerifying updated allowance...")
bal2 = client.get_balance_allowance(params=params)
print("After:", bal2)

print("\nAll good! You can now start the bot.")
