"""
diagnose.py -- Check if PRIVATE_KEY address matches FUNDER_ADDRESS.
Run: python3 diagnose.py
"""
import os
from dotenv import load_dotenv
from eth_account import Account

load_dotenv()

pk = os.getenv("PRIVATE_KEY", "")
funder = os.getenv("FUNDER_ADDRESS", "").strip()

if not pk:
    raise SystemExit("PRIVATE_KEY not set in .env")

derived = Account.from_key(pk).address
print(f"PRIVATE_KEY derives to : {derived}")
print(f"FUNDER_ADDRESS in .env : {funder or '(not set)'}")

if funder and derived.lower() != funder.lower():
    print("\n⚠️  MISMATCH — these are TWO DIFFERENT wallets.")
    print("   The bot signs with PRIVATE_KEY but orders list FUNDER as maker.")
    print("   Solution: remove FUNDER_ADDRESS from .env (or set it to match PRIVATE_KEY address).")
else:
    print("\n✅ Same wallet — funder= is redundant, safe to remove from .env.")
    print("   If you still get 401, the API credentials are stale. Clear API_KEY/SECRET/PASSPHRASE and restart.")
