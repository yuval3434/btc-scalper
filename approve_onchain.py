"""
approve_onchain.py -- Makes the actual on-chain ERC-20 approval transactions
so the Polymarket V2 exchange contracts can spend pUSD from your wallet.

Run this ONCE on the VPS:
    python3 approve_onchain.py

No API credentials needed — uses web3 directly with your PRIVATE_KEY.
"""

import os
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client_v2.config import get_contract_config

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
POLYGON_RPC    = "https://polygon-rpc.com"
CHAIN_ID       = 137
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "").strip() or None

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

# Contract addresses from py_clob_client_v2.config (authoritative source)
cfg = get_contract_config(CHAIN_ID)
PUSD_ADDRESS = Web3.to_checksum_address(cfg.collateral)   # pUSD token

# All exchange contracts that need ERC-20 approval to spend pUSD
EXCHANGE_CONTRACTS = [
    Web3.to_checksum_address(cfg.exchange),              # CTF Exchange V1
    Web3.to_checksum_address(cfg.exchange_v2),           # CTF Exchange V2
    Web3.to_checksum_address(cfg.neg_risk_exchange),     # Neg Risk Exchange
    Web3.to_checksum_address(cfg.neg_risk_exchange_v2),  # Neg Risk Exchange V2
    Web3.to_checksum_address(cfg.neg_risk_adapter),      # Neg Risk Adapter
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {"name": "approve",   "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "allowance", "type": "function",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "balanceOf", "type": "function",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "decimals", "type": "function", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
]

# ── Connect ───────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
if not w3.is_connected():
    raise SystemExit("ERROR: Cannot connect to Polygon RPC.")
print(f"Connected to Polygon (block {w3.eth.block_number})")

account = w3.eth.account.from_key(PRIVATE_KEY)
signer  = account.address
funder  = Web3.to_checksum_address(FUNDER_ADDRESS) if FUNDER_ADDRESS else signer
print(f"Signer  : {signer}")
print(f"Funder  : {funder}")
print(f"pUSD    : {PUSD_ADDRESS}")

if funder.lower() != signer.lower():
    print("\n⚠️  FUNDER differs from SIGNER — cannot sign approval on funder's behalf.")
    print("   Approve manually on polygonscan.com with your funder wallet.")
    for c in EXCHANGE_CONTRACTS:
        print(f"   → approve {c} on pUSD contract {PUSD_ADDRESS}")
    raise SystemExit("Manual approval required.")

pusd        = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
decimals    = pusd.functions.decimals().call()
raw_balance = pusd.functions.balanceOf(funder).call()
balance     = raw_balance / 10**decimals
pol_balance = w3.eth.get_balance(signer)

print(f"\npUSD balance : {balance:.6f}")
print(f"POL balance  : {w3.from_wei(pol_balance, 'ether'):.6f}")

if pol_balance == 0:
    raise SystemExit("ERROR: No POL for gas. Send ~0.01 POL to your wallet first.")
if balance == 0:
    raise SystemExit("ERROR: No pUSD found in this wallet.")

# ── Approve ───────────────────────────────────────────────────────────────────
nonce = w3.eth.get_transaction_count(signer)

for contract_addr in EXCHANGE_CONTRACTS:
    current = pusd.functions.allowance(funder, contract_addr).call()
    if current >= MAX_UINT256 // 2:
        print(f"\n✅ Already approved: {contract_addr}")
        continue
    print(f"\nApproving {contract_addr} ...")
    tx = pusd.functions.approve(contract_addr, MAX_UINT256).build_transaction({
        "from": signer, "nonce": nonce,
        "gas": 100_000, "gasPrice": w3.eth.gas_price, "chainId": CHAIN_ID,
    })
    signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    print(f"  {'✅ Approved!' if receipt.status == 1 else '❌ FAILED'} Gas used: {receipt.gasUsed}")
    nonce += 1

print("\n✅ All done! Run: python3 main.py")
