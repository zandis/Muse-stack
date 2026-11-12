"""Issued x402 quotes: id, expiry, and a nonce bound to the quote id.

``nonce = 0x`` + SHA-256(quoteId). A payment is accepted only when its
*signed* authorization nonce equals this value, the quote has not
expired, and the nonce has not already been spent. This binds a payment
to one result and prevents replay across results or over time.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def quote_nonce(quote_id: str) -> str:
    return "0x" + hashlib.sha256(quote_id.encode("utf-8")).hexdigest()


@dataclass
class Quote:
    quote_id: str
    result_id: str
    amount: str
    pay_to: str
    asset: str
    network: str
    expires_at: float  # unix seconds (wall)
    nonce: str
    issued_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "quoteId": self.quote_id,
            "resultId": self.result_id,
            "amount": self.amount,
            "payTo": self.pay_to,
            "asset": self.asset,
            "network": self.network,
            "expiresAt": self.expires_at,
            "nonce": self.nonce,
            "issuedAt": self.issued_at,
        }


class QuoteStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quotes: dict[str, Quote] = {}
        self._spent_nonces: set[str] = set()

    def issue(
        self,
        *,
        result_id: str,
        amount: str,
        pay_to: str,
        asset: str,
        network: str,
        ttl_seconds: float,
        now_wall: float,
    ) -> Quote:
        quote_id = uuid4().hex
        quote = Quote(
            quote_id=quote_id,
            result_id=result_id,
            amount=str(amount),
            pay_to=pay_to,
            asset=asset,
            network=network,
            expires_at=now_wall + max(1.0, float(ttl_seconds)),
            nonce=quote_nonce(quote_id),
            issued_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._quotes[quote_id] = quote
        return quote

    def get(self, quote_id: str) -> Quote | None:
        with self._lock:
            return self._quotes.get(quote_id)

    def is_spent(self, nonce: str) -> bool:
        with self._lock:
            return nonce.lower() in self._spent_nonces

    def spend(self, nonce: str) -> None:
        with self._lock:
            self._spent_nonces.add(nonce.lower())
