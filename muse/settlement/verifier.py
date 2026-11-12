"""Mock x402 verifier. No RPC, no broadcast, no real funds.

A payment is accepted only when:

* the network is Base Sepolia (``eip155:84532`` / ``base-sepolia``);
* the asset is the Circle USDC testnet address;
* the scheme is ``exact``;
* ``payTo`` matches the quote;
* ``value`` / ``amount`` is at least the quoted atomic amount;
* the payload is a well-formed mock (a ``0xmock`` signature, a ``mock:`` token,
  or a body with ``"mock": true``).

Mainnet networks are always rejected. This verifier never talks to a
facilitator URL and never submits a transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from muse.settlement.constants import (
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_USDC,
    canonicalize_address,
    is_allowed_network,
    is_mainnet_network,
    normalize_network,
)


class PaymentRejected(Exception):
    def __init__(self, reason: str, *, code: str = "invalid_payment") -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


@dataclass(frozen=True)
class Verification:
    accepted: bool
    receipt_id: str
    network: str
    asset: str
    amount: str
    pay_to: str
    payer: str | None
    settled: bool  # always False in phase 0 — we do not settle on chain
    mode: str  # "mock"
    verified_at: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "receiptId": self.receipt_id,
            "network": self.network,
            "asset": self.asset,
            "amount": self.amount,
            "payTo": self.pay_to,
            "payer": self.payer,
            "settled": self.settled,
            "mode": self.mode,
            "verifiedAt": self.verified_at,
            "detail": self.detail,
            "x402Version": 2,
        }


@dataclass
class MockVerifier:
    network: str = BASE_SEPOLIA_CAIP2
    asset: str = BASE_SEPOLIA_USDC
    pay_to: str = ""
    price_atomic: int = 10_000

    def verify(self, payload: dict[str, Any] | None, *, price_atomic: int | None = None) -> Verification:
        if not payload:
            raise PaymentRejected("missing payment payload", code="missing")
        required = self.price_atomic if price_atomic is None else price_atomic
        network = self._network(payload)
        if is_mainnet_network(network) or is_mainnet_network(str(payload.get("network", ""))):
            raise PaymentRejected(
                "mainnet payments are refused by the phase 0 mock verifier",
                code="mainnet_refused",
            )
        if not is_allowed_network(network):
            raise PaymentRejected(
                f"network {network!r} is not Base Sepolia (eip155:84532)",
                code="wrong_network",
            )
        if not self._is_mock(payload):
            raise PaymentRejected(
                "phase 0 only accepts mock payments (signature 0xmock, mock: token, or mock=true). "
                "A live EIP-3009 authorization is not verified or settled here.",
                code="not_mock",
            )
        try:
            asset = canonicalize_address(self._asset(payload))
            expected = canonicalize_address(self.pay_to)
            pay_to = self._pay_to(payload)
            pay_to_c = canonicalize_address(pay_to) if pay_to else ""
        except ValueError as exc:
            raise PaymentRejected(str(exc), code="bad_address") from exc
        if asset != canonicalize_address(self.asset):
            raise PaymentRejected(f"asset is not Base Sepolia USDC", code="wrong_asset")
        if not pay_to_c:
            raise PaymentRejected("missing payTo", code="wrong_payee")
        if pay_to_c != expected:
            raise PaymentRejected("payTo does not match the quote", code="wrong_payee")
        amount = self._amount(payload)
        if amount < required:
            raise PaymentRejected(
                f"amount {amount} is below the quoted {required}",
                code="insufficient",
            )
        payer = self._payer(payload)
        now = datetime.now(timezone.utc).isoformat()
        return Verification(
            accepted=True,
            receipt_id=f"mock_{uuid4().hex}",
            network=BASE_SEPOLIA_CAIP2,
            asset=canonicalize_address(self.asset),
            amount=str(amount),
            pay_to=expected,
            payer=payer,
            settled=False,
            mode="mock",
            verified_at=now,
            detail="mock verification only; no funds moved; not submitted to Base Sepolia",
        )

    def _is_mock(self, payload: dict[str, Any]) -> bool:
        if payload.get("mock") is True:
            return True
        token = payload.get("mock_token")
        if isinstance(token, str) and token.startswith("mock:"):
            return bool(token[5:].strip())
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        sig = str((inner or {}).get("signature") or payload.get("signature") or "")
        return sig.lower() in {"0xmock", "mock", "0x00mock"}

    def _network(self, payload: dict[str, Any]) -> str:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        raw = payload.get("network") or (inner or {}).get("network") or ""
        return normalize_network(str(raw)) if raw else ""

    def _asset(self, payload: dict[str, Any]) -> str:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = (inner or {}).get("authorization") if isinstance((inner or {}).get("authorization"), dict) else {}
        raw = (
            payload.get("asset")
            or (inner or {}).get("asset")
            or (auth or {}).get("asset")
            or self.asset
        )
        return str(raw)

    def _pay_to(self, payload: dict[str, Any]) -> str:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = (inner or {}).get("authorization") if isinstance((inner or {}).get("authorization"), dict) else {}
        raw = (
            payload.get("payTo")
            or (inner or {}).get("payTo")
            or (auth or {}).get("to")
            or ""
        )
        return str(raw)

    def _amount(self, payload: dict[str, Any]) -> int:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = (inner or {}).get("authorization") if isinstance((inner or {}).get("authorization"), dict) else {}
        raw = (
            payload.get("amount")
            or payload.get("value")
            or (inner or {}).get("amount")
            or (auth or {}).get("value")
            or ""
        )
        try:
            return int(str(raw))
        except (TypeError, ValueError) as exc:
            raise PaymentRejected(f"amount is not an integer: {raw!r}", code="bad_amount") from exc

    def _payer(self, payload: dict[str, Any]) -> str | None:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = (inner or {}).get("authorization") if isinstance((inner or {}).get("authorization"), dict) else {}
        raw = payload.get("from") or (auth or {}).get("from") or (inner or {}).get("from")
        if not raw:
            return None
        try:
            return canonicalize_address(str(raw))
        except ValueError:
            return str(raw)
