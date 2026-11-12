"""LocalVault: the user's health records, encrypted at rest in one SQLite file.

Threat model: the disk, backups and the hosting organisation's admins are
untrusted; only the user's passphrase opens the vault. See crypto.py for the
key hierarchy.

What is on disk in plaintext, and why:
- the header: format version, vault id, scrypt parameters and salt, the
  wrapped master key (AES-GCM, useless without the passphrase), the Ed25519
  public key (so receipts can be verified while locked) and created_at;
- opaque random row ids;
- the padded size bucket of each ciphertext (it is equal to the ciphertext
  length anyway): an observer learns "about 1 KiB" / "about 64 KiB", not
  the exact length;
- the day a record was added (YYYY-MM-DD), so a file reveals activity days;
- an HMAC of the record text under a vault key (dedupe), never a plain hash;
- mandate policy fields (grantee, scopes, kinds, times) and receipts, which by
  contract hold no content (receipts.py stores record refs keyed as well);
- counts: how many records, items, mandates and receipts exist.
Everything else (text, title, source, kind, occurred_on, the plain SHA-256,
item kind and data, mandate purpose) is AES-256-GCM encrypted with an AAD that
binds it to this vault, table, row and field.

SQLite notes: journal_mode=DELETE and short IMMEDIATE transactions on a fresh
connection per operation (same as muse/ledger.py), so a web server thread
and a CLI can share the file. secure_delete=ON makes SQLite overwrite deleted
content with zeros inside the database file; temp_store=MEMORY keeps sort
spills off disk. The rollback journal holds copies of pages (ciphertext only)
while a transaction runs and is then unlinked, not overwritten.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args

from muse import paths
from muse.health import crypto
from muse.health.contracts import (
    Item,
    ItemKind,
    Record,
    RecordKind,
    VaultError,
    VaultLocked,
)

FORMAT_VERSION = 1
SCHEMA_VERSION = 1           # record/field encoding version, part of every AAD
BUSY_TIMEOUT_MS = 10000

RECORD_KINDS: tuple[str, ...] = get_args(RecordKind)
ITEM_KINDS: tuple[str, ...] = get_args(ItemKind)

MAX_TEXT_BYTES = 2 * 1024 * 1024      # UTF-8 bytes of record text
MAX_TITLE_CHARS = 200
MAX_SOURCE_CHARS = 300
MAX_ITEM_BYTES = 64 * 1024
MAX_ITEM_DEPTH = 8
MAX_QUERY_CHARS = 200
# Search decrypts every record in memory. Fine for a person's records (a few
# thousand, tens of MB); beyond that it gets slow and a blind index is needed.
SEARCH_SOFT_LIMIT_RECORDS = 5000
ARCHIVE_MAGIC = b"MUSE-HEALTH-ARCHIVE/1\n"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS header (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    format_version INTEGER NOT NULL,
    vault_id TEXT NOT NULL,
    kdf TEXT NOT NULL,
    salt BLOB NOT NULL,
    wrap_nonce BLOB NOT NULL,
    wrapped_key BLOB NOT NULL,
    public_key BLOB NOT NULL,
    created_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    added_day TEXT NOT NULL,
    size_bucket INTEGER NOT NULL,
    content_mac TEXT NOT NULL,
    meta BLOB NOT NULL,
    body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    size_bucket INTEGER NOT NULL,
    body BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS mandates (
    id TEXT PRIMARY KEY,
    grantee TEXT NOT NULL,
    scopes TEXT NOT NULL,
    record_kinds TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    purpose BLOB NOT NULL,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    mandate_id TEXT,
    record_refs TEXT NOT NULL,
    egress TEXT NOT NULL,
    model TEXT,
    privacy TEXT,
    detail TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    signature TEXT NOT NULL
);
"""

# Column name -> (sqlite type, nullable). Used to validate archive imports.
_TABLES: dict[str, dict[str, tuple[str, bool]]] = {
    "header": {
        "id": ("int", False), "format_version": ("int", False), "vault_id": ("text", False), "kdf": ("text", False),
        "salt": ("blob", False), "wrap_nonce": ("blob", False), "wrapped_key": ("blob", False),
        "public_key": ("blob", False), "created_at": ("text", False), "checksum": ("text", False),
    },
    "meta": {"key": ("text", False), "value": ("text", False)},
    "records": {
        "id": ("text", False), "added_day": ("text", False), "size_bucket": ("int", False),
        "content_mac": ("text", False), "meta": ("blob", False), "body": ("blob", False),
    },
    "items": {"id": ("text", False), "size_bucket": ("int", False), "body": ("blob", False)},
    "mandates": {
        "id": ("text", False), "grantee": ("text", False), "scopes": ("text", False), "record_kinds": ("text", False),
        "created_at": ("text", False), "expires_at": ("text", True), "revoked_at": ("text", True),
        "purpose": ("blob", False), "signature": ("text", False),
    },
    "receipts": {
        "seq": ("int", False), "ts": ("text", False), "actor": ("text", False), "action": ("text", False),
        "mandate_id": ("text", True), "record_refs": ("text", False), "egress": ("text", False),
        "model": ("text", True), "privacy": ("text", True), "detail": ("text", False),
        "prev_hash": ("text", False), "hash": ("text", False), "signature": ("text", False),
    },
}
_META_KEYS = ("enc_count", "receipt_head", "mandate_head")

