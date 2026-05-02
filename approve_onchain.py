"""
approve_onchain.py -- Makes the actual on-chain ERC-20 approval transactions
so the Polymarket V2 exchange contracts can spend pUSD from your wallet.

Run this ONCE on the VPS:
    python approve_onchain.py

What it does:
  1. Derives your wallet address from PRIVATE_KEY
  2. Checks pUSD balance in that wallet
  3. Approves all three Polymarket V2 exchange contracts to spend your pUSD
     (these are the contract addresses Polymarket checks for allowance)
  4. Tells the CLOB API to re-read the updated allowance
"""

import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
POLYGON_RPC    = "https://polygon-rpc.com"
PRIVATE_KEY    = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "").strip() or None

if not PRIVATE_KEY:
    raise SystemExit("ERROR: PRIVATE_KEY not set in .env")

# ── Contracts ────────────────────────────────────────────────────────────────
# pUSD (PolymarketUSD) token on Polygon
PUSD_ADDRESS = Web3.to_checksum_address("0xc011a7e12a19f7b1f670d46f08b03f3342e82dfb")

# Polymarket V2 exchange contracts that need approval
# (taken from the allowances dict returned by get_balance_allowance)
EXCHANGE_CONTRACTS = [
    Web3.to_checksum_address("0x111180000d2663C0091e4f400237545B87B996B"),  # CTF Exchange V2
    Web3.to_checksum_address("0xd91F80cF2F7be2e162c6513ceD06f1d0da35296"),  # Neg Risk CTF Exchange
    Web3.to_checksum_address("0xe2222d279d744050d28e0052001052000031059"),  # Neg Risk Adapter
]

MAX_UINT256 = 2**256 - 1

ERC20_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
    },
]

# ── Connect ──────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
if not w3.is_connected():
    raise SystemExit("ERROR: Cannot connect to Polygon RPC. Try a different RPC.")
print(f"Connected to Polygon (block {w3.eth.block_number})")

account = w3.eth.account.from_key(PRIVATE_KEY)
signer  = account.address
print(f"Signer  address: {signer}")
print(f"Funder  address: {FUNDER_ADDRESS or '(same as signer)'}")

# The wallet that actually holds pUSD (and needs to sign approvals)
funder = Web3.to_checksum_address(FUNDER_ADDRESS) if FUNDER_ADDRESS else signer

if funder.lower() != signer.lower():
    print("\n⚠️  WARNING: FUNDER_ADDRESS differs from your PRIVATE_KEY address.")
    print("   The approval must be signed by the FUNDER's wallet.")
    print("   Your bot can only sign with PRIVATE_KEY.")
    print("   Please approve manually via MetaMask / polygonscan.com/token/")
    print(f"   pUSD contract: {PUSD_ADDRESS}")
    for c in EXCHANGE_CONTRACTS:
        print(f"   Approve: {c}")
    raise SystemExit("Manual approval required (funder != signer).")

pusd = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
decimals = pusd.functions.decimals().call()
raw_balance = pusd.functions.balanceOf(funder).call()
balance = raw_balance / 10**decimals
print(f"\npUSD balance in {funder}: {balance:.6f} pUSD")

pol_balance = w3.eth.get_balance(signer)
print(f"POL (gas) balance:        {w3.from_wei(pol_balance, 'ether'):.6f} POL")

if pol_balance == 0:
    raise SystemExit("ERROR: No POL for gas. Send a small amount of POL to your wallet.")

# ── Approve each contract ────────────────────────────────────────────────────
nonce = w3.eth.get_transaction_count(signer)

for contract_addr in EXCHANGE_CONTRACTS:
    current = pusd.functions.allowance(funder, contract_addr).call()
    if current >= MAX_UINT256 // 2:
        print(f"\n✅ {contract_addr} already has max allowance — skipping.")
        continue

    print(f"\nApproving {contract_addr} ...")
    tx = pusd.functions.approve(contract_addr, MAX_UINT256).build_transaction({
        "from":     signer,
        "nonce":    nonce,
        "gas":      100_000,
        "gasPrice": w3.eth.gas_price,
        "chainId":  137,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        print(f"  ✅ Approved! Gas used: {receipt.gasUsed}")
    else:
        print(f"  ❌ Transaction failed! Receipt: {receipt}")
    nonce += 1

# ── Tell the CLOB to re-sync ──────────────────────────────────────────────────
print("\nTelling Polymarket CLOB to re-read on-chain allowance...")
try:
    from py_clob_client_v2 import ApiCreds, ClobClient
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    API_KEY        = os.getenv("API_KEY", "")
    API_SECRET     = os.getenv("API_SECRET", "")
    API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE) if API_KEY else None
    client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=PRIVATE_KEY, creds=creds, funder=FUNDER_ADDRESS or None)
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(params=params)
    bal = client.get_balance_allowance(params=params)
    print("CLOB balance/allowance:", bal)
except Exception as e:
    print(f"CLOB sync skipped: {e}")

print("\n✅ All done! Run: python main.py")
