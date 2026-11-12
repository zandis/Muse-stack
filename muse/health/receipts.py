"""LocalReceiptLog: a signed, hash-chained log of everything done to the vault.

Each receipt's hash is SHA-256 over canonical JSON of every field except
hash and signature (seq and prev_hash included), plus a domain tag and the
vault id so a receipt cannot be transplanted into another vault's chain.
The signature is Ed25519 (vault key, domain "receipt") over the 32 hash bytes.

A signed head pointer (newest seq + hash) is updated in the same transaction
as each append, so deleting the newest receipts (truncating the tail) is
detected. Residual risk: an attacker who replaces the whole file with an older
copy (rows and head consistent with each other) cannot be detected from inside
the file. The mitigation, not built here, is to anchor the head hash somewhere
the attacker does not control (the user's phone, a tenant transparency log,
a printed receipt) and compare.

Readable and verifiable while the vault is locked (receipts hold no content
and the public key is in the header); appending needs the unlocked key.

Decision on `detail` and refs: receipts are stored in clear so they can be
listed while locked. Therefore
- `detail` is capped at MAX_DETAIL_CHARS, may not contain control characters
  and is rejected if it contains any record title of the vault (4+ chars);
- record refs are not stored as the plain content SHA-256 (an unkeyed hash of
  a short lab result is guessable) but as HMAC(index key, sha256): stable per
  record, meaningless without the passphrase. vault.ref_token(sha) maps one
  to the other. Pass keyed_refs=False to store the plain hash as the contract
  literally says.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from muse.health import crypto
from muse.health.contracts import Receipt, VaultError, VerifyResult
from muse.health.vault import LocalVault, has_control_chars, iso_utc

RECEIPT_DOMAIN = "receipt"
HEAD_DOMAIN = "receipt-head"
GENESIS = "0" * 64
ACTORS = ("user", "bot", "system")
EGRESS = ("none", "local", "private", "cloud")
MAX_DETAIL_CHARS = 120
MIN_TITLE_MATCH = 4
MAX_REFS = 200
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SHORT_RE = re.compile(r"^[A-Za-z0-9._:/@+ ()-]{1,200}$")


def receipt_hash(vault_id: str, r: Receipt | dict[str, Any]) -> str:
    """SHA-256 over canonical JSON of all fields except hash and signature."""
    get = (lambda k: r[k]) if isinstance(r, dict) else (lambda k: getattr(r, k))
    obj = {
        "domain": "muse-health/receipt/v1",
        "vault_id": vault_id,
        "seq": int(get("seq")),
        "ts": get("ts"),
        "actor": get("actor"),
        "action": get("action"),
        "mandate_id": get("mandate_id"),
        "record_refs": list(get("record_refs")),
        "egress": get("egress"),
        "model": get("model"),
        "privacy": get("privacy"),
        "detail": get("detail"),
        "prev_hash": get("prev_hash"),
    }
    return crypto.sha256_hex(crypto.canonical_json(obj))


def _head_message(vault_id: str, seq: int, hash_: str) -> bytes:
    return crypto.canonical_json({"vault_id": vault_id, "seq": seq, "hash": hash_})


def verify_chain(vault_id: str, public_key: bytes, receipts: Iterable[Receipt], head: dict[str, Any] | None) -> VerifyResult:
    """Walk the chain from seq 1. Pure; used for the live log and for exports."""
    prev = GENESIS
    count = 0
    expected_seq = 1
    for r in receipts:
        if r.seq != expected_seq:
            return VerifyResult(False, count, expected_seq, f"receipt {expected_seq} is missing or out of order")
        if r.prev_hash != prev:
            return VerifyResult(False, count, r.seq, "chain link broken (a receipt was removed, reordered or changed)")
        if not crypto.ct_equal(receipt_hash(vault_id, r), r.hash):
            return VerifyResult(False, count, r.seq, "receipt content does not match its hash")
        try:
            sig = crypto.b64d(r.signature)
        except VaultError:
            sig = b""
        if not crypto.verify_signature(public_key, RECEIPT_DOMAIN, bytes.fromhex(r.hash), sig):
            return VerifyResult(False, count, r.seq, "receipt signature does not verify")
        prev = r.hash
        count += 1
        expected_seq += 1
    if head is None:
        if count:
            return VerifyResult(False, count, None, "the signed head is missing")
        return VerifyResult(True, 0, None, "empty log")
    try:
        hseq, hhash = int(head["seq"]), str(head["hash"])
        hsig = crypto.b64d(str(head["signature"]))
    except (KeyError, TypeError, ValueError, VaultError):
        return VerifyResult(False, count, None, "the signed head is malformed")
    if not crypto.verify_signature(public_key, HEAD_DOMAIN, _head_message(vault_id, hseq, hhash), hsig):
        return VerifyResult(False, count, None, "the signed head does not verify")
    if hseq != count or not crypto.ct_equal(hhash, prev):
        if hseq > count:
            return VerifyResult(False, count, count + 1, f"the log was truncated: head says {hseq} receipts, found {count}")
        return VerifyResult(False, count, None, "the log does not end at the signed head")
    return VerifyResult(True, count, None, f"{count} receipts verified")


def _row_to_receipt(row: Any) -> Receipt:
    return Receipt(
        seq=int(row["seq"]), ts=row["ts"], actor=row["actor"], action=row["action"], mandate_id=row["mandate_id"],
        record_refs=tuple(json.loads(row["record_refs"])), egress=row["egress"], model=row["model"],
        privacy=row["privacy"], detail=row["detail"], prev_hash=row["prev_hash"], hash=row["hash"],
        signature=row["signature"],
    )


class LocalReceiptLog:
    def __init__(self, vault: LocalVault, clock: Callable[[], float] | None = None, *, keyed_refs: bool = True) -> None:
        self.vault = vault
        self.clock = clock or vault.clock
        self.keyed_refs = keyed_refs

    # ------------------------------------------------------------ validation

    def _check_detail(self, detail: str) -> None:
        if not isinstance(detail, str):
            raise VaultError("detail must be text")
        if len(detail) > MAX_DETAIL_CHARS:
            raise VaultError(f"receipt detail is limited to {MAX_DETAIL_CHARS} characters and must not contain content")
        if has_control_chars(detail):
            raise VaultError("receipt detail may not contain control characters or line breaks")
        if detail:
            low = detail.casefold()
            for title in self.vault.titles():
                t = title.strip().casefold()
                if len(t) >= MIN_TITLE_MATCH and t in low:
                    raise VaultError("receipt detail appears to contain a record title; receipts must not hold content")

    def ref_for(self, content_sha256: str) -> str:
        """The value stored in record_refs for a record's content hash."""
        if not isinstance(content_sha256, str) or not _HEX64.match(content_sha256):
            raise VaultError("record refs must be lowercase hex SHA-256")
        return self.vault.ref_token(content_sha256) if self.keyed_refs else content_sha256

    # ------------------------------------------------------------ ReceiptLog

    def append(self, actor: str, action: str, *, mandate_id: str | None = None, record_refs: tuple[str, ...] = (),
               egress: str = "none", model: str | None = None, privacy: str | None = None, detail: str = "") -> Receipt:
        self.vault._require_keys()
        if actor not in ACTORS:
            raise VaultError("actor must be user, bot or system")
        if not isinstance(action, str) or not _ACTION_RE.match(action):
            raise VaultError("malformed action name")
        if egress not in EGRESS:
            raise VaultError("unknown egress")
        if mandate_id is not None and (not isinstance(mandate_id, str) or not re.fullmatch(r"[0-9a-f]{32}", mandate_id)):
            raise VaultError("malformed mandate id")
        for value in (model, privacy):
            if value is not None and (not isinstance(value, str) or not _SHORT_RE.match(value)):
                raise VaultError("model/privacy must be short identifiers")
        if len(record_refs) > MAX_REFS:
            raise VaultError("too many record refs in one receipt")
        refs = tuple(self.ref_for(r) for r in record_refs)
        self._check_detail(detail)
        ts = iso_utc(self.clock())
        vault_id = self.vault.vault_id
        with self.vault._write() as conn:   # IMMEDIATE: appends are serialised across threads and processes
            last = conn.execute("SELECT seq, hash FROM receipts ORDER BY seq DESC LIMIT 1").fetchone()
            seq = int(last["seq"]) + 1 if last else 1
            prev = last["hash"] if last else GENESIS
            fields = {"seq": seq, "ts": ts, "actor": actor, "action": action, "mandate_id": mandate_id,
                      "record_refs": refs, "egress": egress, "model": model, "privacy": privacy,
                      "detail": detail, "prev_hash": prev}
            h = receipt_hash(vault_id, fields)
            sig = crypto.b64e(self.vault.sign_with_domain(RECEIPT_DOMAIN, bytes.fromhex(h)))
            conn.execute(
                "INSERT INTO receipts (seq, ts, actor, action, mandate_id, record_refs, egress, model, privacy, detail,"
                " prev_hash, hash, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (seq, ts, actor, action, mandate_id, json.dumps(list(refs)), egress, model, privacy, detail, prev, h, sig),
            )
            head_sig = self.vault.sign_with_domain(HEAD_DOMAIN, _head_message(vault_id, seq, h))
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('receipt_head', ?)",
                (json.dumps({"seq": seq, "hash": h, "signature": crypto.b64e(head_sig)}),),
            )
        return Receipt(signature=sig, hash=h, **{**fields, "record_refs": refs})

    def list(self, limit: int = 50, before_seq: int | None = None) -> list[Receipt]:
        """Newest first. Works while locked."""
        limit = max(0, min(int(limit), 10000))
        with self.vault._read() as conn:
            if before_seq is None:
                rows = conn.execute("SELECT * FROM receipts ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM receipts WHERE seq < ? ORDER BY seq DESC LIMIT ?", (int(before_seq), limit)).fetchall()
        return [_row_to_receipt(r) for r in rows]

    def _snapshot(self) -> tuple[list[Receipt], dict[str, Any] | None]:
        with self.vault._read() as conn:
            rows = conn.execute("SELECT * FROM receipts ORDER BY seq").fetchall()
            head_row = conn.execute("SELECT value FROM meta WHERE key='receipt_head'").fetchone()
        receipts = []
        for row in rows:
            try:
                receipts.append(_row_to_receipt(row))
            except (ValueError, TypeError):
                receipts.append(Receipt(int(row["seq"]), "", "", "", None, (), "", None, None, "", "", "", ""))
        head = None
        if head_row is not None:
            try:
                head = json.loads(head_row["value"])
            except ValueError:
                head = {}
        return receipts, head

    def verify(self) -> VerifyResult:
        """Whole chain + signed head, with the public key from the header. Works while locked."""
        receipts, head = self._snapshot()
        return verify_chain(self.vault.vault_id, self.vault.public_key(), receipts, head)

    def export_jsonl(self, path: str | os.PathLike[str]) -> None:
        """For an auditor: a header line (vault id, public key, signed head), then
        one receipt per line, oldest first. Refuses to overwrite."""
        receipts, head = self._snapshot()
        lines = [crypto.canonical_json({
            "type": "muse.receipts", "format": 1, "vault_id": self.vault.vault_id,
            "public_key": crypto.b64e(self.vault.public_key()), "exported_at": iso_utc(self.clock()), "head": head,
        })]
        for r in receipts:
            lines.append(crypto.canonical_json({**r.__dict__, "record_refs": list(r.record_refs)}))
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\n".join(lines) + b"\n")


