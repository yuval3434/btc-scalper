"""
diagnose2.py -- Check proxy wallet registration in V2 exchange contract.
Run: python3 diagnose2.py
"""
import os
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client_v2.config import get_contract_config
from py_clob_client_v2.order_utils.abi.abis import exchange_v2_abi
from eth_account import Account

load_dotenv()

POLYGON_RPC  = "https://polygon-rpc.com"
PRIVATE_KEY  = os.getenv("PRIVATE_KEY", "")
FUNDER       = os.getenv("FUNDER_ADDRESS", "").strip()

if not PRIVATE_KEY:
    raise SystemExit("PRIVATE_KEY not set")

signer  = Account.from_key(PRIVATE_KEY).address
funder  = FUNDER or signer

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
if not w3.is_connected():
    raise SystemExit("Cannot connect to Polygon RPC")
print(f"Connected to Polygon (block {w3.eth.block_number})\n")

cfg = get_contract_config(137)
exchange_v2_addr = Web3.to_checksum_address(cfg.exchange_v2)

print(f"Magic wallet (signer) : {signer}")
print(f"Funder in .env        : {funder}")
print(f"V2 Exchange contract  : {exchange_v2_addr}\n")

exchange = w3.eth.contract(address=exchange_v2_addr, abi=exchange_v2_abi)

# Check what proxy wallet the V2 exchange associates with the Magic wallet
try:
    proxy = exchange.functions.getProxyWalletAddress(signer).call()
    print(f"V2 proxy for signer   : {proxy}")
    zero = "0x" + "0" * 40
    if proxy.lower() == zero.lower():
        print("⚠️  NO proxy wallet registered in V2 for this Magic wallet!")
        print("   → You need to log into polymarket.com and make at least one trade")
        print("     via the web UI to trigger V2 proxy registration.")
    elif proxy.lower() == funder.lower():
        print("✅ Proxy matches FUNDER_ADDRESS — registration is correct!")
        print("   The 'invalid signature' may be a different issue.")
    else:
        print(f"⚠️  Proxy ({proxy}) does NOT match FUNDER_ADDRESS ({funder})")
        print(f"   → Update FUNDER_ADDRESS in .env to: {proxy}")
except Exception as e:
    print(f"getProxyWalletAddress call failed: {e}")

# Also check if the signer is a valid operator
try:
    is_op = exchange.functions.isOperator(signer).call()
    print(f"\nisOperator(signer)    : {is_op}")
except Exception as e:
    print(f"isOperator check failed: {e}")
