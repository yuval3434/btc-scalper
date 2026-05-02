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

load_dotenv()

CLOB_HOST  = "https://clob.polymarket.com"
CHAIN_ID   = 137
PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
API_KEY         = os.getenv("API_KEY", "")
API_SECRET      = os.getenv("API_SECRET", "")
API_PASSPHRASE  = os.getenv("API_PASSPHRASE", "")

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

print("Connecting to Polymarket V2 CLOB...")
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
)

print("Checking current balance / allowance...")
bal = client.get_balance_allowance()
print("Current:", bal)

print("\nSending approval transaction (sets max allowance for pUSD)...")
result = client.update_balance_allowance()
print("Done:", result)
print("\nAll good! You can now start the bot.")
