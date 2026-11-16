"""Cryptographic building blocks for the health vault.

Small and boring on purpose: every primitive is a reviewed one from the
standard library or `cryptography` (scrypt, HKDF-SHA256, AES-256-GCM,
HMAC-SHA256, Ed25519). Nothing here is home-made except the glue.

Key hierarchy (see also docs in the report / module docstring of vault.py):

    passphrase --NFC, UTF-8--> scrypt(salt, n, r, p) --> KEK (32 bytes)
    KEK --AES-256-GCM(nonce, AAD=header)--> wraps a random 32-byte master key
    master --HKDF-SHA256(salt=vault_id, info=label)--> record key
                                                    --> index key (keyed hashes)
                                                    --> Ed25519 signing seed
                                                    --> archive MAC key

Changing the passphrase re-wraps the master key only; no record is touched.

AES-GCM with random 96-bit nonces is safe for about 2**32 encryptions per key
(NIST SP 800-38D, collision probability <= 2**-32). The vault counts its
encryptions and refuses to go past MAX_ENCRYPTIONS_PER_KEY, far below that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from wardmuse.trust.contracts import VaultError, WrongPassphrase

KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 32
TAG_BYTES = 16
# Refuse plaintexts above this; the vault's own limits are much lower.
MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024
# Random-nonce GCM bound is ~2**32 messages per key; stop well before it.
MAX_ENCRYPTIONS_PER_KEY = 2**31

MIN_PASSPHRASE_CHARS = 10

_SIG_PREFIX = b"muse-health/v1/sig/"
_HKDF_LABELS = {
    "record": b"muse-health/v1/record-key",
    "index": b"muse-health/v1/index-key",
    "signing": b"muse-health/v1/ed25519-seed",
    "archive": b"muse-health/v1/archive-mac-key",
}


class WeakPassphrase(VaultError):
    """The new passphrase fails the minimum policy."""


# ---------------------------------------------------------------- encoding


def canonical_json(obj: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 (not ASCII-escaped). Used for AAD,
    signatures and hashes, so it must be deterministic across installs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    """Strict base64 decode; VaultError instead of binascii noise."""
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, AttributeError) as exc:
        raise VaultError("malformed base64 field") from exc


def ct_equal(a: bytes | str, b: bytes | str) -> bool:
    """Constant-time comparison for MACs, hashes and key material."""
    if isinstance(a, str):
        a = a.encode("utf-8")
    if isinstance(b, str):
        b = b.encode("utf-8")
    return hmac.compare_digest(a, b)


def wipe(buf: bytearray | None) -> None:
    """Overwrite a mutable buffer in place. Only bytearrays can be wiped;
    immutable `bytes` copies made by Python or libraries cannot."""
    if buf is not None:
        buf[:] = bytes(len(buf))


# ---------------------------------------------------------------- KDF


@dataclass(frozen=True)
class KdfParams:
    """scrypt cost. Stored in the vault header so it can be raised later.

    Default n=2**17, r=8, p=1 (the OWASP 2023 recommendation): memory is
    128 * r * n = 128 MiB and an unlock takes roughly 0.3-1 s on a laptop,
    which is acceptable once per session and makes each offline guess cost
    the same. Tests pass a cheaper value explicitly (never via env vars).
    """

    n: int = 2**17
    r: int = 8
    p: int = 1

    def validate(self) -> None:
        n, r, p = self.n, self.r, self.p
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (n, r, p)):
            raise VaultError("KDF parameters must be integers")
        if n < 2**10 or n > 2**22 or n & (n - 1):
            raise VaultError("scrypt n must be a power of two between 2**10 and 2**22")
        if not 1 <= r <= 32 or not 1 <= p <= 16:
            raise VaultError("scrypt r or p out of range")
        if 128 * r * n > 2**30:
            raise VaultError("scrypt memory cost above 1 GiB")

    def memory_bytes(self) -> int:
        return 128 * self.r * self.n

    def to_dict(self) -> dict[str, Any]:
        return {"alg": "scrypt", "n": self.n, "r": self.r, "p": self.p}

    @classmethod
    def from_dict(cls, raw: Any) -> KdfParams:
        if not isinstance(raw, dict) or raw.get("alg") != "scrypt":
            raise VaultError("unsupported KDF in vault header")
        params = cls(n=raw.get("n"), r=raw.get("r"), p=raw.get("p"))  # type: ignore[arg-type]
        params.validate()
        return params


DEFAULT_KDF = KdfParams()
# Only for tests: ~1 MiB, milliseconds. Weak; never use for a real vault.
INSECURE_TEST_KDF = KdfParams(n=2**10, r=8, p=1)


def normalize_passphrase(passphrase: str) -> bytes:
    """NFC-normalise so the same Chinese passphrase typed on two devices
    (different IMEs may emit composed or decomposed forms) derives the same key."""
    if not isinstance(passphrase, str):
        raise VaultError("passphrase must be text")
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_kek(passphrase: str, salt: bytes, params: KdfParams) -> bytearray:
    params.validate()
    if len(salt) < 16:
        raise VaultError("KDF salt too short")
    maxmem = min(2**31 - 1, 2 * params.memory_bytes() + 2**24)
    key = hashlib.scrypt(normalize_passphrase(passphrase), salt=salt, n=params.n, r=params.r, p=params.p, maxmem=maxmem, dklen=KEY_BYTES)
    return bytearray(key)


# ---------------------------------------------------------------- AEAD


def random_key() -> bytearray:
    return bytearray(secrets.token_bytes(KEY_BYTES))


def wrap_key(kek: bytes | bytearray, master: bytes | bytearray, aad: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM key wrap with a random nonce; the header is the AAD."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(bytes(kek)).encrypt(nonce, bytes(master), aad)


def unwrap_key(kek: bytes | bytearray, nonce: bytes, wrapped: bytes, aad: bytes) -> bytearray:
    """Wrong passphrase and a forged header are indistinguishable here by
    design: the only thing reported is WrongPassphrase."""
    if len(nonce) != NONCE_BYTES or len(wrapped) != KEY_BYTES + TAG_BYTES:
        raise VaultError("vault header is corrupted")
    try:
        return bytearray(AESGCM(bytes(kek)).decrypt(nonce, wrapped, aad))
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase") from None


def record_aad(vault_id: str, table: str, row_id: str, field: str, schema_version: int) -> bytes:
    """Binds a ciphertext to exactly one vault, table, row and field, so a blob
    cannot be moved between records, fields or vaults without failing."""
    return canonical_json(["muse-health/v1/aad", vault_id, table, row_id, field, int(schema_version)])


def encrypt(key: bytes | bytearray, plaintext: bytes, aad: bytes) -> bytes:
    """nonce (12) || ciphertext || tag (16). Fresh random nonce every call."""
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise VaultError("plaintext too large to encrypt")
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(bytes(key)).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes | bytearray, blob: bytes, aad: bytes) -> bytes:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < NONCE_BYTES + TAG_BYTES:
        raise VaultError("ciphertext is truncated or corrupted")
    try:
        return AESGCM(bytes(key)).decrypt(bytes(blob[:NONCE_BYTES]), bytes(blob[NONCE_BYTES:]), aad)
    except InvalidTag:
        raise VaultError("ciphertext failed authentication (corrupted, swapped or from another vault)") from None


def bucket_size(n: int) -> int:
    """Padded size: powers of two from 256 B to 64 KiB, then 64 KiB steps.
    The bucket (not the exact length) is what an observer of the file learns."""
    size = 256
    while size < n and size < 65536:
        size *= 2
    if n <= size:
        return size
    return -(-n // 65536) * 65536


def pad(data: bytes) -> bytes:
    """4-byte length prefix, data, zero padding up to the bucket."""
    body = len(data).to_bytes(4, "big") + data
    return body + bytes(bucket_size(len(body)) - len(body))


def unpad(padded: bytes) -> bytes:
    if len(padded) < 4:
        raise VaultError("padded plaintext is corrupted")
    n = int.from_bytes(padded[:4], "big")
    if n > len(padded) - 4:
        raise VaultError("padded plaintext is corrupted")
    return padded[4 : 4 + n]


# ---------------------------------------------------------------- derived keys


def hkdf(master: bytes | bytearray, salt: bytes, info: bytes) -> bytearray:
    return bytearray(HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(bytes(master)))


class KeySet:
    """Sub-keys derived from the master key. Held in bytearrays so lock() can
    at least overwrite our own copies."""

    def __init__(self, master: bytes | bytearray, vault_id: str) -> None:
        salt = vault_id.encode("utf-8")
        self.record = hkdf(master, salt, _HKDF_LABELS["record"])
        self.index = hkdf(master, salt, _HKDF_LABELS["index"])
        self.signing_seed = hkdf(master, salt, _HKDF_LABELS["signing"])
        self.archive = hkdf(master, salt, _HKDF_LABELS["archive"])

    def public_key(self) -> bytes:
        return public_key_from_seed(self.signing_seed)

    def wipe(self) -> None:
        for buf in (self.record, self.index, self.signing_seed, self.archive):
            wipe(buf)


def keyed_hash(key: bytes | bytearray, label: str, data: bytes) -> str:
    """HMAC-SHA256 with a vault key and a purpose label; hex."""
    return hmac.new(bytes(key), label.encode("utf-8") + b"\x00" + data, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- signatures


def _sig_message(domain: str, message: bytes) -> bytes:
    """Domain separation: a signature made for one purpose (a receipt) can
    never be replayed as another (a mandate, or an app-level signature)."""
    if not domain or "\x00" in domain:
        raise VaultError("bad signature domain")
    return _SIG_PREFIX + domain.encode("utf-8") + b"\x00" + message


def public_key_from_seed(seed: bytes | bytearray) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(bytes(seed))
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def sign(seed: bytes | bytearray, domain: str, message: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(bytes(seed)).sign(_sig_message(domain, message))


def verify_signature(public_key: bytes, domain: str, message: bytes, signature: bytes) -> bool:
    """Pure function; False for any malformed input rather than an exception."""
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(bytes(public_key)).verify(bytes(signature), _sig_message(domain, message))
        return True
    except (InvalidSignature, ValueError, TypeError, VaultError):
        return False


# ---------------------------------------------------------------- passphrase policy


@dataclass(frozen=True)
class PassphraseCheck:
    ok: bool
    hint: str        # "too_short" | "weak" | "fair" | "strong"
    message: str     # one plain sentence for the user


def check_passphrase(passphrase: str) -> PassphraseCheck:
    """Minimum length 10 characters, nothing else is enforced.

    Why only length: composition rules and entropy estimators are tuned for
    Latin passwords and badly misjudge Chinese passphrases (each character
    carries much more than a letter) and word phrases; NIST SP 800-63B also
    advises against composition rules. Length is the one robust signal, and
    the scrypt cost multiplies whatever entropy the user picked. The hint is
    advisory.
    """
    if not isinstance(passphrase, str) or passphrase == "":
        return PassphraseCheck(False, "too_short", "The passphrase cannot be empty.")
    text = unicodedata.normalize("NFC", passphrase)
    n = len(text)
    if n < MIN_PASSPHRASE_CHARS:
        return PassphraseCheck(False, "too_short", f"Use at least {MIN_PASSPHRASE_CHARS} characters.")
    distinct = len(set(text))
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    if distinct <= 3 or text.isdigit():
        return PassphraseCheck(True, "weak", "Accepted, but it repeats few characters; a short sentence is much stronger.")
    if n >= 20 or cjk >= 8:
        return PassphraseCheck(True, "strong", "Strong. Nobody, including us, can recover it if you forget it.")
    if n >= 14:
        return PassphraseCheck(True, "fair", "Fair. A longer phrase would be stronger.")
    return PassphraseCheck(True, "weak", "Accepted, but a longer phrase of several words is much stronger.")


def require_passphrase(passphrase: str) -> None:
    result = check_passphrase(passphrase)
    if not result.ok:
        raise WeakPassphrase(result.message)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
