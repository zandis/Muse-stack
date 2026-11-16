"""Where a chain is kept: the RecordStore interface, an in-memory store, and the home's SQLite store.

The store is not part of the format. It keeps records in order and makes
sure an append extends the head: a second record for a taken sequence number
would be a fork, signed by this very device.

SqliteRecordStore follows muse/ledger.py: journal_mode=DELETE, a new
connection per operation, a busy timeout and short IMMEDIATE transactions, so
several processes can append. `append_with` reads the head, builds (signs)
the next record and inserts it inside one IMMEDIATE transaction; nothing is
ever signed for a sequence number another process has taken. The file is
created 0600. It holds no content: summaries are short safe text by rule and
resources are keyed references.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from wardmuse.records import format as fmt
from wardmuse.records.errors import RecordError
from wardmuse.records.jcs import canonicalize, loads_strict
from wardmuse.records.seal import check_checkpoint

BUSY_TIMEOUT_MS = 15000
SCHEMA_VERSION = 1

Record = dict[str, Any]


class RecordStore(Protocol):
    """The four operations the records code needs from storage (the vault will supply an encrypted one)."""

    def get(self, seq: int) -> Record | None: ...

    def len(self) -> int: ...

    def head(self) -> Record | None: ...

    def append(self, record: Record) -> None:
        """Add `record` if it is the next one after the head; RecordError otherwise (missing, fork, wrong_prev)."""


def check_extends(head: Record | None, size: int, record: Record) -> None:
    """Is `record` the one that may follow a chain of `size` records ending in `head`?"""
    fmt.check_record(record)
    if record["seq"] > size:
        raise RecordError("missing", detail="the store is behind this record")
    if record["seq"] < size:
        raise RecordError("fork", detail="that sequence number is taken")
    if head is not None and record["device"] != head["device"]:
        raise RecordError("wrong_device")
    if record["prev"] != (fmt.genesis(record["device"]) if head is None else fmt.record_hash(head)):
        raise RecordError("wrong_prev")


class MemoryStore:
    """A RecordStore in a list, for tests."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    def get(self, seq: int) -> Record | None:
        return self.records[seq] if 0 <= seq < len(self.records) else None

    def len(self) -> int:
        return len(self.records)

    def head(self) -> Record | None:
        return self.records[-1] if self.records else None

    def append(self, record: Record) -> None:
        check_extends(self.head(), len(self.records), record)
        self.records.append(dict(record))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY,
    time INTEGER NOT NULL,
    action TEXT NOT NULL,
    hash TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    size INTEGER NOT NULL,
    time INTEGER NOT NULL,
    body TEXT NOT NULL
)
"""


class SqliteRecordStore:
    """The home's chain in one SQLite file. Safe for several processes."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.close(os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            have = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone()
            if not have:
                self._init_schema(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"records schema {row['value']} is newer than this code ({SCHEMA_VERSION})")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _record(row: Any) -> Record | None:
        return None if row is None else loads_strict(row["body"])

    @staticmethod
    def _head(conn: sqlite3.Connection) -> tuple[Record | None, int]:
        row = conn.execute("SELECT seq, body FROM records ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return None, 0
        return loads_strict(row["body"]), int(row["seq"]) + 1

    @staticmethod
    def _insert(conn: sqlite3.Connection, record: Record) -> None:
        conn.execute("INSERT INTO records (seq, time, action, hash, body) VALUES (?,?,?,?,?)",
                     (record["seq"], record["time"], record["action"], fmt.record_hash(record),
                      canonicalize(record).decode("utf-8")))

    # ------------------------------------------------------------ RecordStore

    def get(self, seq: int) -> Record | None:
        with self._read() as conn:
            return self._record(conn.execute("SELECT body FROM records WHERE seq=?", (int(seq),)).fetchone())

    def len(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT MAX(seq) AS top FROM records").fetchone()
        return 0 if row["top"] is None else int(row["top"]) + 1

    def head(self) -> Record | None:
        with self._read() as conn:
            return self._head(conn)[0]

    def append(self, record: Record) -> None:
        with self._write() as conn:
            head, size = self._head(conn)
            check_extends(head, size, record)
            self._insert(conn, record)

    # ------------------------------------------------------------ more than the interface

    def append_with(self, make: Callable[[Record | None], Record]) -> Record:
        """Call `make(head)` and store what it returns, all under the write lock."""
        with self._write() as conn:
            head, size = self._head(conn)
            record = make(head)
            check_extends(head, size, record)
            self._insert(conn, record)
        return record

    def all(self) -> list[Record]:
        """Every stored row in sequence order, as stored (verify_chain decides whether it is a chain)."""
        with self._read() as conn:
            rows = conn.execute("SELECT body FROM records ORDER BY seq").fetchall()
        out: list[Record] = []
        for row in rows:
            try:
                out.append(loads_strict(row["body"]))
            except RecordError:
                out.append({})  # unreadable: verify_chain reports it as malformed at this index
        return out

    def add_checkpoint(self, cp: dict[str, Any]) -> None:
        check_checkpoint(cp)
        with self._write() as conn:
            conn.execute("INSERT INTO checkpoints (size, time, body) VALUES (?,?,?)",
                         (cp["size"], cp["time"], canonicalize(cp).decode("utf-8")))

    def last_checkpoint(self) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute("SELECT body FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else loads_strict(row["body"])
