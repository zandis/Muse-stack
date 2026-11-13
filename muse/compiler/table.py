"""The compiler's own bookkeeping: `MUSE_HOME/compiler.sqlite` (0600).

  artifacts   one row per thing made for a permission: (mandate id, target, key) -> the artifact's id,
              its state, and the TYPE of the last error. A row is written `pending` BEFORE the artifact
              is made and removed only AFTER the artifact is gone, so a crash in between leaves a row
              the sweep finishes, never an artifact nobody knows about.
  usage       what a permission's holder used in each period (requests, money), counted by the guard.
              This is the "used so far" figure a check needs; the token cannot hold it.
  approvals   a card that stands for one guarded action (a proposed file): which narrowed token asked,
              for what. Approving the card mints the single-use token from it (architecture 3.6).
  drafts      a permission as proposed through the home API, waiting to be issued.
  requests    a permission a device asked for: its card, and how the person answered.

No token, key or app token is ever stored here: tokens live in `MUSE_HOME/permissions/` (0600 files).
Every method opens its own short transaction.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from muse import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  mandate_id TEXT NOT NULL,
  target TEXT NOT NULL,
  key TEXT NOT NULL,
  kind TEXT NOT NULL,
  artifact_id TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL,               -- pending | active | failed | not_built | removing
  reason TEXT NOT NULL DEFAULT '',   -- a fixed plain sentence, never exception text
  error TEXT NOT NULL DEFAULT '',    -- the TYPE of the last error only
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (mandate_id, target, key)
);
CREATE TABLE IF NOT EXISTS usage (
  mandate_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  unit TEXT NOT NULL,
  period TEXT NOT NULL,              -- day:2026-11-13 or month:2026-11, in the permission's own time zone
  amount INTEGER NOT NULL,
  PRIMARY KEY (mandate_id, kind, unit, period)
);
CREATE TABLE IF NOT EXISTS approvals (
  card_id TEXT PRIMARY KEY,
  mandate_id TEXT NOT NULL,
  token_name TEXT NOT NULL,          -- the stored narrowed token (a file name, never the token)
  action TEXT NOT NULL,
  resource_kind TEXT NOT NULL,
  resource_value TEXT NOT NULL,
  privacy TEXT NOT NULL,
  job TEXT,
  mode TEXT NOT NULL,                -- advise | enforce, as it was for the run that asked
  state TEXT NOT NULL,               -- pending | used | refused
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id TEXT PRIMARY KEY,
  device TEXT NOT NULL,
  fields TEXT NOT NULL,              -- JSON of the draft's fields; no secret is among them
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  card_id TEXT PRIMARY KEY,
  device TEXT NOT NULL,
  fingerprint TEXT NOT NULL,         -- what was asked for (holder, actions, resources), hashed
  fields TEXT NOT NULL,
  state TEXT NOT NULL,               -- pending | issued | declined | expired | failed
  mandate_id TEXT,
  created_at REAL NOT NULL,
  settled_at REAL
);
CREATE INDEX IF NOT EXISTS requests_device ON requests (device, created_at);
"""

MAX_DRAFTS = 200
DRAFT_TTL_S = 3600.0
MAX_REQUESTS = 2000


def table_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "compiler.sqlite"


