"""Records: the signed, hash-chained entries of one device (format v1).

The format is specified in docs/spec/interfaces/record-format.md; this module
is its Python implementation and core/src/records.rs is the Rust one. Both are
held to core/conformance/records.json. Everything here is pure: no clock, no
files, no randomness (Ed25519 signatures are deterministic).

A record is a plain dict in exactly the JSON shape of the format. `append`
makes one from an entry; `verify_record` and `verify_chain` check them.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Any, Iterable, Mapping, Protocol

from muse.records.errors import RecordError
from muse.records.jcs import MAX_INT, canonicalize

FORMAT_VERSION = 1
TIME_TOLERANCE_MS = 120_000
MAX_SUMMARY_CHARS = 200
MAX_RESOURCE_CHARS = 1024

ACTORS = ("person", "helper", "system")
OUTCOMES = ("ok", "declined", "failed", "undone")
LINK_KEYS = ("job", "card", "undo_of", "payment_ref")

D_SIG = b"muse/record/v1/sig\x00"
D_HASH = b"muse/record/v1/hash\x00"
D_GENESIS = b"muse/record/v1/genesis\x00"
D_RESOURCE = b"muse/record/v1/resource\x00"
D_SALT = b"muse/record/v1/summary-salt\x00"
D_SUMMARY = b"muse/record/v1/summary\x00"

SIGNED_FIELDS = ("v", "device", "seq", "time", "mandate", "actor", "action", "resource", "summary_hash", "outcome",
                 "cost", "prev")
SIGNED_OPTIONAL = ("links", "egress")  # signed when present; a record without one has the bytes it always had
REQUIRED_FIELDS = SIGNED_FIELDS + ("sig",)
OPTIONAL_FIELDS = SIGNED_OPTIONAL + ("summary", "summary_salt")
ENTRY_REQUIRED = ("time", "actor", "action", "outcome", "summary")
ENTRY_OPTIONAL = ("mandate", "resource", "cost", "links", "egress")
EGRESS_LEFT = ("service:", "host:")  # the values that mean the content left the person's devices

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX128 = re.compile(r"[0-9a-f]{128}\Z")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ACTION = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}\Z")
_CURRENCY = re.compile(r"[A-Z0-9]{3,8}\Z")
_EGRESS = re.compile(r"(?:none|home|(?:service|host):[a-z0-9][a-z0-9._-]{0,63})\Z")

# Summary rule, part 3 (append only): text that looks like a credential.
KEY_PREFIXES = ("sk-", "sk_", "pk_", "rk_", "gsk_", "csk-", "nvapi-", "jina_", "hf_", "glpat-", "ghp_", "gho_", "ghu_",
                "ghs_", "ghr_", "github_pat_", "xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-", "AKIA", "ASIA", "AIza", "eyJ")
KEY_PREFIX_MIN = 20
KEY_RUN_MIN = 32
_TOKEN = re.compile(r"[A-Za-z0-9_+=-]+")


# ---------------------------------------------------------------- keys


class Signer(Protocol):
    """What signs for a device. The home's key file implements it today; the vault will later."""

    device: str  # lower-case hex of the 32-byte Ed25519 public key

    def sign(self, message: bytes) -> bytes: ...  # 64-byte Ed25519 signature


class RefKey(Protocol):
    """The per-vault reference key, used only through HMAC-SHA-256."""

    def mac(self, message: bytes) -> bytes: ...


