"""Build a mock PAYMENT-SIGNATURE value for tests and local demos.

This is not a real EIP-3009 authorization. The mock verifier accepts it
only because MUSE_X402_FACILITATOR is forced to ``mock``.
"""

from __future__ import annotations

from muse.settlement.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_USDC, default_pay_to
from muse.settlement.x402 import encode_payment


def make_mock_payment(
    *,
    amount: int,
    pay_to: str | None = None,
    asset: str = BASE_SEPOLIA_USDC,
    network: str = BASE_SEPOLIA_CAIP2,
    payer: str = "0x1111111111111111111111111111111111111111",
    as_header: bool = True,
) -> str | dict:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": network,
        "mock": True,
        "asset": asset,
        "payload": {
            "signature": "0xmock",
            "authorization": {
                "from": payer,
                "to": pay_to or default_pay_to(),
                "value": str(amount),
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "ab" * 32,
            },
        },
    }
    return encode_payment(payload) if as_header else payload