class Table:
    def __init__(self, environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 path: Path | None = None) -> None:
        self.clock = clock
        self.path = Path(path) if path is not None else table_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(SCHEMA)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if write and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if write and conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            conn.close()

    # ---------------------------------------------------------------- artifacts

    def put(self, mandate_id: str, target: str, key: str, kind: str, *, state: str, artifact_id: str | None = None,
            reason: str = "", error: str = "") -> None:
        """Insert or update one row. `artifact_id=None` keeps the id already there."""
        now = self.clock()
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT artifact_id FROM artifacts WHERE mandate_id=? AND target=? AND key=?",
                               (mandate_id, target, key)).fetchone()
            if row is None:
                conn.execute("INSERT INTO artifacts (mandate_id, target, key, kind, artifact_id, state, reason, error, created_at, "
                             "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (mandate_id, target, key, kind, artifact_id or "", state, reason[:300], error[:80], now, now))
            else:
                conn.execute("UPDATE artifacts SET kind=?, artifact_id=?, state=?, reason=?, error=?, updated_at=? "
                             "WHERE mandate_id=? AND target=? AND key=?",
                             (kind, row["artifact_id"] if artifact_id is None else artifact_id, state, reason[:300], error[:80], now,
                              mandate_id, target, key))

    def get(self, mandate_id: str, target: str, key: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE mandate_id=? AND target=? AND key=?", (mandate_id, target, key)).fetchone()
        return None if row is None else dict(row)

    def rows(self, mandate_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM artifacts", []
        if mandate_id is not None:
            sql, args = sql + " WHERE mandate_id=?", [mandate_id]
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY mandate_id, target, key", args)]

    def delete(self, mandate_id: str, target: str, key: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM artifacts WHERE mandate_id=? AND target=? AND key=?", (mandate_id, target, key))

    def mandate_ids(self) -> set[str]:
        with self._conn() as conn:
            return {str(r["mandate_id"]) for r in conn.execute("SELECT DISTINCT mandate_id FROM artifacts")}

    # ---------------------------------------------------------------- usage

    def used(self, mandate_id: str, kind: str, unit: str, period: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT amount FROM usage WHERE mandate_id=? AND kind=? AND unit=? AND period=?",
                               (mandate_id, kind, unit, period)).fetchone()
        return 0 if row is None else int(row["amount"])

    def add_usage(self, mandate_id: str, kind: str, unit: str, periods: list[str], amount: int) -> None:
        if amount <= 0:
            return
        with self._conn(write=True) as conn:
            for period in periods:
                conn.execute("INSERT INTO usage (mandate_id, kind, unit, period, amount) VALUES (?,?,?,?,?) "
                             "ON CONFLICT(mandate_id, kind, unit, period) DO UPDATE SET amount = amount + excluded.amount",
                             (mandate_id, kind, unit, period, int(amount)))

    def forget_usage(self, mandate_id: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM usage WHERE mandate_id=?", (mandate_id,))

    # ---------------------------------------------------------------- approvals

    def add_approval(self, card_id: str, *, mandate_id: str, token_name: str, action: str, resource: tuple[str, str],
                     privacy: str, job: str | None, mode: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO approvals (card_id, mandate_id, token_name, action, resource_kind, resource_value, "
                         "privacy, job, mode, state, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
                         (card_id, mandate_id, token_name, action, resource[0], resource[1], privacy, job, mode, self.clock()))
            conn.execute("DELETE FROM approvals WHERE created_at < ?", (self.clock() - 60 * 86400,))

    def approval(self, card_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE card_id=?", (card_id,)).fetchone()
        return None if row is None else dict(row)

    def settle_approval(self, card_id: str, state: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("UPDATE approvals SET state=? WHERE card_id=?", (state, card_id))

    def drop_approvals(self, mandate_id: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM approvals WHERE mandate_id=? AND state='pending'", (mandate_id,))

    # ---------------------------------------------------------------- drafts

    def add_draft(self, device: str, fields: Mapping[str, Any]) -> str:
        draft_id = secrets.token_hex(8)
        now = self.clock()
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM drafts WHERE created_at < ?", (now - DRAFT_TTL_S,))
            conn.execute("DELETE FROM drafts WHERE id NOT IN (SELECT id FROM drafts ORDER BY created_at DESC LIMIT ?)", (MAX_DRAFTS - 1,))
            conn.execute("INSERT INTO drafts (id, device, fields, created_at) VALUES (?,?,?,?)",
                         (draft_id, device, json.dumps(fields, ensure_ascii=False, sort_keys=True), now))
        return draft_id

    def take_draft(self, draft_id: str, device: str, *, remove: bool) -> dict[str, Any] | None:
        """The fields of a draft this device made, if it is not older than an hour."""
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT fields, created_at FROM drafts WHERE id=? AND device=?", (draft_id, device)).fetchone()
            if row is None or row["created_at"] < self.clock() - DRAFT_TTL_S:
                return None
            if remove:
                conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        return json.loads(row["fields"])

    # ---------------------------------------------------------------- requests

    def add_request(self, card_id: str, device: str, fingerprint: str, fields: Mapping[str, Any]) -> None:
        now = self.clock()
        with self._conn(write=True) as conn:
            conn.execute("INSERT INTO requests (card_id, device, fingerprint, fields, state, created_at) VALUES (?,?,?,?, 'pending', ?)",
                         (card_id, device, fingerprint, json.dumps(fields, ensure_ascii=False, sort_keys=True), now))
            conn.execute("DELETE FROM requests WHERE card_id NOT IN (SELECT card_id FROM requests ORDER BY created_at DESC LIMIT ?)",
                         (MAX_REQUESTS,))

    def claim_request(self, card_id: str) -> dict[str, Any] | None:
        """Take a pending request for settling (compare-and-set), or None."""
        with self._conn(write=True) as conn:
            cur = conn.execute("UPDATE requests SET state='settling' WHERE card_id=? AND state='pending'", (card_id,))
            if cur.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM requests WHERE card_id=?", (card_id,)).fetchone()
        out = dict(row)
        out["fields"] = json.loads(out["fields"])
        return out

    def settle_request(self, card_id: str, state: str, mandate_id: str | None = None) -> None:
        with self._conn(write=True) as conn:
            conn.execute("UPDATE requests SET state=?, mandate_id=?, settled_at=? WHERE card_id=?",
                         (state, mandate_id, self.clock(), card_id))

    def request(self, card_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM requests WHERE card_id=?", (card_id,)).fetchone()
        return None if row is None else dict(row)

    def requests(self, *, state: str | None = None, device: str | None = None, since: float | None = None) -> list[dict[str, Any]]:
        sql, where, args = "SELECT card_id, device, fingerprint, fields, state, mandate_id, created_at, settled_at FROM requests", [], []
        for column, value in (("state", state), ("device", device)):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        if since is not None:
            where.append("COALESCE(settled_at, created_at) >= ?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql + " ORDER BY created_at DESC", args)]
        for row in rows:
            row["fields"] = json.loads(row["fields"])
        return rows
