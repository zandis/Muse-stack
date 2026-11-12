"""Mock x402 verifier. No RPC, no broadcast, no real funds.

A payment is accepted only when:

* the network is on the Base Sepolia allow-list (case-insensitive);
* the asset is the Circle USDC testnet address;
* the scheme is ``exact`` and the signed amount equals the quote;
* the *signed* authorization ``to`` / ``value`` / ``nonce`` match the
  quote (unsigned top-level ``amount`` / ``payTo`` are ignored);
* ``nonce`` equals ``SHA-256(quoteId)`` and has not been spent;
* ``validAfter`` / ``validBefore`` contain the current time;
* the quote has not expired;
* the payload is a well-formed mock (a ``0xmock`` signature, a
  ``mock:`` token, or a body with ``"mock": true``).

Mainnet (and any non-Sepolia) networks are always rejected. This
verifier never talks to a facilitator URL and never submits a
transaction.
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
    normalize_network,
)
from muse.settlement.quotes import Quote, QuoteStore, quote_nonce


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
    quote_id: str | None = None
    nonce: str | None = None

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
            "quoteId": self.quote_id,
            "nonce": self.nonce,
        }


@dataclass
class MockVerifier:
    network: str = BASE_SEPOLIA_CAIP2
    asset: str = BASE_SEPOLIA_USDC
    pay_to: str = ""
    price_atomic: int = 10_000
    quotes: QuoteStore | None = None
    clock_wall: Any = None  # optional callable returning unix seconds

    def _now(self) -> float:
        if self.clock_wall is not None:
            return float(self.clock_wall())
        return datetime.now(timezone.utc).timestamp()

    def verify(
        self,
        payload: dict[str, Any] | None,
        *,
        price_atomic: int | None = None,
        quote: Quote | None = None,
        result_id: str | None = None,
    ) -> Verification:
        if not payload:
            raise PaymentRejected("missing payment payload", code="missing")
        required = self.price_atomic if price_atomic is None else price_atomic
        if quote is not None:
            required = int(quote.amount)
        network = self._network(payload)
        if not is_allowed_network(network) or (
            payload.get("network") and not is_allowed_network(str(payload.get("network")))
        ):
            # Distinguish a clearly-mainnet name so existing tests can assert
            # the code, but the rule itself is the allow-list.
            folded = normalize_network(network or str(payload.get("network") or ""))
            if folded and folded != BASE_SEPOLIA_CAIP2:
                # If the caller named a mainnet-looking id, keep the old code.
                if any(s in folded for s in ("8453", "mainnet", "eip155:1", "ethereum", "base")) and "84532" not in folded and "sepolia" not in folded:
                    raise PaymentRejected(
                        "mainnet payments are refused by the phase 0 mock verifier",
                        code="mainnet_refused",
                    )
                raise PaymentRejected(
                    f"network {network!r} is not on the Base Sepolia allow-list (eip155:84532)",
                    code="wrong_network",
                )
        if not network:
            raise PaymentRejected(
                "network is required and must be Base Sepolia (eip155:84532)",
                code="wrong_network",
            )
        if not self._is_mock(payload):
            raise PaymentRejected(
                "phase 0 only accepts mock payments (signature 0xmock, mock: token, or mock=true). "
                "A live EIP-3009 authorization is not verified or settled here.",
                code="not_mock",
            )
        scheme = str(payload.get("scheme") or (payload.get("payload") or {}).get("scheme") or "").strip().lower()
        if scheme != "exact":
            raise PaymentRejected(
                "scheme must be 'exact'",
                code="wrong_scheme",
            )

        try:
            expected = canonicalize_address(quote.pay_to if quote is not None else self.pay_to)
            expected_asset = canonicalize_address(quote.asset if quote is not None else self.asset)
        except ValueError as exc:
            raise PaymentRejected(str(exc), code="bad_address") from exc

        # Signed fields only. Unsigned top-level amount / payTo are ignored.
        try:
            asset = canonicalize_address(self._signed_asset(payload) or self.asset)
            pay_to_c = canonicalize_address(self._signed_pay_to(payload)) if self._signed_pay_to(payload) else ""
        except ValueError as exc:
            raise PaymentRejected(str(exc), code="bad_address") from exc
        if asset != expected_asset:
            raise PaymentRejected("asset is not Base Sepolia USDC", code="wrong_asset")
        if not pay_to_c:
            raise PaymentRejected("missing signed authorization.to (top-level payTo is ignored)", code="wrong_payee")
        if pay_to_c != expected:
            raise PaymentRejected("signed payTo does not match the quote", code="wrong_payee")

        amount = self._signed_amount(payload)
        if amount != required:
            raise PaymentRejected(
                f"signed amount {amount} does not equal the quoted {required}",
                code="insufficient" if amount < required else "wrong_amount",
            )

        now = self._now()
        self._check_validity_window(payload, now)

        quote_id = (
            (quote.quote_id if quote is not None else None)
            or payload.get("quoteId")
            or (payload.get("payload") or {}).get("quoteId")
        )
        if quote is not None:
            if now > quote.expires_at:
                raise PaymentRejected("quote has expired", code="quote_expired")
            if result_id is not None and quote.result_id != result_id:
                raise PaymentRejected("quote is bound to a different result", code="wrong_resource")
            expected_nonce = quote.nonce
        elif quote_id:
            expected_nonce = quote_nonce(str(quote_id))
        else:
            raise PaymentRejected("payment is not bound to a quote (missing quoteId)", code="unbound")

        nonce = self._signed_nonce(payload)
        if not nonce:
            raise PaymentRejected("missing signed authorization.nonce", code="missing_nonce")
        if nonce.lower() != expected_nonce.lower():
            raise PaymentRejected(
                "nonce must equal SHA-256(quoteId); payment is not bound to this quote",
                code="wrong_nonce",
            )
        store = self.quotes
        if store is not None:
            if store.is_spent(nonce):
                raise PaymentRejected("nonce has already been spent (replay)", code="replay")
            store.spend(nonce)

        payer = self._payer(payload)
        return Verification(
            accepted=True,
            receipt_id=f"mock_{uuid4().hex}",
            network=BASE_SEPOLIA_CAIP2,
            asset=expected_asset,
            amount=str(amount),
            pay_to=expected,
            payer=payer,
            settled=False,
            mode="mock",
            verified_at=datetime.now(timezone.utc).isoformat(),
            detail="mock verification only; no funds moved; not submitted to Base Sepolia",
            quote_id=str(quote_id) if quote_id else None,
            nonce=nonce,
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

    def _authorization(self, payload: dict[str, Any]) -> dict[str, Any]:
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = (inner or {}).get("authorization") if isinstance((inner or {}).get("authorization"), dict) else {}
        return auth or {}

    def _signed_asset(self, payload: dict[str, Any]) -> str:
        # Prefer a signed / inner asset; do not let a conflicting top-level
        # unsigned asset through if an inner one is present. If only a
        # top-level asset is present (legacy mock helper), accept it as
        # the asset identity — assets are not an authorization field in
        # the EIP-3009 shape, they are the token contract.
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        auth = self._authorization(payload)
        raw = (inner or {}).get("asset") or auth.get("asset") or self.asset
        return str(raw)

    def _signed_pay_to(self, payload: dict[str, Any]) -> str:
        # ONLY authorization.to. Top-level payTo is ignored.
        return str(self._authorization(payload).get("to") or "")

    def _signed_amount(self, payload: dict[str, Any]) -> int:
        # ONLY authorization.value. Top-level amount / value are ignored.
        raw = self._authorization(payload).get("value") or ""
        try:
            return int(str(raw))
        except (TypeError, ValueError) as exc:
            raise PaymentRejected(
                "signed authorization.value is missing or not an integer "
                "(top-level amount is ignored)",
                code="bad_amount",
            ) from exc

    def _signed_nonce(self, payload: dict[str, Any]) -> str:
        raw = self._authorization(payload).get("nonce") or ""
        return str(raw).strip()

    def _check_validity_window(self, payload: dict[str, Any], now: float) -> None:
        auth = self._authorization(payload)
        after = _as_int(auth.get("validAfter"), default=0)
        before = _as_int(auth.get("validBefore"), default=None)
        if after and now < after:
            raise PaymentRejected("authorization is not yet valid (validAfter)", code="not_yet_valid")
        if before is not None and now >= before:
            raise PaymentRejected("authorization has expired (validBefore)", code="expired")

    def _payer(self, payload: dict[str, Any]) -> str | None:
        raw = self._authorization(payload).get("from")
        if not raw:
            return None
        try:
            return canonicalize_address(str(raw))
        except ValueError:
            return str(raw)


def _as_int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise PaymentRejected(f"validity window field is not an integer: {value!r}", code="bad_window") from exc