class SeedSigner:
    """A Signer from a 32-byte Ed25519 seed held in memory."""

    def __init__(self, seed: bytes) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise RecordError("malformed", detail="an Ed25519 seed is 32 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(bytes(seed))
        public = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.device = public.hex()

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


class HmacKey:
    """A RefKey from key bytes held in memory."""

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
            raise RecordError("malformed", detail="a reference key is at least 16 bytes")
        self._key = bytes(key)

    def mac(self, message: bytes) -> bytes:
        return hmac.new(self._key, message, hashlib.sha256).digest()


def _signer(value: Signer | bytes) -> Signer:
    return SeedSigner(value) if isinstance(value, (bytes, bytearray)) else value


def _ref(value: RefKey | bytes) -> RefKey:
    return HmacKey(value) if isinstance(value, (bytes, bytearray)) else value


def _verify_sig(device: str, message: bytes, sig_hex: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(device)).verify(bytes.fromhex(sig_hex), message)
    except (InvalidSignature, ValueError):
        return False
    return True


# ---------------------------------------------------------------- text rules


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INT


def _is(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.match(value) is not None


def _bad_char(ch: str) -> bool:
    code = ord(ch)
    return (code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029) or 0x202A <= code <= 0x202E
            or 0x2066 <= code <= 0x2069 or 0xD800 <= code <= 0xDFFF)


def _summary_shape_ok(summary: Any) -> bool:
    """Parts 1 and 2 of the summary rule: what every reader checks."""
    return isinstance(summary, str) and 1 <= len(summary) <= MAX_SUMMARY_CHARS and not any(_bad_char(ch) for ch in summary)


def key_shaped(text: str) -> bool:
    """Part 3 of the summary rule: does `text` hold something shaped like a credential?

    A token is a longest run of ASCII letters, digits and `_ + = -`. The text is
    key-shaped when it contains `-----BEGIN`, or a token of 20 or more characters
    that starts with a known key prefix, or a token with a piece (between
    hyphens) of 32 or more characters holding both a letter and a digit.
    """
    if "-----BEGIN" in text:
        return True
    for token in _TOKEN.findall(text):
        if len(token) >= KEY_PREFIX_MIN and token.startswith(KEY_PREFIXES):
            return True
        for piece in token.split("-"):
            if len(piece) >= KEY_RUN_MIN and any(c.isdigit() for c in piece) and any(c.isalpha() for c in piece):
                return True
    return False


def check_summary(summary: Any) -> str:
    """The summary as it will be signed (NFC), or RecordError("summary")."""
    if not isinstance(summary, str) or any(0xD800 <= ord(ch) <= 0xDFFF for ch in summary):
        raise RecordError("summary", detail="a summary is text")
    text = unicodedata.normalize("NFC", summary)
    if not _summary_shape_ok(text):
        raise RecordError("summary", detail=f"1 to {MAX_SUMMARY_CHARS} characters, no control characters")
    if key_shaped(text):
        raise RecordError("summary", detail="looks like it contains a key or token")
    return text


# ---------------------------------------------------------------- references and commitments


def resource_ref(ref_key: RefKey | bytes, resource: str) -> str:
    """The keyed reference stored in `resource`: HMAC-SHA-256 over the NFC resource string."""
    if not isinstance(resource, str) or not 1 <= len(resource) <= MAX_RESOURCE_CHARS:
        raise RecordError("malformed", detail="a resource is 1 to 1024 characters of text")
    try:
        data = unicodedata.normalize("NFC", resource).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RecordError("malformed", detail="a lone surrogate is not text") from exc
    return _ref(ref_key).mac(D_RESOURCE + data).hex()


def summary_salt(ref_key: RefKey | bytes, device: str, seq: int) -> str:
    """The 16-byte salt of a record's summary commitment, derived so that `append` needs no randomness."""
    if not _is(_HEX64, device) or not _is_int(seq):
        raise RecordError("malformed")
    return _ref(ref_key).mac(D_SALT + device.encode("ascii") + b"\x00" + str(seq).encode("ascii"))[:16].hex()


def summary_hash(salt: str, summary: str) -> str:
    """What a record signs in place of its summary: SHA-256(domain || salt || UTF-8 summary)."""
    if not _is(_HEX32, salt) or not isinstance(summary, str):
        raise RecordError("malformed")
    try:
        data = summary.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RecordError("malformed", detail="a lone surrogate is not text") from exc
    return hashlib.sha256(D_SUMMARY + bytes.fromhex(salt) + data).hexdigest()


def genesis(device: str) -> str:
    """`prev` of a device's first record (seq 0)."""
    if not _is(_HEX64, device):
        raise RecordError("malformed")
    return hashlib.sha256(D_GENESIS + bytes.fromhex(device)).hexdigest()


# ---------------------------------------------------------------- shape


def _check_cost(cost: Any) -> None:
    if cost is None:
        return
    if not isinstance(cost, dict) or set(cost) != {"currency", "minor"}:
        raise RecordError("malformed", detail="cost is null or {currency, minor}")
    if not _is(_CURRENCY, cost["currency"]) or not _is_int(cost["minor"]):
        raise RecordError("malformed", detail="cost.currency is a code, cost.minor a non-negative integer")


def _check_links(links: Any) -> None:
    if not isinstance(links, dict) or not links or not set(links) <= set(LINK_KEYS):
        raise RecordError("malformed", detail="links is a non-empty object of job, card, undo_of, payment_ref")
    for key, value in links.items():
        if not _is(_HEX64 if key == "undo_of" else _ID, value):
            raise RecordError("malformed", detail=f"links.{key}")


def _check_common(obj: Mapping[str, Any]) -> None:
    """The fields an entry and a record share."""
    if not _is_int(obj["time"]):
        raise RecordError("malformed", detail="time is integer milliseconds")
    if obj["actor"] not in ACTORS or not isinstance(obj["actor"], str):
        raise RecordError("malformed", detail="actor")
    if not _is(_ACTION, obj["action"]):
        raise RecordError("malformed", detail="action is a dotted lower-case name")
    if obj["outcome"] not in OUTCOMES or not isinstance(obj["outcome"], str):
        raise RecordError("malformed", detail="outcome")
    if obj.get("mandate") is not None and not _is(_ID, obj["mandate"]):
        raise RecordError("malformed", detail="mandate")
    _check_cost(obj.get("cost"))
    if "links" in obj:
        _check_links(obj["links"])
    if "egress" in obj and not _is(_EGRESS, obj["egress"]):
        raise RecordError("malformed", detail="egress is none, home, service:<id> or host:<name>")


def check_record(record: Any) -> None:
    """RecordError("malformed") unless `record` has exactly the shape and value ranges of format v1.

    This is shape only: it says nothing about the signature or the chain."""
    if not isinstance(record, dict):
        raise RecordError("malformed", detail="a record is an object")
    keys = set(record)
    if not set(REQUIRED_FIELDS) <= keys or not keys <= set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS):
        raise RecordError("malformed", detail="missing or unknown fields")
    if record["v"] != FORMAT_VERSION or isinstance(record["v"], bool) or not isinstance(record["v"], int):
        raise RecordError("malformed", detail="unknown format version")
    if not _is(_HEX64, record["device"]) or not _is_int(record["seq"]):
        raise RecordError("malformed", detail="device or seq")
    _check_common(record)
    if record["resource"] is not None and not _is(_HEX64, record["resource"]):
        raise RecordError("malformed", detail="resource is null or a keyed reference")
    if not _is(_HEX64, record["summary_hash"]) or not _is(_HEX64, record["prev"]) or not _is(_HEX128, record["sig"]):
        raise RecordError("malformed", detail="summary_hash, prev or sig")
    if ("summary" in record) != ("summary_salt" in record):
        raise RecordError("malformed", detail="summary and summary_salt come together")
    if "summary" in record and (not _summary_shape_ok(record["summary"]) or not _is(_HEX32, record["summary_salt"])):
        raise RecordError("malformed", detail="summary or summary_salt")


