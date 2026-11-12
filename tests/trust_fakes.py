"""In-memory fakes of the health contracts, for bot tests.

Deliberately simple and independent of muse/trust/vault.py, mandates.py and
receipts.py, which are built in parallel. They follow the Protocols in
muse/trust/contracts.py exactly.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from dataclasses import replace
from typing import Any, Callable

from muse.trust.contracts import (
    Action,
    Decision,
    Item,
    LLMRefused,
    LLMResult,
    LLMUnavailable,
    Mandate,
    Receipt,
    Record,
    VaultLocked,
    VerifyResult,
)

NOW = "2026-11-12T00:00:00Z"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _terms(text: str) -> set[str]:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9.]{2,}", text)}
    cjk = "".join(re.findall(r"[一-鿿]+", text))
    return words | {cjk[i : i + 2] for i in range(len(cjk) - 1)}


class FakeVault:
    def __init__(self) -> None:
        self._unlocked = True
        self.records: dict[str, tuple[Record, str]] = {}
        self.items: dict[str, Item] = {}
        self._ids = itertools.count(1)
        self.reads: list[str] = []          # every get/search/list call, for "no read" assertions
        self.writes: list[str] = []

    def is_unlocked(self) -> bool:
        return self._unlocked

    def unlock(self, passphrase: str) -> None:
        self._unlocked = True

    def lock(self) -> None:
        self._unlocked = False

    def _need(self) -> None:
        if not self._unlocked:
            raise VaultLocked("vault is locked")

    def add(self, kind: str, title: str, text: str, occurred_on: str | None = None, source: str = "", record_id: str | None = None) -> Record:
        self._need()
        rid = record_id or f"r{next(self._ids):05d}"
        rec = Record(rid, kind, title, occurred_on, source, NOW, len(text), _sha(text))
        self.records[rid] = (rec, text)
        return rec

    def get(self, record_id: str) -> tuple[Record, str]:
        self._need()
        self.reads.append(f"get:{record_id}")
        if record_id not in self.records:
            raise KeyError(record_id)
        return self.records[record_id]

    def list(self, kind: str | None = None, since: str | None = None, until: str | None = None, limit: int = 200) -> list[Record]:
        self._need()
        self.reads.append("list")
        out = [r for r, _ in self.records.values() if kind is None or r.kind == kind]
        if since:
            out = [r for r in out if (r.occurred_on or "") >= since]
        if until:
            out = [r for r in out if (r.occurred_on or "") <= until]
        return sorted(out, key=lambda r: r.occurred_on or "")[:limit]

    def search(self, query: str, limit: int = 8) -> list[tuple[Record, str]]:
        self._need()
        self.reads.append("search")
        q = _terms(query)
        scored = []
        for rec, text in self.records.values():
            score = len(q & _terms(text + " " + rec.title))
            if score:
                scored.append((score, rec.occurred_on or "", rec, text[:200]))
        scored.sort(key=lambda t: (-t[0], t[1]), reverse=False)
        return [(rec, snip) for _, _, rec, snip in scored[:limit]]

    def delete(self, record_id: str) -> None:
        self._need()
        self.writes.append(f"delete:{record_id}")
        self.records.pop(record_id, None)

    def add_item(self, kind: str, data: dict[str, Any]) -> Item:
        self._need()
        iid = f"i{next(self._ids):05d}"
        item = Item(iid, kind, dict(data), NOW, NOW)
        self.items[iid] = item
        self.writes.append(f"add_item:{kind}")
        return item

    def list_items(self, kind: str | None = None) -> list[Item]:
        self._need()
        self.reads.append("list_items")
        return [i for i in self.items.values() if kind is None or i.kind == kind]

    def update_item(self, item_id: str, data: dict[str, Any]) -> Item:
        self._need()
        item = replace(self.items[item_id], data={**self.items[item_id].data, **data})
        self.items[item_id] = item
        self.writes.append(f"update_item:{item_id}")
        return item

    def delete_item(self, item_id: str) -> None:
        self._need()
        del self.items[item_id]
        self.writes.append(f"delete_item:{item_id}")

    def sign(self, message: bytes) -> bytes:
        return hashlib.sha256(b"k" + message).digest()

    def public_key(self) -> bytes:
        return b"\x00" * 32

    def export_archive(self, dest: str) -> None:
        raise NotImplementedError


class FakeMandateBook:
    def __init__(self) -> None:
        self.mandates: dict[str, Mandate] = {}
        self._ids = itertools.count(1)
        self.checks: list[tuple[str, Action]] = []

    def grant(self, grantee: str, scopes: tuple[str, ...], purpose: str, record_kinds: tuple[str, ...] = (), expires_at: str | None = None) -> Mandate:
        mid = f"m{next(self._ids)}"
        m = Mandate(mid, grantee, tuple(scopes), purpose, tuple(record_kinds), NOW, expires_at, None, "sig")
        self.mandates[mid] = m
        return m

    def revoke(self, mandate_id: str) -> None:
        self.mandates[mandate_id] = replace(self.mandates[mandate_id], revoked_at=NOW)

    def all(self) -> list[Mandate]:
        return list(self.mandates.values())

    def check(self, grantee: str, action: Action) -> Decision:
        self.checks.append((grantee, action))
        for m in self.mandates.values():
            if m.grantee != grantee or m.revoked_at or action.scope not in m.scopes:
                continue
            if m.record_kinds and not set(action.record_kinds) <= set(m.record_kinds):
                continue
            return Decision(True, m.id, "allowed")
        return Decision(False, None, f"You have not allowed the assistant to do this ({action.scope}).")


class FakeReceiptLog:
    def __init__(self) -> None:
        self.receipts: list[Receipt] = []

    def append(self, actor: str, action: str, *, mandate_id: str | None = None, record_refs: tuple[str, ...] = (), egress: str = "none",
               model: str | None = None, privacy: str | None = None, detail: str = "") -> Receipt:
        prev = self.receipts[-1].hash if self.receipts else "0" * 64
        seq = len(self.receipts) + 1
        body = json.dumps([seq, actor, action, mandate_id, list(record_refs), egress, model, privacy, detail, prev])
        rec = Receipt(seq, NOW, actor, action, mandate_id, tuple(record_refs), egress, model, privacy, detail, prev, _sha(body), "sig")
        self.receipts.append(rec)
        return rec

    def list(self, limit: int = 50, before_seq: int | None = None) -> list[Receipt]:
        out = [r for r in self.receipts if before_seq is None or r.seq < before_seq]
        return list(reversed(out))[:limit]

    def verify(self) -> VerifyResult:
        return VerifyResult(True, len(self.receipts), None, "ok")


class ScriptedLLM:
    """Returns scripted answers in order (or from a function of the messages); records every call."""

    def __init__(self, *answers: str | Exception | Callable[[list[dict[str, str]]], str], provider: str = "local_openai",
                 deployment: str = "local-model__smart", privacy: str | None = None) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.provider = provider
        self.deployment = deployment
        self.privacy = privacy

    def complete(self, messages: list[dict[str, str]], *, privacy: str, alias: str = "smart", max_tokens: int = 800) -> LLMResult:
        self.calls.append({"messages": messages, "privacy": privacy, "alias": alias})
        answer = self.answers.pop(0) if len(self.answers) > 1 else (self.answers[0] if self.answers else "ok")
        if isinstance(answer, Exception):
            raise answer
        text = answer(messages) if callable(answer) else answer
        return LLMResult(text, self.provider, self.deployment, self.privacy or privacy, 10, 5)


class EchoLLM(ScriptedLLM):
    """Answers with the last user message: handy for checking what was sent."""

    def __init__(self) -> None:
        super().__init__(lambda messages: messages[-1]["content"])


class RaisingLLM(ScriptedLLM):
    def __init__(self, exc: Exception | None = None) -> None:
        super().__init__(exc or LLMUnavailable("down"))


def refused() -> LLMRefused:
    return LLMRefused("[muse] no deployment of 'smart' may receive a 'local' request")
