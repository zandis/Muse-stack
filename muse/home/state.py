"""The home's own small bookkeeping: who queued a job, and skill runs started through the API.

One SQLite file, `MUSE_HOME/home-state.sqlite` (0600, rollback journal, busy
timeout, secure_delete), apart from the queue (which this module never opens),
the cards and the records. Every method opens its own short transaction.

Three tables:

  job_owners    job id -> the device that queued it through the API. The queue
                itself has no such column; a job queued with `muse queue add`
                has no row here and belongs to the person. This is what lets a
                `host` device see and cancel only its own jobs.

  skill_runs    one row per `POST /v1/skills/{name}/run`: who started it, its
                status and, once finished, its result as JSON. A result may
                hold the person's content (the skill's answer), so it is bounded,
                erased with the row, and returned only to the device that
                started the run or to one that sees everything.

  skill_writes  a file a finished run proposed, waiting for its `save_file`
                card. It holds the bytes until the card is approved (the home
                then writes the file) or declined / expired (the bytes are
                erased). `claim` is a compare-and-set, so a write is applied at
                most once however many answers or sweeps race.

Everything is bounded: old finished runs and old job rows are pruned on insert.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from muse import paths

MAX_JOB_OWNERS = 5000
MAX_RUNS_KEPT = 200
MAX_RESULT_BYTES = 256 * 1024
RUN_STATUSES = ("running", "done", "partial", "needs_choice", "failed")
WRITE_STATES = ("pending", "applying", "written", "refused", "discarded")
RUN_ID_CHARS = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_owners (
  job_id TEXT PRIMARY KEY,
  device TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_runs (
  id TEXT PRIMARY KEY,
  skill TEXT NOT NULL,
  device TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  finished_at REAL,
  result TEXT                        -- JSON; may hold the person's content
);
CREATE INDEX IF NOT EXISTS skill_runs_device ON skill_runs (device, created_at);
CREATE TABLE IF NOT EXISTS skill_writes (
  card_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  skill TEXT NOT NULL,
  root TEXT NOT NULL,                -- the output folder the file must stay inside
  path TEXT NOT NULL,                -- absolute, as the run proposed it
  size INTEGER NOT NULL,
  data BLOB,                         -- the file's bytes; erased once settled
  state TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  settled_at REAL
);
CREATE INDEX IF NOT EXISTS skill_writes_run ON skill_writes (run_id);
"""


def state_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "home-state.sqlite"


class HomeState:
    """Access to home-state.sqlite. Safe across threads and processes."""

    def __init__(self, environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 path: Path | None = None) -> None:
        self.clock = clock
        self.path = Path(path) if path is not None else state_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600))  # 0600 before sqlite touches it
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

    # ---------------------------------------------------------------- job owners

    def set_job_owner(self, job_id: str, device: str) -> None:
        with self._conn(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO job_owners (job_id, device, created_at) VALUES (?,?,?)",
                         (job_id, device, self.clock()))
            conn.execute("DELETE FROM job_owners WHERE job_id NOT IN "
                         "(SELECT job_id FROM job_owners ORDER BY created_at DESC, rowid DESC LIMIT ?)", (MAX_JOB_OWNERS,))

    def job_owner(self, job_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT device FROM job_owners WHERE job_id=?", (job_id,)).fetchone()
        return None if row is None else str(row["device"])

    def jobs_of(self, device: str) -> set[str]:
        with self._conn() as conn:
            return {str(r["job_id"]) for r in conn.execute("SELECT job_id FROM job_owners WHERE device=?", (device,))}

    # ---------------------------------------------------------------- skill runs

    def new_run(self, skill: str, device: str, role: str) -> str:
        run_id = secrets.token_hex(RUN_ID_CHARS // 2)
        with self._conn(write=True) as conn:
            conn.execute("INSERT INTO skill_runs (id, skill, device, role, status, created_at) VALUES (?,?,?,?, 'running', ?)",
                         (run_id, skill, device, role, self.clock()))
            # Keep the newest finished runs; a run with a write still waiting for its card is never pruned.
            old = conn.execute(
                "SELECT id FROM skill_runs WHERE status != 'running' AND id NOT IN "
                "(SELECT run_id FROM skill_writes WHERE state IN ('pending', 'applying')) "
                "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?", (MAX_RUNS_KEPT,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM skill_writes WHERE run_id=?", (row["id"],))
                conn.execute("DELETE FROM skill_runs WHERE id=?", (row["id"],))
        return run_id

    def finish_run(self, run_id: str, status: str, result: Mapping[str, Any]) -> None:
        if status not in RUN_STATUSES or status == "running":
            raise ValueError("a finished run is done, partial, needs_choice or failed")
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            kept = {k: result[k] for k in ("skill", "check", "used", "options", "refusal", "privacy") if k in result}
            kept["output"] = None
            kept["notes"] = ["The answer was too large to keep on the home for this call. Run the skill with `muse skills run` to see it."]
            text = json.dumps(kept, ensure_ascii=False, default=str)
        with self._conn(write=True) as conn:
            conn.execute("UPDATE skill_runs SET status=?, finished_at=?, result=? WHERE id=? AND status='running'",
                         (status, self.clock(), text, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM skill_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        try:
            out["result"] = json.loads(row["result"]) if row["result"] else None
        except ValueError:
            out["result"] = None
        return out

    def running(self, device: str | None = None) -> list[str]:
        sql, args = "SELECT id FROM skill_runs WHERE status='running'", []
        if device is not None:
            sql, args = sql + " AND device=?", [device]
        with self._conn() as conn:
            return [str(r["id"]) for r in conn.execute(sql, args)]

    # ---------------------------------------------------------------- proposed writes

    def add_write(self, *, card_id: str, run_id: str, skill: str, root: str, path: str, data: bytes) -> None:
        with self._conn(write=True) as conn:
            conn.execute(
                "INSERT INTO skill_writes (card_id, run_id, skill, root, path, size, data, state, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', ?)", (card_id, run_id, skill, root, path, len(data), data, self.clock()))

    def claim_write(self, card_id: str) -> dict[str, Any] | None:
        """Take the pending write of a card for settling. None when there is none or another caller took it."""
        with self._conn(write=True) as conn:
            cur = conn.execute("UPDATE skill_writes SET state='applying' WHERE card_id=? AND state='pending'", (card_id,))
            if cur.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM skill_writes WHERE card_id=?", (card_id,)).fetchone()
        return dict(row)

    def settle_write(self, card_id: str, state: str, reason: str = "") -> None:
        """Record how a claimed write ended and erase its bytes."""
        if state not in ("written", "refused", "discarded"):
            raise ValueError("a write ends as written, refused or discarded")
        with self._conn(write=True) as conn:
            conn.execute("UPDATE skill_writes SET state=?, reason=?, data=NULL, settled_at=? WHERE card_id=? AND state='applying'",
                         (state, reason[:200], self.clock(), card_id))

    def write_for(self, card_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT card_id, run_id, skill, root, path, size, state, reason, created_at, settled_at "
                               "FROM skill_writes WHERE card_id=?", (card_id,)).fetchone()
        return None if row is None else dict(row)

    def writes(self, run_id: str | None = None, states: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Proposed writes without their bytes, oldest first."""
        sql = "SELECT card_id, run_id, skill, root, path, size, state, reason, created_at, settled_at FROM skill_writes"
        where, args = [], []
        if run_id is not None:
            where.append("run_id=?")
            args.append(run_id)
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, rowid", args)]
