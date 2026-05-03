"""
wrap_usdce.py -- Wraps USDC.e into Polymarket pUSD using the Collateral Onramp.
"""
import logging
from web3 import Web3
from py_clob_client_v2.config import get_contract_config
import config

logger = logging.getLogger(__name__)

USDC_E_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

ERC20_ABI = [
    {"name": "approve", "type": "function", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "balanceOf", "type": "function", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "decimals", "type": "function", "inputs": [], "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
    {"name": "allowance", "type": "function", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

ONRAMP_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable"
    }
]

def wrap_usdce_if_needed() -> None:
    if not config.PRIVATE_KEY:
        logger.error("[WRAP] Cannot wrap: PRIVATE_KEY not set.")
        return

    if not config.COLLATERAL_ONRAMP_ADDRESS:
        logger.error("[WRAP] COLLATERAL_ONRAMP_ADDRESS is empty in .env. Skipping wrap.")
        return

    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC))
    if not w3.is_connected():
        logger.error("[WRAP] Cannot connect to Polygon RPC.")
        return

    account = w3.eth.account.from_key(config.PRIVATE_KEY)
    signer = account.address

    if config.FUNDER_ADDRESS and config.FUNDER_ADDRESS.lower() != signer.lower():
        logger.warning("[WRAP] FUNDER differs from SIGNER. Only SIGNER's balance can be wrapped via script. Proceeding for SIGNER.")

    usdce = w3.eth.contract(address=USDC_E_ADDRESS, abi=ERC20_ABI)
    cfg = get_contract_config(config.CHAIN_ID)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(cfg.collateral), abi=ERC20_ABI)

    usdce_decimals = usdce.functions.decimals().call()
    pusd_decimals = pusd.functions.decimals().call()

    usdce_bal = usdce.functions.balanceOf(signer).call()
    pusd_bal = pusd.functions.balanceOf(signer).call()

    logger.info("[WRAP] Startup Balances -> USDC.e: %.6f | pUSD: %.6f", usdce_bal / 10**usdce_decimals, pusd_bal / 10**pusd_decimals)

    if usdce_bal == 0:
        logger.info("[WRAP] No USDC.e balance to wrap.")
        return

    onramp_addr = Web3.to_checksum_address(config.COLLATERAL_ONRAMP_ADDRESS)
    logger.info("[WRAP] Wrapping %.6f USDC.e to pUSD via Onramp %s...", usdce_bal / 10**usdce_decimals, onramp_addr)

    nonce = w3.eth.get_transaction_count(signer)
    allowance = usdce.functions.allowance(signer, onramp_addr).call()

    # 1. Approve USDC.e for Onramp
    if allowance < usdce_bal:
        logger.info("[WRAP] Approving Collateral Onramp to spend USDC.e...")
        tx = usdce.functions.approve(onramp_addr, usdce_bal).build_transaction({
            "from": signer, "nonce": nonce, "gas": 100_000, "gasPrice": w3.eth.gas_price, "chainId": config.CHAIN_ID
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash)
        nonce += 1

    # 2. Execute wrap()
    logger.info("[WRAP] Executing wrap()...")
    onramp = w3.eth.contract(address=onramp_addr, abi=ONRAMP_ABI)
    tx = onramp.functions.wrap(usdce_bal).build_transaction({
        "from": signer, "nonce": nonce, "gas": 200_000, "gasPrice": w3.eth.gas_price, "chainId": config.CHAIN_ID
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("[WRAP] Wrap tx sent: %s", tx_hash.hex())
    w3.eth.wait_for_transaction_receipt(tx_hash)

    new_pusd_bal = pusd.functions.balanceOf(signer).call()
    logger.info("[WRAP] Wrap complete! New pUSD Balance: %.6f", new_pusd_bal / 10**pusd_decimals)