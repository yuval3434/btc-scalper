"""
approve_allowance.py -- One-time script to approve the Polymarket V2 exchange
contract to spend pUSD from your wallet.

Run this ONCE on the VPS before starting the bot:
    python approve_allowance.py

You only need to run it again if you change wallets.
"""

import os
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client_v2 import ApiCreds, ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
from py_clob_client_v2.order_utils import SignatureTypeV2

load_dotenv()

CLOB_HOST       = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")
CHAIN_ID        = 137
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS  = os.getenv("FUNDER_ADDRESS", "") or None
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
API_PASSPHRASE  = os.getenv("API_PASSPHRASE", "")
SIGNATURE_TYPE  = os.getenv("SIGNATURE_TYPE", "").strip()

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

funder_addr = Web3.to_checksum_address(FUNDER_ADDRESS) if FUNDER_ADDRESS else None

print("Connecting to Polymarket V2 CLOB...")
print(f"Funder address: {funder_addr or '(same as signer)'}")

sig_type = (
    SignatureTypeV2(int(SIGNATURE_TYPE))
    if SIGNATURE_TYPE
    else (SignatureTypeV2.POLY_PROXY if funder_addr else SignatureTypeV2.EOA)
)
print(f"Signature type: {sig_type.name} ({int(sig_type)})")

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
    signature_type=sig_type,
    funder=funder_addr,      # <-- critical: approves allowance for funder's pUSD
    use_server_time=True,
    retry_on_error=True,
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
