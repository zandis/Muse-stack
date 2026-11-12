"""In-memory (optionally file-backed) catalogue of verified results for sale.

A result is produced by the useful-work runner: a Lean-verified proof and its
hash. The settlement endpoint sells access to that record. The payload is the
proof and metadata; the chain, later, would only see the hash.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class ResultRecord:
    result_id: str
    result_hash: str
    problem_id: str
    title: str
    verdict: str
    verifier: str
    model: str
    proof: str
    statement: str
    price_atomic: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sold_count: int = 0
    last_receipt_id: str | None = None

    def public_view(self, *, include_proof: bool) -> dict[str, Any]:
        body = {
            "resultId": self.result_id,
            "resultHash": self.result_hash,
            "problemId": self.problem_id,
            "title": self.title,
            "verdict": self.verdict,
            "verifier": self.verifier,
            "model": self.model,
            "priceAtomic": self.price_atomic,
            "asset": "USDC",
            "network": "eip155:84532",
            "createdAt": self.created_at,
            "metadata": self.metadata,
        }
        if include_proof:
            body["proof"] = self.proof
            body["statement"] = self.statement
        else:
            body["proof"] = None
            body["statement"] = self.statement  # the statement is the quote, not the goods
            body["access"] = "payment_required"
        return body


class ResultStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._items: dict[str, ResultRecord] = {}
        if path and path.is_file():
            self._load(path)

    def put(self, record: ResultRecord) -> ResultRecord:
        with self._lock:
            self._items[record.result_id] = record
            self._flush()
            return record

    def add(
        self,
        *,
        result_hash: str,
        problem_id: str,
        title: str,
        verdict: str,
        verifier: str,
        model: str,
        proof: str,
        statement: str,
        price_atomic: int,
        metadata: dict[str, Any] | None = None,
        result_id: str | None = None,
    ) -> ResultRecord:
        record = ResultRecord(
            result_id=result_id or uuid4().hex,
            result_hash=result_hash,
            problem_id=problem_id,
            title=title,
            verdict=verdict,
            verifier=verifier,
            model=model,
            proof=proof,
            statement=statement,
            price_atomic=price_atomic,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=metadata or {},
        )
        return self.put(record)

    def get(self, result_id: str) -> ResultRecord | None:
        with self._lock:
            return self._items.get(result_id)

    def list(self) -> list[ResultRecord]:
        with self._lock:
            return list(self._items.values())

    def mark_sold(self, result_id: str, receipt_id: str) -> ResultRecord:
        with self._lock:
            record = self._items[result_id]
            record.sold_count += 1
            record.last_receipt_id = receipt_id
            self._flush()
            return record

    def _load(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("results", []):
            self._items[item["result_id"]] = ResultRecord(**item)

    def _flush(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"results": [asdict(item) for item in self._items.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
