"""
approve_onchain.py -- Makes the actual on-chain ERC-20 approval transactions
so the Polymarket V2 exchange contracts can spend pUSD from your wallet.

Run this ONCE on the VPS:
    python3 approve_onchain.py

What it does:
  1. Asks the Polymarket CLOB API which contracts need allowance
  2. Checks your pUSD balance and POL gas balance
  3. Sends approve(contract, MAX_UINT256) on the pUSD ERC-20 for each contract
  4. Tells the CLOB to re-read the updated allowance
"""

import os
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client_v2 import ApiCreds, ClobClient
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
POLYGON_RPC    = "https://polygon-rpc.com"
CLOB_HOST      = "https://clob.polymarket.com"
CHAIN_ID       = 137
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "").strip() or None
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

# pUSD token contract on Polygon (6 decimals)
PUSD_ADDRESS = Web3.to_checksum_address("0xc011a7e12a19f7b1f670d46f08b03f3342e82dfb")
MAX_UINT256  = 2**256 - 1

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
    {"name": "decimals",  "type": "function", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
]

# ── Step 1: get contract addresses from CLOB API ──────────────────────────────
print("Fetching required allowance contracts from Polymarket CLOB...")
creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE) if API_KEY else None
client = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=PRIVATE_KEY, creds=creds, funder=FUNDER_ADDRESS)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
try:
    bal_info = client.get_balance_allowance(params=params)
except Exception as e:
    print(f"Warning: CLOB API call failed: {e}")
    bal_info = {}

print("CLOB balance/allowance info:", bal_info)

# Extract contract addresses from the 'allowances' dict returned by the API
exchange_contracts = []
allowances_dict = bal_info.get("allowances", {}) if isinstance(bal_info, dict) else {}
for addr_str in allowances_dict.keys():
    try:
        exchange_contracts.append(Web3.to_checksum_address(addr_str))
    except Exception as e:
        print(f"  Skipping invalid address '{addr_str}': {e}")

if not exchange_contracts:
    print("No exchange contracts found in CLOB response — using known Polymarket V2 contracts")
    # Fallback: known Polymarket contracts from their GitHub
    exchange_contracts = [
        Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982D"),  # CTF Exchange
        Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a"),  # Neg Risk CTF Exchange
        Web3.to_checksum_address("0xd91f80cF2f7bE2e162c6513ceD06f1d0da35296"),  # Neg Risk Adapter
    ]

print(f"Will approve {len(exchange_contracts)} contracts: {exchange_contracts}")

# ── Step 2: connect to Polygon ────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
if not w3.is_connected():
    raise SystemExit("ERROR: Cannot connect to Polygon RPC.")
print(f"\nConnected to Polygon (block {w3.eth.block_number})")

account = w3.eth.account.from_key(PRIVATE_KEY)
signer  = account.address
funder  = Web3.to_checksum_address(FUNDER_ADDRESS) if FUNDER_ADDRESS else signer
print(f"Signer  : {signer}")
print(f"Funder  : {funder}")

if funder.lower() != signer.lower():
    print("\n⚠️  WARNING: FUNDER_ADDRESS differs from your PRIVATE_KEY address.")
    print("   The ERC-20 approval must come from the FUNDER's wallet.")
    print("   Since your bot only holds the SIGNER key, you need to approve manually:")
    print(f"   Go to polygonscan.com, connect with your funder wallet, and call")
    print(f"   approve(spender, 115792089...MAX) on pUSD contract {PUSD_ADDRESS}")
    for c in exchange_contracts:
        print(f"   → spender: {c}")
    raise SystemExit("Manual approval required.")

pusd = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
decimals    = pusd.functions.decimals().call()
raw_balance = pusd.functions.balanceOf(funder).call()
balance     = raw_balance / 10**decimals
pol_balance = w3.eth.get_balance(signer)

print(f"\npUSD balance : {balance:.6f} pUSD")
print(f"POL balance  : {w3.from_wei(pol_balance, 'ether'):.6f} POL")

if pol_balance == 0:
    raise SystemExit("ERROR: No POL for gas. Send some POL to your wallet first.")

if balance == 0:
    raise SystemExit("ERROR: No pUSD in this wallet. Check your FUNDER_ADDRESS in .env.")

# ── Step 3: approve each contract ────────────────────────────────────────────
nonce = w3.eth.get_transaction_count(signer)

for contract_addr in exchange_contracts:
    current = pusd.functions.allowance(funder, contract_addr).call()
    if current >= MAX_UINT256 // 2:
        print(f"\n✅ {contract_addr} already approved — skipping.")
        continue
    print(f"\nApproving {contract_addr} ...")
    tx = pusd.functions.approve(contract_addr, MAX_UINT256).build_transaction({
        "from": signer, "nonce": nonce,
        "gas": 100_000, "gasPrice": w3.eth.gas_price, "chainId": CHAIN_ID,
    })
    signed   = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent: {tx_hash.hex()}")
    receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    status   = "✅ Approved!" if receipt.status == 1 else "❌ FAILED"
    print(f"  {status} Gas used: {receipt.gasUsed}")
    nonce += 1

# ── Step 4: tell CLOB to re-sync ─────────────────────────────────────────────
print("\nAsking CLOB to re-read on-chain allowance...")
result = client.update_balance_allowance(params=params)
bal2   = client.get_balance_allowance(params=params)
print("Updated CLOB view:", bal2)

print("\n✅ All done! Run: python3 main.py")