MAX_EXPORT_BYTES = 512 * 1024 * 1024


def verify_export(path: str | os.PathLike[str], public_key: bytes) -> VerifyResult:
    """Standalone check of an export_jsonl file against a public key the
    auditor obtained independently (not the one written in the file)."""
    p = Path(path)
    try:
        if p.stat().st_size > MAX_EXPORT_BYTES:
            return VerifyResult(False, 0, None, "export is too large")
        lines = p.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return VerifyResult(False, 0, None, "export could not be read")
    if not lines:
        return VerifyResult(False, 0, None, "export is empty")
    try:
        header = json.loads(lines[0])
        if header.get("type") != "muse.receipts" or header.get("format") != 1:
            return VerifyResult(False, 0, None, "not a receipts export")
        vault_id = str(header["vault_id"])
        if not crypto.ct_equal(crypto.b64d(str(header["public_key"])), bytes(public_key)):
            return VerifyResult(False, 0, None, "export was made by a different key")
        receipts = []
        for line in lines[1:]:
            if not line.strip():
                continue
            obj = json.loads(line)
            obj["record_refs"] = tuple(obj["record_refs"])
            receipts.append(Receipt(**obj))
    except (ValueError, KeyError, TypeError, VaultError):
        return VerifyResult(False, 0, None, "export is malformed")
    return verify_chain(vault_id, bytes(public_key), receipts, header.get("head"))