# ---------------------------------------------------------------- bytes, hash, signature


def canonical_bytes(record: Mapping[str, Any], with_sig: bool = False) -> bytes:
    """The canonical (RFC 8785) bytes of a record's signed fields; with `sig` too when `with_sig`.

    `summary` and `summary_salt` are never part of them: the record signs `summary_hash`."""
    try:
        obj = {name: record[name] for name in SIGNED_FIELDS}
        for name in SIGNED_OPTIONAL:
            if name in record:
                obj[name] = record[name]
        if with_sig:
            obj["sig"] = record["sig"]
    except (KeyError, TypeError) as exc:
        raise RecordError("malformed", detail="missing fields") from exc
    return canonicalize(obj)


def signing_digest(record: Mapping[str, Any]) -> bytes:
    """The 32 bytes Ed25519 signs."""
    return hashlib.sha256(D_SIG + canonical_bytes(record)).digest()


def record_hash(record: Mapping[str, Any]) -> str:
    """The record's hash: what the next record's `prev` holds and what the Merkle tree is built over.
    It covers the signature, so it names one signed record and nothing else."""
    return hashlib.sha256(D_HASH + canonical_bytes(record, with_sig=True)).hexdigest()


def sign(unsigned: Mapping[str, Any], signer: Signer | bytes) -> dict[str, Any]:
    """`unsigned` (a record without `sig`) with its signature added."""
    signer = _signer(signer)
    if unsigned.get("device") != signer.device:
        raise RecordError("wrong_device", detail="the record names another device than the signer")
    record = dict(unsigned)
    record.pop("sig", None)
    record["sig"] = signer.sign(signing_digest(record)).hex()
    check_record(record)
    return record


def _verify_checked(record: Mapping[str, Any], device: str | None) -> None:
    if device is not None and record["device"] != device:
        raise RecordError("wrong_device")
    if not _verify_sig(record["device"], signing_digest(record), record["sig"]):
        raise RecordError("bad_signature")
    if "summary" in record and not hmac.compare_digest(summary_hash(record["summary_salt"], record["summary"]), record["summary_hash"]):
        raise RecordError("summary_mismatch")


