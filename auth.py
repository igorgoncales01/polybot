"""
Authentication module for Polymarket CLOB API.
Handles API key derivation, request signing, and EIP-712 order signatures.
"""

import hmac
import hashlib
import time
import secrets
import logging
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from config import (
    CLOB_API_URL,
    PRIVATE_KEY,
    CHAIN_ID,
    EIP712_DOMAIN,
    EIP712_ORDER_TYPES,
)

logger = logging.getLogger("polybot.auth")


class ClobAuth:
    """Manages authentication with the Polymarket CLOB API."""

    def __init__(self):
        if not PRIVATE_KEY or PRIVATE_KEY == "0xYOUR_POLYGON_PRIVATE_KEY_HERE":
            raise ValueError("Set PRIVATE_KEY in .env")
        self.account = Account.from_key(PRIVATE_KEY)
        self.address = self.account.address
        self.api_key: str | None = None
        self.api_secret: str | None = None
        self.api_passphrase: str | None = None
        logger.info("Wallet loaded: %s", self.address)

    # ── API key lifecycle ─────────────────────────────────────────────

    def derive_api_key(self) -> None:
        """Derive (or re-derive) CLOB API credentials via L1 auth header."""
        nonce = int(time.time())
        raw = f"polymarket-clob:{nonce}"
        message_hash = hashlib.sha256(raw.encode()).hexdigest()
        sig = self.account.signHash(
            Account._parse_and_hash_message(message_hash)  # noqa: SLF001
        )

        headers = self._l1_auth_headers(nonce, sig.signature.hex())
        resp = httpx.post(
            f"{CLOB_API_URL}/auth/derive-api-key",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self.api_key = data["apiKey"]
        self.api_secret = data["secret"]
        self.api_passphrase = data["passphrase"]
        logger.info("API key derived successfully")

    def ensure_api_key(self) -> None:
        """Derive API key if we don't have one yet."""
        if not self.api_key:
            self.derive_api_key()

    # ── L1 auth (used only for derive-api-key) ───────────────────────

    @staticmethod
    def _l1_auth_headers(nonce: int, signature: str) -> dict[str, str]:
        return {
            "POLY_ADDRESS": "",
            "POLY_SIGNATURE": signature,
            "POLY_TIMESTAMP": str(nonce),
            "POLY_NONCE": str(nonce),
        }

    # ── L2 auth (HMAC on every CLOB request) ─────────────────────────

    def l2_headers(
        self, method: str, path: str, body: str = ""
    ) -> dict[str, str]:
        """Return HMAC-signed headers for an authenticated CLOB request."""
        self.ensure_api_key()
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method.upper()}{path}{body}"
        sig = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY_ADDRESS": self.address,
            "POLY_SIGNATURE": sig,
            "POLY_TIMESTAMP": timestamp,
            "POLY_NONCE": timestamp,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.api_passphrase,
        }

    # ── EIP-712 order signing ─────────────────────────────────────────

    def sign_order(self, order: dict[str, Any]) -> str:
        """Sign a CLOB order using EIP-712 typed data and return hex signature."""
        signable = encode_typed_data(
            domain_data=EIP712_DOMAIN,
            message_types=EIP712_ORDER_TYPES,
            message_data=order,
        )
        signed = self.account.sign_message(signable)
        return signed.signature.hex()

    def build_order_payload(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,  # "BUY" or "SELL"
        fee_rate_bps: int = 0,
        expiration: int = 0,
    ) -> dict[str, Any]:
        """
        Build a signed CLOB order payload ready to POST.

        price: decimal price 0-1 (e.g. 0.15 for 15¢)
        size: number of shares
        side: "BUY" or "SELL"
        """
        side_int = 0 if side == "BUY" else 1
        # CLOB uses 6-decimal USDC amounts
        maker_amount = int(size * price * 1e6) if side == "BUY" else int(size * 1e6)
        taker_amount = int(size * 1e6) if side == "BUY" else int(size * price * 1e6)
        salt = secrets.randbits(128)
        nonce = 0

        order_data = {
            "salt": salt,
            "maker": self.address,
            "signer": self.address,
            "taker": "0x0000000000000000000000000000000000000000",
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "expiration": expiration,
            "nonce": nonce,
            "feeRateBps": fee_rate_bps,
            "side": side_int,
            "signatureType": 2,  # POLY_GNOSIS_SAFE
        }

        signature = self.sign_order(order_data)

        return {
            "order": order_data,
            "signature": signature,
            "owner": self.address,
            "orderType": "GTC",
        }