_init_lock = threading.Lock()
_initialised: set[str] = set()


# ---------------------------------------------------------------- small helpers


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> float:
    """Strict: YYYY-MM-DDTHH:MM:SS[.ffffff]Z only."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise VaultError("timestamps must be ISO-8601 UTC ending in Z")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    raise VaultError("timestamps must be ISO-8601 UTC ending in Z")


def valid_date(value: str) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def has_control_chars(text: str, allow_newlines: bool = False) -> bool:
    """Cc (C0/C1 controls) and Unicode bidi/format overrides that can disguise
    text in a UI (e.g. RLO). Newlines and tabs only where allowed."""
    for ch in text:
        if allow_newlines and ch in "\n\t\r":
            continue
        cat = unicodedata.category(ch)
        if cat == "Cc" or ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f":
            return True
    return False


def new_id() -> str:
    return secrets.token_hex(16)


def check_id(value: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise VaultError("malformed id")
    return value


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _json_depth(obj: Any, depth: int = 0) -> int:
    if depth > MAX_ITEM_DEPTH:
        return depth
    if isinstance(obj, dict):
        return max([_json_depth(v, depth + 1) for v in obj.values()] or [depth + 1])
    if isinstance(obj, list):
        return max([_json_depth(v, depth + 1) for v in obj] or [depth + 1])
    return depth


def default_vault_path() -> Path:
    directory = paths.home() / "health"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    return directory / "vault.sqlite"


def _header_aad(h: dict[str, Any]) -> bytes:
    return crypto.canonical_json({
        "domain": "muse-health/v1/header",
        "format": int(h["format_version"]),
        "vault_id": h["vault_id"],
        "kdf": json.loads(h["kdf"]),
        "salt": crypto.b64e(h["salt"]),
        "public_key": crypto.b64e(h["public_key"]),
        "created_at": h["created_at"],
    })


def _header_checksum(h: dict[str, Any]) -> str:
    """Unkeyed SHA-256 over the header. Detects accidental corruption so it can
    be reported as VaultError instead of a misleading WrongPassphrase; it is
    not a security control (an attacker can recompute it) and gives no oracle."""
    return crypto.sha256_hex(_header_aad(h) + h["wrap_nonce"] + h["wrapped_key"])


def _check_header(h: dict[str, Any]) -> None:
    try:
        if int(h["format_version"]) > FORMAT_VERSION:
            raise VaultError("this vault was written by a newer version")
        if int(h["format_version"]) < 1 or not _ID_RE.match(str(h["vault_id"])):
            raise VaultError("vault header is corrupted")
        for col in ("salt", "wrap_nonce", "wrapped_key", "public_key"):
            if not isinstance(h[col], bytes):
                raise VaultError("vault header is corrupted")
        if len(h["public_key"]) != 32 or len(h["salt"]) < 16:
            raise VaultError("vault header is corrupted")
        if not crypto.ct_equal(_header_checksum(h), str(h["checksum"])):
            raise VaultError("vault header is corrupted")
        crypto.KdfParams.from_dict(json.loads(h["kdf"]))
    except VaultError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise VaultError("vault header is corrupted") from exc


def _unwrap(h: dict[str, Any], passphrase: str) -> tuple[bytearray, crypto.KeySet]:
    """Passphrase -> master key -> sub-keys. Returns the master too (caller wipes)."""
    params = crypto.KdfParams.from_dict(json.loads(h["kdf"]))
    kek = crypto.derive_kek(passphrase, h["salt"], params)
    try:
        master = crypto.unwrap_key(kek, h["wrap_nonce"], h["wrapped_key"], _header_aad(h))
    finally:
        crypto.wipe(kek)
    keys = crypto.KeySet(master, h["vault_id"])
    if not crypto.ct_equal(keys.public_key(), h["public_key"]):
        keys.wipe()
        crypto.wipe(master)
        raise VaultError("vault header is inconsistent")
    return master, keys


def _make_header(vault_id: str, master: bytes | bytearray, passphrase: str, params: crypto.KdfParams, created_at: str, public_key: bytes) -> dict[str, Any]:
    salt = os.urandom(crypto.SALT_BYTES)
    h: dict[str, Any] = {
        "id": 1, "format_version": FORMAT_VERSION, "vault_id": vault_id,
        "kdf": json.dumps(params.to_dict(), sort_keys=True), "salt": salt,
        "public_key": public_key, "created_at": created_at,
    }
    kek = crypto.derive_kek(passphrase, salt, params)
    try:
        h["wrap_nonce"], h["wrapped_key"] = crypto.wrap_key(kek, master, _header_aad(h))
    finally:
        crypto.wipe(kek)
    h["checksum"] = _header_checksum(h)
    return h


def _connect_path(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _create_file(path: Path) -> None:
    """Create the file 0600 and exclusively, so we never adopt an existing file."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        try:
            path.parent.chmod(0o700)   # only a directory we created; never chmod the user's
        except OSError:
            pass
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise VaultError("a vault already exists at that path") from None
    os.close(fd)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- the vault


