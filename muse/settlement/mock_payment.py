"""Build a mock PAYMENT-SIGNATURE value for tests and local demos.

This is not a real EIP-3009 authorization. The mock verifier accepts it
only because MUSE_X402_FACILITATOR is forced to ``mock``.

The signed authorization is the source of truth: ``to``, ``value``,
and ``nonce = SHA-256(quoteId)``. Top-level ``amount`` / ``payTo`` are
unsigned and are ignored by the verifier; they are omitted here so
tests cannot accidentally depend on them.
"""

from __future__ import annotations

from muse.settlement.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_USDC, default_pay_to
from muse.settlement.quotes import Quote, quote_nonce
from muse.settlement.x402 import encode_payment


def make_mock_payment(
    *,
    amount: int,
    pay_to: str | None = None,
    asset: str = BASE_SEPOLIA_USDC,
    network: str = BASE_SEPOLIA_CAIP2,
    payer: str = "0x1111111111111111111111111111111111111111",
    as_header: bool = True,
    quote: Quote | None = None,
    quote_id: str | None = None,
    valid_after: str = "0",
    valid_before: str = "9999999999",
    nonce: str | None = None,
    scheme: str = "exact",
) -> str | dict:
    qid = quote.quote_id if quote is not None else (quote_id or "test-quote")
    bound_nonce = nonce if nonce is not None else (quote.nonce if quote is not None else quote_nonce(qid))
    dest = pay_to or (quote.pay_to if quote is not None else default_pay_to())
    value = str(amount if quote is None else quote.amount)
    payload = {
        "x402Version": 2,
        "scheme": scheme,
        "network": network,
        "mock": True,
        "quoteId": qid,
        "payload": {
            "signature": "0xmock",
            "authorization": {
                "from": payer,
                "to": dest,
                "value": value,
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": bound_nonce,
            },
        },
    }
    return encode_payment(payload) if as_header else payload