def verify_record(record: Any, device: str | None = None) -> None:
    """One record on its own: shape, device (when given), signature, and the summary commitment.

    Raises RecordError with kind malformed, wrong_device, bad_signature or summary_mismatch."""
    check_record(record)
    _verify_checked(record, device)


def redact(record: Mapping[str, Any]) -> dict[str, Any]:
    """The record without its summary. Still verifiable: the signature covers `summary_hash`."""
    return {key: value for key, value in record.items() if key not in ("summary", "summary_salt")}


# ---------------------------------------------------------------- chain


class ChainVerifier:
    """Checks a chain one record at a time, from seq 0. `push` raises RecordError (with the index) at the first problem."""

    def __init__(self, device: str | None = None) -> None:
        self.device = device
        self.hashes: list[str] = []
        self._prev_time = 0

    @property
    def size(self) -> int:
        return len(self.hashes)

    def push(self, record: Any) -> None:
        index = len(self.hashes)
        try:
            check_record(record)
            if self.device is None:
                self.device = record["device"]
            _verify_checked(record, self.device)
            this = record_hash(record)
            seq = record["seq"]
            if seq > index:
                raise RecordError("missing")
            if seq < index:
                raise RecordError("duplicate" if hmac.compare_digest(self.hashes[seq], this) else "fork")
            if record["prev"] != (self.hashes[-1] if index else genesis(self.device)):
                raise RecordError("wrong_prev")
            if index and record["time"] < self._prev_time - TIME_TOLERANCE_MS:
                raise RecordError("time_went_back")
        except RecordError as exc:
            raise RecordError(exc.kind, index, exc.detail) from None
        self.hashes.append(this)
        self._prev_time = record["time"]


def verify_chain(records: Iterable[Any], device: str | None = None) -> int:
    """The number of records when the whole chain checks out; RecordError(kind, index) for the first problem.

    Per record, in this order: shape (malformed); device (wrong_device); signature
    (bad_signature); summary commitment (summary_mismatch); sequence (missing when a
    number is skipped, duplicate or fork when one comes again); link to the record
    before (wrong_prev); time (time_went_back)."""
    verifier = ChainVerifier(device)
    for record in records:
        verifier.push(record)
    return verifier.size


def append(head: Mapping[str, Any] | None, entry: Mapping[str, Any], signer: Signer | bytes, ref_key: RefKey | bytes) -> dict[str, Any]:
    """The signed record that follows `head` (None for a device's first record). Pure.

    `entry` has time, actor, action, outcome, summary, and optionally mandate,
    resource (the plain canonical resource string: only its keyed reference is
    stored), cost and links."""
    signer, ref_key = _signer(signer), _ref(ref_key)
    if not isinstance(entry, Mapping) or not set(ENTRY_REQUIRED) <= set(entry) or not set(entry) <= set(ENTRY_REQUIRED) | set(ENTRY_OPTIONAL):
        raise RecordError("malformed", detail="missing or unknown entry fields")
    _check_common(entry)
    summary = check_summary(entry["summary"])
    device = signer.device
    if head is None:
        seq, prev = 0, genesis(device)
    else:
        check_record(head)
        if head["device"] != device:
            raise RecordError("wrong_device", detail="the head belongs to another device")
        if head["seq"] >= MAX_INT:
            raise RecordError("malformed", detail="the chain is full")
        if entry["time"] < head["time"] - TIME_TOLERANCE_MS:
            raise RecordError("time_went_back")
        seq, prev = head["seq"] + 1, record_hash(head)
    salt = summary_salt(ref_key, device, seq)
    record: dict[str, Any] = {
        "v": FORMAT_VERSION, "device": device, "seq": seq, "time": entry["time"], "mandate": entry.get("mandate"),
        "actor": entry["actor"], "action": entry["action"],
        "resource": None if entry.get("resource") is None else resource_ref(ref_key, entry["resource"]),
        "summary_hash": summary_hash(salt, summary), "outcome": entry["outcome"],
        "cost": None if entry.get("cost") is None else dict(entry["cost"]), "prev": prev,
        "summary": summary, "summary_salt": salt,
    }
    if "links" in entry:
        record["links"] = dict(entry["links"])
    if "egress" in entry:
        record["egress"] = entry["egress"]
    return sign(record, signer)


def left_devices(record: Mapping[str, Any]) -> bool:
    """Did the content of this action leave the person's devices, by the record's own statement?

    True for `service:...` and `host:...`. False for `none`, `home`, and for a record that does not say."""
    egress = record.get("egress")
    return isinstance(egress, str) and egress.startswith(EGRESS_LEFT)