class LocalVault:
    """Encrypted record store. Construct with LocalVault.create / .open.

    Thread-safe: an RLock guards the key material, so lock() cannot wipe keys
    while another thread is in the middle of encrypting with them.
    """

    def __init__(self, path: Path, header: dict[str, Any], clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.vault_id: str = header["vault_id"]
        self._public_key: bytes = bytes(header["public_key"])
        self.clock = clock
        self._keys: crypto.KeySet | None = None
        self._lock = threading.RLock()
        self._last_used = time.monotonic()
        self._destroyed = False

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, path: str | os.PathLike[str] | None, passphrase: str, *, kdf: crypto.KdfParams | None = None,
               clock: Callable[[], float] = time.time) -> LocalVault:
        """New vault, unlocked. `kdf` exists for tests; production uses DEFAULT_KDF."""
        crypto.require_passphrase(passphrase)
        params = kdf or crypto.DEFAULT_KDF
        params.validate()
        target = Path(path) if path is not None else default_vault_path()
        _create_file(target)
        vault_id = new_id()
        master = crypto.random_key()
        keys = crypto.KeySet(master, vault_id)
        try:
            h = _make_header(vault_id, master, passphrase, params, iso_utc(clock()), keys.public_key())
            conn = _connect_path(target)
            try:
                _init_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                cols = list(_TABLES["header"])
                conn.execute(f"INSERT INTO header ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [h[c] for c in cols])
                conn.execute("INSERT INTO meta (key, value) VALUES ('enc_count', '0')")
                conn.execute("COMMIT")
            finally:
                conn.close()
        except BaseException:
            keys.wipe()
            try:
                os.unlink(target)
            except OSError:
                pass
            raise
        finally:
            crypto.wipe(master)
        vault = cls(target, h, clock)
        vault._keys = keys
        return vault

    @classmethod
    def open(cls, path: str | os.PathLike[str] | None = None, *, clock: Callable[[], float] = time.time) -> LocalVault:
        """Open an existing vault, locked."""
        target = Path(path) if path is not None else default_vault_path()
        if not target.is_file():
            raise VaultError("no vault at that path")
        h = cls._read_header(target)
        return cls(target, h, clock)

    @staticmethod
    def _read_header(path: Path) -> dict[str, Any]:
        try:
            conn = _connect_path(path)
            try:
                row = conn.execute("SELECT * FROM header WHERE id=1").fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise VaultError("not a vault file, or the file is corrupted") from exc
        if row is None:
            raise VaultError("vault header is missing")
        h = dict(row)
        _check_header(h)
        return h

    def is_unlocked(self) -> bool:
        return self._keys is not None

    def unlock(self, passphrase: str) -> None:
        """Re-reads the header each time: another process may have changed the passphrase."""
        self._check_alive()
        h = self._read_header(self.path)
        if h["vault_id"] != self.vault_id or not crypto.ct_equal(h["public_key"], self._public_key):
            raise VaultError("the vault file was replaced by a different vault")
        master, keys = _unwrap(h, passphrase)
        crypto.wipe(master)
        with self._lock:
            if self._keys is not None:
                self._keys.wipe()
            self._keys = keys
            self.touch()

    def lock(self) -> None:
        """Overwrite our bytearray copies of the sub-keys and drop them.

        Honest limits: Python and OpenSSL make transient copies (bytes objects
        passed to AESGCM, the scrypt output, decrypted plaintext strings) that
        we cannot overwrite; they linger until the allocator reuses the memory
        and may be swapped to disk or appear in a core dump. Locking stops this
        object from using the keys; it is not a guarantee that no copy remains
        in the process. Use an encrypted swap and disable core dumps for that.
        """
        with self._lock:
            if self._keys is not None:
                self._keys.wipe()
            self._keys = None

    def touch(self) -> None:
        self._last_used = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    def lock_if_idle(self, max_idle_seconds: float) -> bool:
        """For the app's timer: lock after inactivity. True if it locked."""
        if self.is_unlocked() and self.idle_seconds() >= max_idle_seconds:
            self.lock()
            return True
        return False

    def public_key(self) -> bytes:
        return self._public_key

    def change_passphrase(self, old: str, new: str, *, kdf: crypto.KdfParams | None = None) -> None:
        """Re-wraps the master key under a new passphrase (and optionally new
        scrypt parameters). Records are untouched. Old backups of the file still
        open with the old passphrase."""
        self._check_alive()
        crypto.require_passphrase(new)
        with self._write() as conn:
            row = conn.execute("SELECT * FROM header WHERE id=1").fetchone()
            if row is None:
                raise VaultError("vault header is missing")
            h = dict(row)
            _check_header(h)
            master, keys = _unwrap(h, old)
            try:
                params = kdf or crypto.KdfParams.from_dict(json.loads(h["kdf"]))
                params.validate()
                nh = _make_header(h["vault_id"], master, new, params, h["created_at"], h["public_key"])
            finally:
                crypto.wipe(master)
                keys.wipe()
            conn.execute(
                "UPDATE header SET kdf=?, salt=?, wrap_nonce=?, wrapped_key=?, checksum=? WHERE id=1",
                (nh["kdf"], nh["salt"], nh["wrap_nonce"], nh["wrapped_key"], nh["checksum"]),
            )

    # ------------------------------------------------------------ plumbing used by mandates.py / receipts.py

    def _check_alive(self) -> None:
        if self._destroyed:
            raise VaultError("this vault has been destroyed")

    def _connect(self) -> sqlite3.Connection:
        self._check_alive()
        if not self.path.is_file():
            raise VaultError("the vault file is missing")
        conn = _connect_path(self.path)
        key = str(self.path.resolve())
        if key not in _initialised:
            with _init_lock:
                _init_schema(conn)
                _initialised.add(key)
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Short IMMEDIATE transaction on a fresh connection."""
        try:
            conn = self._connect()
        except sqlite3.DatabaseError as exc:
            raise VaultError("the vault file could not be opened") from exc
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise VaultError("the vault is busy; try again") from exc
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.DatabaseError as exc:
            raise VaultError("the vault file is corrupted or busy") from exc
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A read transaction, so multi-statement reads see one snapshot."""
        try:
            conn = self._connect()
        except sqlite3.DatabaseError as exc:
            raise VaultError("the vault file could not be opened") from exc
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise VaultError("the vault file is corrupted or busy") from exc
        finally:
            conn.close()

    def _require_keys(self) -> crypto.KeySet:
        self._check_alive()
        keys = self._keys
        if keys is None:
            raise VaultLocked("the vault is locked")
        self.touch()
        return keys

    def _bump_encryptions(self, conn: sqlite3.Connection, n: int) -> None:
        row = conn.execute("SELECT value FROM meta WHERE key='enc_count'").fetchone()
        count = int(row["value"]) if row else 0
        if count + n > crypto.MAX_ENCRYPTIONS_PER_KEY:
            raise VaultError("this vault reached its encryption limit; export and re-key it")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('enc_count', ?)", (str(count + n),))

    def encrypt_field(self, conn: sqlite3.Connection, table: str, row_id: str, field: str, plaintext: bytes) -> bytes:
        """Padded AES-GCM with the row-binding AAD; counts toward the nonce budget."""
        with self._lock:
            keys = self._require_keys()
            self._bump_encryptions(conn, 1)
            aad = crypto.record_aad(self.vault_id, table, row_id, field, SCHEMA_VERSION)
            return crypto.encrypt(keys.record, crypto.pad(plaintext), aad)

    def decrypt_field(self, table: str, row_id: str, field: str, blob: bytes) -> bytes:
        with self._lock:
            keys = self._require_keys()
            aad = crypto.record_aad(self.vault_id, table, row_id, field, SCHEMA_VERSION)
            return crypto.unpad(crypto.decrypt(keys.record, blob, aad))

    def sign_with_domain(self, domain: str, message: bytes) -> bytes:
        with self._lock:
            keys = self._require_keys()
            return crypto.sign(keys.signing_seed, domain, message)

    def keyed_hash(self, label: str, data: bytes) -> str:
        with self._lock:
            keys = self._require_keys()
            return crypto.keyed_hash(keys.index, label, data)

    def ref_token(self, content_sha256: str) -> str:
        """What receipts store instead of the plain content hash (see receipts.py)."""
        if not isinstance(content_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
            raise VaultError("record refs must be lowercase hex SHA-256")
        return self.keyed_hash("receipt-ref", bytes.fromhex(content_sha256))

    def titles(self) -> list[str]:
        """Decrypted titles, for receipts.py's detail check. Needs the key."""
        return [m["title"] for _, m in self._metas()]

    # ------------------------------------------------------------ records

    def _metas(self) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
        with self._read() as conn:
            rows = conn.execute("SELECT id, added_day, size_bucket, meta FROM records ORDER BY rowid DESC").fetchall()
        out = []
        for row in rows:
            meta = json.loads(self.decrypt_field("records", row["id"], "meta", row["meta"]).decode("utf-8"))
            out.append((row, meta))
        return out

    @staticmethod
    def _record(row_id: str, meta: dict[str, Any]) -> Record:
        return Record(
            id=row_id, kind=meta["kind"], title=meta["title"], occurred_on=meta["occurred_on"],
            source=meta["source"], added_at=meta["added_at"], size=int(meta["size"]),
            content_sha256=meta["sha256"],
        )

    def add(self, kind: str, title: str, text: str, occurred_on: str | None = None, source: str = "") -> Record:
        if kind not in RECORD_KINDS:
            raise VaultError("unknown record kind")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS or has_control_chars(title):
            raise VaultError(f"title must be 1-{MAX_TITLE_CHARS} characters without control characters")
        if not isinstance(source, str) or len(source) > MAX_SOURCE_CHARS or has_control_chars(source):
            raise VaultError(f"source must be at most {MAX_SOURCE_CHARS} characters without control characters")
        if occurred_on is not None and not valid_date(occurred_on):
            raise VaultError("occurred_on must be YYYY-MM-DD")
        if not isinstance(text, str):
            raise VaultError("record text must be a string")
        body = text.encode("utf-8")
        if len(body) > MAX_TEXT_BYTES:
            raise VaultError("record text is larger than 2 MiB")
        record_id = new_id()
        now = self.clock()
        meta = {
            "kind": kind, "title": title, "source": source, "occurred_on": occurred_on,
            "added_at": iso_utc(now), "size": len(text), "sha256": crypto.sha256_hex(body),
        }
        # Never hold self._lock while waiting for the SQLite write lock: another
        # thread may hold the file lock and need self._lock to sign/encrypt.
        mac = self.keyed_hash("content", body)
        with self._write() as conn:
            meta_ct = self.encrypt_field(conn, "records", record_id, "meta", crypto.canonical_json(meta))
            body_ct = self.encrypt_field(conn, "records", record_id, "text", body)
            conn.execute(
                "INSERT INTO records (id, added_day, size_bucket, content_mac, meta, body) VALUES (?,?,?,?,?,?)",
                (record_id, meta["added_at"][:10], len(body_ct), mac, meta_ct, body_ct),
            )
        return self._record(record_id, meta)

    def find_by_text(self, text: str) -> str | None:
        """Record id with exactly this text, via the keyed hash (for dedupe on import)."""
        mac = self.keyed_hash("content", text.encode("utf-8"))
        with self._read() as conn:
            row = conn.execute("SELECT id FROM records WHERE content_mac=? LIMIT 1", (mac,)).fetchone()
        return row["id"] if row else None

    def get(self, record_id: str) -> tuple[Record, str]:
        check_id(record_id)
        self._require_keys()
        with self._read() as conn:
            row = conn.execute("SELECT id, meta, body FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise VaultError("no such record")
        meta = json.loads(self.decrypt_field("records", record_id, "meta", row["meta"]).decode("utf-8"))
        body = self.decrypt_field("records", record_id, "text", row["body"])
        # The plain hash is recomputed in memory from the decrypted text.
        if not crypto.ct_equal(crypto.sha256_hex(body), meta["sha256"]):
            raise VaultError("record text does not match its hash")
        return self._record(record_id, meta), body.decode("utf-8")

    def list(self, kind: str | None = None, since: str | None = None, until: str | None = None, limit: int = 200) -> list[Record]:
        """Newest first by occurred_on (or the day added when unknown). since/until are inclusive dates."""
        if kind is not None and kind not in RECORD_KINDS:
            raise VaultError("unknown record kind")
        for value in (since, until):
            if value is not None and not valid_date(value):
                raise VaultError("since/until must be YYYY-MM-DD")
        limit = max(0, min(int(limit), 100000))
        out = []
        for row, meta in self._metas():
            day = meta["occurred_on"] or meta["added_at"][:10]
            if kind is not None and meta["kind"] != kind:
                continue
            if since is not None and day < since:
                continue
            if until is not None and day > until:
                continue
            out.append((day, meta["added_at"], self._record(row["id"], meta)))
        out.sort(key=lambda t: (t[0], t[1]), reverse=True)   # stable: newest insert first on ties
        return [r for _, _, r in out[:limit]]

    def search(self, query: str, limit: int = 8) -> list[tuple[Record, str]]:
        """Decrypt-and-scan in memory; nothing is indexed on disk.

        Case-insensitive (NFKC + casefold). Chinese has no spaces, so instead
        of word segmentation a record scores on exact substring hits plus the
        share of the query's character bigrams it contains; Latin queries also
        score per whitespace token. Practical up to a few thousand records.
        """
        if not isinstance(query, str):
            raise VaultError("query must be text")
        q = _norm(query.strip())[:MAX_QUERY_CHARS]
        if not q:
            return []
        compact = "".join(q.split())
        bigrams = {compact[i : i + 2] for i in range(len(compact) - 1)}
        tokens = [t for t in q.split() if t]
        self._require_keys()
        with self._read() as conn:
            rows = conn.execute("SELECT id, meta, body FROM records").fetchall()
        scored: list[tuple[float, str, Record, str]] = []
        for row in rows:
            meta = json.loads(self.decrypt_field("records", row["id"], "meta", row["meta"]).decode("utf-8"))
            text = self.decrypt_field("records", row["id"], "text", row["body"]).decode("utf-8")
            haystack_orig = f"{meta['title']}\n{meta['source']}\n{text}"
            hay = _norm(haystack_orig)
            title = _norm(meta["title"])
            score = 0.0
            hit = hay.find(q)
            if hit >= 0:
                score += 10 + 2 * min(hay.count(q), 10)
                if q in title:
                    score += 20
            if len(tokens) > 1:
                present = [t for t in tokens if t in hay]
                if len(present) == len(tokens):
                    score += 8
                score += 2 * len(present)
                if hit < 0 and present:
                    hit = hay.find(present[0])
            if bigrams:
                found = [b for b in bigrams if b in hay]
                frac = len(found) / len(bigrams)
                if frac >= 0.6:
                    score += 6 * frac
                    if hit < 0:
                        hit = hay.find(found[0])
            if score <= 0 or hit < 0:
                continue
            snippet = _snippet(haystack_orig, hay, hit, len(q))
            scored.append((score, meta["added_at"], self._record(row["id"], meta), snippet))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [(r, s) for _, _, r, s in scored[: max(0, int(limit))]]

    def delete(self, record_id: str) -> None:
        """Removes the row; secure_delete=ON zeroes its bytes in the file."""
        check_id(record_id)
        self._require_keys()
        with self._write() as conn:
            cur = conn.execute("DELETE FROM records WHERE id=?", (record_id,))
            if cur.rowcount == 0:
                raise VaultError("no such record")

    # ------------------------------------------------------------ items

    @staticmethod
    def _check_item_data(data: dict[str, Any]) -> bytes:
        if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
            raise VaultError("item data must be an object with string keys")
        if _json_depth(data) > MAX_ITEM_DEPTH:
            raise VaultError("item data is nested too deeply")
        try:
            raw = crypto.canonical_json(data)
        except (TypeError, ValueError) as exc:
            raise VaultError("item data must be plain JSON values") from exc
        if len(raw) > MAX_ITEM_BYTES:
            raise VaultError("item data is too large")
        return raw

    def _item(self, item_id: str, body: bytes) -> Item:
        obj = json.loads(self.decrypt_field("items", item_id, "item", body).decode("utf-8"))
        return Item(id=item_id, kind=obj["kind"], data=obj["data"], created_at=obj["created_at"], updated_at=obj["updated_at"])

    def _write_item(self, conn: sqlite3.Connection, item_id: str, obj: dict[str, Any], insert: bool) -> None:
        ct = self.encrypt_field(conn, "items", item_id, "item", crypto.canonical_json(obj))
        if insert:
            conn.execute("INSERT INTO items (id, size_bucket, body) VALUES (?,?,?)", (item_id, len(ct), ct))
        else:
            conn.execute("UPDATE items SET size_bucket=?, body=? WHERE id=?", (len(ct), ct, item_id))

    def add_item(self, kind: str, data: dict[str, Any]) -> Item:
        if kind not in ITEM_KINDS:
            raise VaultError("unknown item kind")
        self._check_item_data(data)
        self._require_keys()
        item_id = new_id()
        now = iso_utc(self.clock())
        obj = {"kind": kind, "data": data, "created_at": now, "updated_at": now}
        with self._write() as conn:
            self._write_item(conn, item_id, obj, insert=True)
        return Item(id=item_id, kind=kind, data=json.loads(crypto.canonical_json(data)), created_at=now, updated_at=now)

    def list_items(self, kind: str | None = None) -> list[Item]:
        if kind is not None and kind not in ITEM_KINDS:
            raise VaultError("unknown item kind")
        self._require_keys()
        with self._read() as conn:
            rows = conn.execute("SELECT id, body FROM items ORDER BY rowid").fetchall()
        items = [self._item(r["id"], r["body"]) for r in rows]
        items = [i for i in items if kind is None or i.kind == kind]
        items.sort(key=lambda i: i.created_at)   # stable: insertion order breaks ties
        return items

    def update_item(self, item_id: str, data: dict[str, Any]) -> Item:
        """Replaces the item's data (not a merge), keeping kind and created_at."""
        check_id(item_id)
        self._check_item_data(data)
        self._require_keys()
        with self._write() as conn:
            row = conn.execute("SELECT body FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise VaultError("no such item")
            old = self._item(item_id, row["body"])
            obj = {"kind": old.kind, "data": data, "created_at": old.created_at, "updated_at": iso_utc(self.clock())}
            self._write_item(conn, item_id, obj, insert=False)
        return Item(id=item_id, kind=old.kind, data=json.loads(crypto.canonical_json(data)), created_at=old.created_at, updated_at=obj["updated_at"])

    def delete_item(self, item_id: str) -> None:
        check_id(item_id)
        self._require_keys()
        with self._write() as conn:
            if conn.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount == 0:
                raise VaultError("no such item")

    # ------------------------------------------------------------ signing

    def sign(self, message: bytes) -> bytes:
        """Ed25519 over b"muse-health/v1/sig/app\\0" + message. The domain prefix
        means nothing signed through this method can pass as a mandate or a
        receipt. Verify with crypto.verify_signature(pk, "app", message, sig)."""
        if not isinstance(message, (bytes, bytearray)):
            raise VaultError("message must be bytes")
        return self.sign_with_domain("app", bytes(message))

    # ------------------------------------------------------------ archive

    def export_archive(self, dest: str) -> None:
        """One file, still encrypted: the header (wrapped master key) and every
        row as stored, plus SHA-256 (truncation / corruption) and HMAC-SHA256
        under the archive key (tampering, checked when imported with the
        passphrase). Refuses to overwrite `dest`."""
        with self._lock:
            keys = self._require_keys()
            archive_key = bytes(keys.archive)
        tables: dict[str, list[dict[str, Any]]] = {}
        with self._read() as conn:
            for table, cols in _TABLES.items():
                order = "seq" if table == "receipts" else ("key" if table == "meta" else "rowid")
                rows = conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY {order}").fetchall()
                tables[table] = [{c: _enc_value(row[c]) for c in cols} for row in rows]
        body = crypto.canonical_json({
            "format": "muse-health-archive", "version": 1, "vault_id": self.vault_id,
            "exported_at": iso_utc(self.clock()), "tables": tables,
        })
        signed = ARCHIVE_MAGIC + body + b"\n"
        trailer = crypto.canonical_json({
            "sha256": crypto.sha256_hex(signed),
            "hmac": crypto.keyed_hash(archive_key, "archive", signed),
        })
        target = Path(dest)
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(signed + trailer + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, target)   # fails if dest exists: never overwrite
            except FileExistsError:
                raise VaultError("the archive destination already exists") from None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @classmethod
    def import_archive(cls, src: str | os.PathLike[str], dest_path: str | os.PathLike[str] | None = None, *,
                       passphrase: str | None = None, clock: Callable[[], float] = time.time,
                       max_bytes: int = MAX_ARCHIVE_BYTES) -> LocalVault:
        """Create a new vault file from an archive; returns it locked.

        Always checks structure and the SHA-256 (truncation, corruption). With
        `passphrase` it also checks the HMAC, which an attacker cannot forge
        without the passphrase; the app should always pass it. Without it, a
        carefully re-hashed archive with rows removed would still import (each
        row stays individually authenticated, but a missing row is not seen).
        Never overwrites an existing file.
        """
        src = Path(src)
        target = Path(dest_path) if dest_path is not None else default_vault_path()
        if target.exists():
            raise VaultError("a vault already exists at that path")
        try:
            size = src.stat().st_size
        except OSError as exc:
            raise VaultError("cannot read the archive") from exc
        if size > max_bytes:
            raise VaultError("archive is larger than allowed")
        data = src.read_bytes()
        tables, header = _parse_archive(data)
        if passphrase is not None:
            master, keys = _unwrap(header, passphrase)
            crypto.wipe(master)
            try:
                signed = data[: data.rindex(b"\n", 0, len(data) - 1) + 1]
                trailer = json.loads(data[len(signed):].decode("utf-8"))
                if not crypto.ct_equal(crypto.keyed_hash(keys.archive, "archive", signed), str(trailer.get("hmac", ""))):
                    raise VaultError("archive failed its integrity check (tampered)")
            finally:
                keys.wipe()
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.importing")
        _create_file(tmp)
        try:
            conn = _connect_path(tmp)
            try:
                _init_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                for table, cols in _TABLES.items():
                    names = list(cols)
                    sql = f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})"
                    for row in tables[table]:
                        conn.execute(sql, [row[c] for c in names])
                conn.execute("COMMIT")
            finally:
                conn.close()
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise VaultError("a vault already exists at that path") from None
        finally:
            for leftover in (tmp, Path(str(tmp) + "-journal")):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass
        return cls.open(target, clock=clock)

    # ------------------------------------------------------------ destruction

    def destroy(self) -> None:
        """Crypto-shred, then delete.

        1. Overwrite the wrapped master key, salt and public key in the header
           (secure_delete zeroes the old page content): without the wrapped key
           no passphrase can open the ciphertext left anywhere in this file.
        2. Overwrite the whole file with zeros, fsync, unlink; remove a journal.
        Honest limits: on SSDs (wear levelling), copy-on-write filesystems
        (btrfs, ZFS, APFS), snapshots and backups, the old blocks may survive.
        Any surviving copy of the file still opens with the passphrase. The
        only complete answer is that every copy is encrypted and the passphrase
        stays secret.
        """
        self._require_keys()
        with self._write() as conn:
            conn.execute(
                "UPDATE header SET salt=?, wrap_nonce=?, wrapped_key=?, public_key=?, checksum='destroyed' WHERE id=1",
                (os.urandom(32), os.urandom(12), os.urandom(48), os.urandom(32)),
            )
        self.lock()
        size = self.path.stat().st_size
        with open(self.path, "r+b") as fh:
            remaining = size
            chunk = bytes(1024 * 1024)
            while remaining > 0:
                n = min(remaining, len(chunk))
                fh.write(chunk[:n])
                remaining -= n
            fh.flush()
            os.fsync(fh.fileno())
        for p in (self.path, Path(str(self.path) + "-journal")):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        _initialised.discard(str(self.path.resolve()))
        self._destroyed = True


# ---------------------------------------------------------------- archive helpers


def _enc_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"b64": crypto.b64e(value)}
    return value


def _dec_value(value: Any, kind: str, nullable: bool) -> Any:
    if value is None:
        if not nullable:
            raise VaultError("archive is malformed")
        return None
    if kind == "blob":
        if not isinstance(value, dict) or set(value) != {"b64"} or not isinstance(value["b64"], str):
            raise VaultError("archive is malformed")
        return crypto.b64d(value["b64"])
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise VaultError("archive is malformed")
        return value
    if not isinstance(value, str):
        raise VaultError("archive is malformed")
    return value


def _parse_archive(data: bytes) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Validate structure and SHA-256; returns decoded rows and the header."""
    if not data.startswith(ARCHIVE_MAGIC):
        raise VaultError("not a vault archive")
    if not data.endswith(b"\n") or data.count(b"\n") < 3:
        raise VaultError("archive is truncated")
    cut = data.rindex(b"\n", 0, len(data) - 1) + 1
    signed, trailer_raw = data[:cut], data[cut:]
    try:
        trailer = json.loads(trailer_raw.decode("utf-8"))
        expected = str(trailer["sha256"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise VaultError("archive is truncated or corrupted") from None
    if not crypto.ct_equal(crypto.sha256_hex(signed), expected):
        raise VaultError("archive is truncated or corrupted")
    try:
        doc = json.loads(signed[len(ARCHIVE_MAGIC):-1].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise VaultError("archive is malformed") from None
    if not isinstance(doc, dict) or doc.get("format") != "muse-health-archive" or doc.get("version") != 1:
        raise VaultError("unsupported archive version")
    raw_tables = doc.get("tables")
    if not isinstance(raw_tables, dict) or set(raw_tables) != set(_TABLES):
        raise VaultError("archive is malformed")
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, cols in _TABLES.items():
        rows = raw_tables[table]
        if not isinstance(rows, list):
            raise VaultError("archive is malformed")
        decoded = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(cols):
                raise VaultError("archive is malformed")
            decoded.append({c: _dec_value(row[c], *cols[c]) for c in cols})
        tables[table] = decoded
    if len(tables["header"]) != 1:
        raise VaultError("archive is malformed")
    if any(r["key"] not in _META_KEYS for r in tables["meta"]):
        raise VaultError("archive is malformed")
    for table in ("records", "items", "mandates"):
        for r in tables[table]:
            check_id(r["id"])
    header = tables["header"][0]
    _check_header(header)
    if header["vault_id"] != doc.get("vault_id"):
        raise VaultError("archive is malformed")
    return tables, header


def _snippet(orig: str, norm: str, hit: int, qlen: int, before: int = 30, after: int = 60) -> str:
    """Short context around a hit, mapped back to the original text."""
    if len(orig) == len(norm):
        pos = hit
    else:
        # Normalisation changed lengths somewhere: walk to find the original index.
        acc = 0
        pos = 0
        for i, ch in enumerate(orig):
            if acc >= hit:
                pos = i
                break
            acc += len(_norm(ch))
        else:
            pos = len(orig)
    start = max(0, pos - before)
    end = min(len(orig), pos + max(qlen, 1) + after)
    text = " ".join(orig[start:end].split())
    return ("…" if start > 0 else "") + text + ("…" if end < len(orig) else "")
