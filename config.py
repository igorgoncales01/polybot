import os
from dotenv import load_dotenv

load_dotenv()

# API
CLOB_API_URL = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

# Strategy thresholds
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.05"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.25"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000"))
BOUNCE_TARGET = float(os.getenv("BOUNCE_TARGET", "0.08"))
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.20"))
ORDER_FRAGMENTS = int(os.getenv("ORDER_FRAGMENTS", "15"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "8"))

# Risk management
MAX_POSITION_USD = float(os.getenv("MAX_POSITION_USD", "50"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))

# Category filter (set FILTER_CATEGORIES=false in .env to disable)
FILTER_CATEGORIES = os.getenv("FILTER_CATEGORIES", "true").lower() == "true"
TARGET_CATEGORIES = ["soccer", "nba", "nhl", "tennis", "esports"]

# Live market filter
MAX_HOURS_TO_END = float(os.getenv("MAX_HOURS_TO_END", "0"))  # 0 = disabled

# SportMonks API (live soccer engine)
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY", "")

# Logging
LOG_FILE = os.getenv("LOG_FILE", "polybot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Polymarket CLOB chain ID (Polygon mainnet)
CHAIN_ID = 137

# CLOB Exchange contract address (Polygon)
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# EIP-712 domain for CLOB orders
EIP712_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": CHAIN_ID,
    "verifyingContract": EXCHANGE_ADDRESS,
}

EIP712_ORDER_TYPES = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}
