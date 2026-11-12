"""x402 payment-required documents and header helpers.

Phase 0 speaks **x402 v2** (PAYMENT-REQUIRED / PAYMENT-SIGNATURE /
PAYMENT-RESPONSE, CAIP-2 networks) and still accepts the v1 header names
(``X-PAYMENT``, ``X-PAYMENT-RESPONSE``) and the v1 body shape, which v2
libraries also look for. See:

- https://docs.cdp.coinbase.com/x402/core-concepts/how-it-works
- https://docs.cdp.coinbase.com/x402/migration-guide

This module only builds and parses messages. It does not verify or settle.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

from muse.settlement.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_USDC

V2 = 2
V1 = 1

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"
# v1 names, still accepted.
X_PAYMENT_HEADER = "X-PAYMENT"
X_PAYMENT_RESPONSE_HEADER = "X-PAYMENT-RESPONSE"


def _b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: str) -> dict[str, Any]:
    raw = value.strip()
    # Allow raw JSON for tests and for humans curling the stub.
    if raw.startswith("{") or raw.startswith("mock:"):
        if raw.startswith("mock:"):
            return {"mock_token": raw, "x402Version": V2, "scheme": "exact", "network": BASE_SEPOLIA_CAIP2}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("payment payload must be an object")
        return parsed
    padding = "=" * ((4 - len(raw) % 4) % 4)
    parsed = json.loads(base64.b64decode(raw + padding).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("payment payload must be an object")
    return parsed


@dataclass(frozen=True)
class PaymentOption:
    scheme: str
    network: str
    amount: str
    asset: str
    pay_to: str
    max_timeout_seconds: int
    extra: dict[str, Any]

    def as_v2(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "amount": self.amount,
            "asset": self.asset,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": self.extra,
        }

    def as_v1(self, *, resource: str, description: str, mime_type: str) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": "base-sepolia" if self.network == BASE_SEPOLIA_CAIP2 else self.network,
            "maxAmountRequired": self.amount,
            "resource": resource,
            "description": description,
            "mimeType": mime_type,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset": self.asset,
            "extra": self.extra,
        }


def usdc_sepolia_option(
    *,
    amount_atomic: int,
    pay_to: str,
    asset: str = BASE_SEPOLIA_USDC,
    max_timeout_seconds: int = 60,
) -> PaymentOption:
    return PaymentOption(
        scheme="exact",
        network=BASE_SEPOLIA_CAIP2,
        amount=str(amount_atomic),
        asset=asset,
        pay_to=pay_to,
        max_timeout_seconds=max_timeout_seconds,
        extra={"name": "USDC", "version": "2", "decimals": 6},
    )


def payment_required(
    *,
    option: PaymentOption,
    resource_url: str,
    description: str,
    mime_type: str = "application/json",
    error: str = "PAYMENT_REQUIRED",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(body, headers)`` for an HTTP 402 response.

    The body is the v1-compatible document (still widely parsed). The
    ``PAYMENT-REQUIRED`` header carries the v2 document, base64-encoded.
    """
    v2 = {
        "x402Version": V2,
        "error": error,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": mime_type,
        },
        "accepts": [option.as_v2()],
    }
    v1 = {
        "x402Version": V1,
        "error": error,
        "accepts": [
            option.as_v1(resource=resource_url, description=description, mime_type=mime_type)
        ],
    }
    encoded = _b64(v2)
    headers = {
        PAYMENT_REQUIRED_HEADER: encoded,
        # Help a v1 client that looks at a header of the same shape.
        "Access-Control-Expose-Headers": (
            f"{PAYMENT_REQUIRED_HEADER}, {PAYMENT_RESPONSE_HEADER}, {X_PAYMENT_RESPONSE_HEADER}"
        ),
    }
    return v1, headers


def read_payment_header(headers: Mapping[str, str]) -> dict[str, Any] | None:
    """Extract a payment payload from v2 or v1 header names, or None if absent."""
    value = None
    for key, item in headers.items():
        if key.lower() in {PAYMENT_SIGNATURE_HEADER.lower(), X_PAYMENT_HEADER.lower()}:
            value = item
            break
    if not value or not str(value).strip():
        return None
    return _unb64(str(value))


def encode_payment(payload: dict[str, Any]) -> str:
    """Base64-encode a payment payload for PAYMENT-SIGNATURE / X-PAYMENT."""
    return _b64(payload)


def encode_payment_response(receipt: dict[str, Any]) -> dict[str, str]:
    encoded = _b64(receipt)
    return {
        PAYMENT_RESPONSE_HEADER: encoded,
        X_PAYMENT_RESPONSE_HEADER: encoded,
    }
