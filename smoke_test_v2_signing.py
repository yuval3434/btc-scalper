import time
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

# 1. Bump the EIP-712 Exchange domain version to "2"
# 2. Update verifyingContract to V2 CTF Exchange (or Neg Risk V2)
DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "2",
    "chainId": 137,
    "verifyingContract": "0xE111180000d2663C0091e4f400237545B87B996B",
}

# 4. Update the Order EIP-712 type (Dropped taker, expiration, nonce, feeRateBps)
#    Added timestamp, metadata, builder.
TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "amount", "type": "uint256"},
        {"name": "price", "type": "uint256"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ]
}

def main():
    # Create a random throwaway account for the smoke test
    acct = Account.create()
    
    # 5. Build the message with the new fields
    timestamp_ms = int(time.time() * 1000)
    
    message = {
        "salt": 123456789,
        "maker": acct.address,
        "signer": acct.address,
        "timestamp": timestamp_ms,
        "amount": Web3.to_wei(10, 'ether'),  # Example: 10 shares
        "price": Web3.to_wei(0.5, 'ether'),  # Example: 50 cents
        "tokenId": int("0x" + "1" * 64, 16), # Example mock token
        "side": 0,                           # 0 = BUY, 1 = SELL
        "metadata": b'\x00' * 32,            # 32 zero bytes
        "builder": b'\x00' * 32,             # 32 zero bytes (or builder code)
    }

    typed_data = {
        "types": TYPES,
        "primaryType": "Order",
        "domain": DOMAIN,
        "message": message,
    }

    # Hash and sign the payload
    signable_message = encode_typed_data(full_message=typed_data)
    signed = acct.sign_message(signable_message)
    
    print("=== V2 EIP-712 Order Digest Smoke Test ===")
    print(f"Signer Address:   {acct.address}")
    print(f"Timestamp (ms):   {timestamp_ms}")
    print(f"Message Hash:     {signable_message.body.hex()}")
    print(f"Signature:        {signed.signature.hex()}")
    
    # 6. Example payload for POST /order
    api_payload = {
        "order": {
            "salt": str(message["salt"]),
            "maker": message["maker"],
            "signer": message["signer"],
            "timestamp": str(message["timestamp"]),
            "amount": str(message["amount"]),
            "price": str(message["price"]),
            "tokenId": str(message["tokenId"]),
            "side": "BUY",
            "expiration": str(timestamp_ms + 60000), # Expiration stays in API body
            "metadata": message["metadata"].hex(),
            "builder": message["builder"].hex(),
            "signature": signed.signature.hex(),
        }
    }
    # 7. Note: Make sure to NOT include POLY_BUILDER_* headers in your HTTP request.

if __name__ == "__main__":
    main()