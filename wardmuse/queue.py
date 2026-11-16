"""The background queue: non-urgent jobs that run on allowance that would otherwise go unused.

Why a separate file from the usage ledger: a job holds the user's prompt text (or
a path and an instruction). The ledger must never hold content, so jobs live in
`MUSE_HOME/queue.sqlite` (0600, rollback journal, busy timeout). The ledger sees
only what it always sees: counts, tokens, status. Results go to files under
`MUSE_HOME/queue-out/` (0600) or a path the user names; never to the ledger,
the queue's metadata columns, or any log line. A finished job's prompt text is
erased from the database.

Who decides what: the proxy does all rationing (muse/ration.py). This runner
only sends through the proxy as a *background* app and does what the answer says.
A job may carry a routing task (`muse queue add --task light`, or the default the
rules file gives the `queue` app); it is sent as `x-muse-task` and the proxy picks
the alias and caps the answer length (muse/rules.py).

Runner policy (same text as README, kept exact because the tests pin it):

  * Auth: Bearer token of the managed app `queue` (background, never paid). A job
    added with --allow-paid uses the managed app `queue-paid` (background, paid
    allowed, still bound by the daily paid cap and known prices). The runner never
    switches a job to the paid app because a deadline is close.
  * 200: write the output atomically, record provider/deployment/cost/tokens, done.
  * 429 + `x-muse-ration: defer:*`: nothing was sent upstream. Requeue at
    Retry-After + jitter (jitter 0..20 % of it, at most 30 s). Not an attempt.
  * 429 + `refuse:app-requests|app-tokens`: hold every job of that app until
    Retry-After (local midnight) + jitter. Not an attempt.
  * 429 + `refuse:paid-spend-unknown`: retry in ~60 s. Any other `*:paid-*`: retry at
    Retry-After if given, else next local midnight. Not an attempt.
  * 429 without `x-muse-ration`: a provider block or upstream limit. With
    `x-muse-wait: provider-limit` and a Retry-After, the proxy knows when the
    provider's block ends: retry at Retry-After (cap 6 h) + jitter. Otherwise
    exponential backoff (30 s * 2^(attempt-1), cap 1 h, jittered to 50..100 %),
    never earlier than Retry-After (cap 6 h). Either way it counts as an attempt.
  * 403 whose message starts `[muse]`: privacy refusal, failed (`privacy-refused`), no retry.
  * 401: the token was revoked. The runner re-checks its managed app once (rotating
    its own token if the registry changed); if still 401 it stops and leaves the job queued.
  * other 4xx: failed. 5xx, timeout, unreadable 200: backoff as above, an attempt.
  * The proxy is unreachable (connection refused or timed out): wait 15 s * 2^n (cap 5 min)
    with jitter; not an attempt, because the job did nothing wrong.
  * A job past its deadline is failed with reason `deadline` (also while waiting).
  * Pause (`MUSE_HOME/paused`): nothing is sent while the file exists. It stops the
    queue, not the proxy.
  * One runner at a time (flock on `queue.lock`); jobs are claimed with a compare-and-set.
    A job left `running` by a dead runner is requeued (at once on start-up, or after
    STALE_RUNNING_S) and counts one attempt.
  * No busy polling: the runner sleeps until the earliest next_attempt_at, hold or
    deadline, in slices of at most 5 s that only look at the local pause file and
    queue database. It sends no request while waiting.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import math
import os
import random
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from wardmuse import apps, keys, paths

KINDS = ("prompt", "file-summary", "batch")
STATUSES = ("queued", "running", "deferred", "done", "failed", "cancelled")
WAITING = ("queued", "deferred")
FINISHED = ("done", "failed", "cancelled")

QUEUE_APP = "queue"
QUEUE_PAID_APP = "queue-paid"
TOKEN_VARS = {QUEUE_APP: "MUSE_QUEUE_APP_TOKEN", QUEUE_PAID_APP: "MUSE_QUEUE_PAID_APP_TOKEN"}

MAX_FILE_BYTES = 100_000
MAX_PROMPT_CHARS = 100_000
MAX_DIR_FILES = 50
HARD_DIR_FILES = 200
MAX_BATCH_ITEMS = 20
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_TOKENS = 1024
STALE_RUNNING_S = 600.0
REQUEST_TIMEOUT_S = 120.0
SLICE_S = 5.0
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 3600.0
RETRY_AFTER_CAP_S = 6 * 3600.0
DEFAULT_DEFER_S = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  kind TEXT NOT NULL,
  alias TEXT NOT NULL,
  privacy TEXT,
  payload TEXT NOT NULL,            -- prompt text, a file path, or a JSON list: erased when the job finishes
  instruction TEXT,                 -- file-summary: what to do with the file
  out_path TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  deferrals INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  last_reason TEXT,                 -- an x-muse-ration code or error class, never content
  started_at REAL,
  finished_at REAL,
  runner TEXT,
  heartbeat REAL,
  provider TEXT, deployment TEXT, cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER,
  max_attempts INTEGER NOT NULL,
  max_tokens INTEGER NOT NULL,
  deadline REAL,
  allow_paid INTEGER NOT NULL DEFAULT 0,
  progress INTEGER NOT NULL DEFAULT 0,  -- batch: items finished
  partial TEXT,                          -- batch: JSON list of finished outputs (content; erased with payload)
  task TEXT                              -- routing task (config/routing.yaml), sent as x-muse-task; NULL = none
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

PUBLIC_COLUMNS = (
    "id, created_at, kind, alias, privacy, out_path, status, attempts, deferrals, next_attempt_at, last_reason,"
    " started_at, finished_at, provider, deployment, cost_usd, tokens_in, tokens_out, max_attempts, max_tokens,"
    " deadline, allow_paid, progress, task"
)


class QueueError(ValueError):
    """A queue operation that is not valid. The message is safe to show (never content)."""


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- pause


def is_paused(environ: Mapping[str, str] | None = None) -> bool:
    return paths.paused_path(environ).exists()


def set_paused(environ: Mapping[str, str] | None = None, now: float | None = None) -> None:
    path = paths.paused_path(environ)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(iso(time.time() if now is None else now) or "")


def clear_paused(environ: Mapping[str, str] | None = None) -> bool:
    try:
        paths.paused_path(environ).unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- deadlines


_REL = re.compile(r"^(\d+(?:\.\d+)?)\s*(m|min|h|hr|d)$", re.I)


def parse_deadline(text: str, now: float, tz: tzinfo | None = None) -> float:
    """`90m`, `2h`, `1d`, `08:30` (next time it is 08:30), or `2026-11-13[T08:30]` (local time)."""
    raw = text.strip()
    zone = tz or datetime.now().astimezone().tzinfo or timezone.utc
    match = _REL.match(raw)
    if match:
        unit = match.group(2).lower()
        secs = float(match.group(1)) * {"m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}[unit]
        if secs <= 0:
            raise QueueError("the deadline must be in the future")
        return now + secs
    local_now = datetime.fromtimestamp(now, zone)
    clock = re.fullmatch(r"(\d{1,2}):(\d\d)", raw)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        if hour > 23 or minute > 59:
            raise QueueError(f"cannot read the deadline {text!r}")
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target.timestamp()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise QueueError(f"cannot read the deadline {text!r}; try 2h, 90m, 08:30 or 2026-11-13T08:30") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    if parsed.timestamp() <= now:
        raise QueueError("the deadline must be in the future")
    return parsed.timestamp()


# ---------------------------------------------------------------- storage


@dataclass(slots=True)
class Job:
    id: str
    created_at: float
    kind: str
    alias: str
    privacy: str | None
    payload: str
    instruction: str | None
    out_path: str
    status: str
    attempts: int
    deferrals: int
    next_attempt_at: float
    last_reason: str | None
    max_attempts: int
    max_tokens: int
    deadline: float | None
    allow_paid: bool
    progress: int
    partial: str | None
    task: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    provider: str | None = None
    deployment: str | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    runner: str | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """Metadata only: no payload, instruction, partial output."""
        return {
            "id": self.id, "created_at": iso(self.created_at), "kind": self.kind, "alias": self.alias,
            "privacy": self.privacy, "out_path": self.out_path, "status": self.status, "attempts": self.attempts,
            "deferrals": self.deferrals, "next_attempt_at": iso(self.next_attempt_at) if self.status in WAITING else None,
            "last_reason": self.last_reason, "started_at": iso(self.started_at), "finished_at": iso(self.finished_at),
            "provider": self.provider, "deployment": self.deployment, "cost_usd": self.cost_usd,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "max_attempts": self.max_attempts,
            "max_tokens": self.max_tokens, "deadline": iso(self.deadline), "allow_paid": self.allow_paid,
            "progress": self.progress, "task": self.task,
        }


def _job(row: sqlite3.Row) -> Job:
    keys_ = row.keys()
    values = {k: row[k] for k in keys_ if k in Job.__dataclass_fields__}
    values["allow_paid"] = bool(values["allow_paid"])
    return Job(**values)


class Queue:
    """Access to queue.sqlite. Every method opens its own short transaction (safe across processes)."""

    def __init__(self, environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.environ = environ
        self.clock = clock
        self.path = paths.queue_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)  # 0600 before sqlite touches it
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA secure_delete=ON")  # an erased prompt must not linger in free pages
            conn.executescript(SCHEMA)
            if "task" not in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}:
                try:  # a queue.sqlite from before routing tasks; additive, like the ledger
                    conn.execute("ALTER TABLE jobs ADD COLUMN task TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
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

    # ---- adding

    def add(
        self, *, kind: str, payload: str, alias: str = "cheap", privacy: str | None = None, out_path: str | None = None,
        instruction: str | None = None, deadline: float | None = None, allow_paid: bool = False,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS, max_tokens: int = DEFAULT_MAX_TOKENS, force: bool = False,
        task: str | None = None,
    ) -> Job:
        if kind not in KINDS:
            raise QueueError(f"kind must be one of {', '.join(KINDS)}")
        if task is not None and not re.fullmatch(r"[a-z0-9][a-z0-9 _-]{0,63}", task):
            raise QueueError("task must be a task name from the routing rules, such as light or everyday")
        if not alias or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", alias):
            raise QueueError("alias must be a model alias such as cheap or fast")
        if privacy is not None and privacy not in apps.PRIVACY_CLASSES:
            raise QueueError(f"privacy must be one of {', '.join(apps.PRIVACY_CLASSES)}")
        if max_attempts < 1 or max_tokens < 1:
            raise QueueError("max attempts and max tokens must be at least 1")
        now = self.clock()
        if deadline is not None and deadline <= now:
            raise QueueError("the deadline must be in the future")
        job_id = secrets.token_hex(6)
        out = self._resolve_out(out_path, job_id, force)
        with self._conn(write=True) as conn:
            if out_path:
                clash = conn.execute(
                    "SELECT id FROM jobs WHERE out_path=? AND status IN ('queued','running','deferred')", (out,)
                ).fetchone()
                if clash:
                    raise QueueError(f"job {clash['id']} already writes to {out}")
            conn.execute(
                "INSERT INTO jobs (id, created_at, kind, alias, privacy, payload, instruction, out_path, status, next_attempt_at,"
                " max_attempts, max_tokens, deadline, allow_paid, task) VALUES (?,?,?,?,?,?,?,?, 'queued', ?,?,?,?,?,?)",
                (job_id, now, kind, alias, privacy, payload, instruction, out, now, max_attempts, max_tokens, deadline, int(allow_paid),
                 task),
            )
        return self.get(job_id)

    def _resolve_out(self, out_path: str | None, job_id: str, force: bool) -> str:
        if not out_path:
            return str(paths.queue_out_dir(self.environ) / f"{job_id}.txt")
        target = Path(out_path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        if target.is_dir():
            raise QueueError(f"{target} is a directory; give a file path")
        if target.exists() and not force:
            raise QueueError(f"{target} already exists; choose another path or pass --overwrite")
        if not target.parent.is_dir():
            raise QueueError(f"the folder {target.parent} does not exist")
        return str(target)

    # ---- reading

    def get(self, job_id: str) -> Job:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise QueueError(f"no job '{job_id}'")
        return _job(row)

    def find(self, prefix: str) -> Job:
        """A job by id or unique id prefix."""
        if not re.fullmatch(r"[0-9a-f]{1,12}", prefix or ""):
            raise QueueError(f"no job '{prefix}'")
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        if not rows:
            raise QueueError(f"no job '{prefix}'")
        if len(rows) > 1:
            raise QueueError(f"'{prefix}' matches more than one job; type more of the id")
        return _job(rows[0])

    def list(self, statuses: Sequence[str] | None = None, limit: int = 500) -> list[Job]:
        sql, args = "SELECT * FROM jobs", []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        sql += " ORDER BY created_at, id LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, (*args, limit)).fetchall()
        return [_job(r) for r in rows]

    # ---- changes

    def cancel(self, job_id: str) -> Job:
        """Cancel a waiting or running job. A running job's result is dropped when it returns."""
        job = self.find(job_id)
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='cancelled', finished_at=?, payload='', partial=NULL, last_reason='cancelled'"
                " WHERE id=? AND status IN ('queued','deferred','running')",
                (self.clock(), job.id),
            )
            if cur.rowcount == 0:
                raise QueueError(f"job {job.id} is already {job.status}")
        return self.get(job.id)

    def clear(self, statuses: Sequence[str]) -> int:
        bad = [s for s in statuses if s not in FINISHED]
        if bad or not statuses:
            raise QueueError("only finished jobs (done, failed, cancelled) can be cleared")
        with self._conn(write=True) as conn:
            cur = conn.execute(f"DELETE FROM jobs WHERE status IN ({','.join('?' * len(statuses))})", tuple(statuses))
            return cur.rowcount

    # ---- runner support

    def fail_overdue(self, now: float) -> int:
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='failed', last_reason='deadline', finished_at=?, payload='', partial=NULL"
                " WHERE status IN ('queued','deferred') AND deadline IS NOT NULL AND deadline<=?",
                (now, now),
            )
            return cur.rowcount

    def requeue_stale(self, now: float, timeout: float = STALE_RUNNING_S) -> int:
        """Jobs `running` whose runner stopped heartbeating. Counts one attempt (the send may have happened)."""
        with self._conn(write=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET status='queued', runner=NULL, attempts=attempts+1, last_reason='runner-lost', next_attempt_at=?"
                " WHERE status='running' AND COALESCE(heartbeat, 0) <= ?",
                (now, now - timeout),
            )
            n = cur.rowcount
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, payload='', partial=NULL, last_reason='max-attempts:runner-lost'"
                " WHERE status='queued' AND attempts>=max_attempts AND last_reason='runner-lost'",
                (now,),
            )
        return n

    def holds(self) -> dict[str, float]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'hold:%'").fetchall()
        return {r["key"][5:]: float(r["value"]) for r in rows}

    def set_hold(self, app: str, until: float) -> None:
        with self._conn(write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"hold:{app}", repr(float(until))))

    def claim_next(self, now: float, runner: str) -> Job | None:
        """The oldest due job whose app is not on hold, claimed with a compare-and-set."""
        holds = self.holds()
        with self._conn(write=True) as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','deferred') AND next_attempt_at<=? ORDER BY created_at, id", (now,)
            ).fetchall()
            for row in rows:
                if holds.get(app_for(bool(row["allow_paid"])), 0) > now:
                    continue
                cur = conn.execute(
                    "UPDATE jobs SET status='running', runner=?, heartbeat=?, started_at=COALESCE(started_at, ?)"
                    " WHERE id=? AND status IN ('queued','deferred')",
                    (runner, now, now, row["id"]),
                )
                if cur.rowcount == 1:
                    return _job(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        return None

    def next_wake(self, now: float) -> float | None:
        """The earliest moment something can happen: a due time (after any hold) or a deadline."""
        holds = self.holds()
        wake: float | None = None
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT next_attempt_at AS n, allow_paid AS p, deadline AS d FROM jobs WHERE status IN ('queued','deferred')"
            ).fetchall()
        for row in rows:
            due = max(float(row["n"]), holds.get(app_for(bool(row["p"])), 0))
            for candidate in (due, row["d"]):
                if candidate is not None and (wake is None or candidate < wake):
                    wake = float(candidate)
        return wake

    def heartbeat(self, job_id: str, runner: str, now: float) -> None:
        with self._conn(write=True) as conn:
            conn.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND runner=? AND status='running'", (now, job_id, runner))

    def save_progress(self, job_id: str, runner: str, now: float, progress: int, partial: str, tin: int, tout: int, cost: float | None) -> None:
        """Batch: remember finished items (and running totals) so a deferral or crash resumes mid-way."""
        with self._conn(write=True) as conn:
            conn.execute(
                "UPDATE jobs SET progress=?, partial=?, heartbeat=?, tokens_in=?, tokens_out=?, cost_usd=? "
                "WHERE id=? AND runner=? AND status='running'",
                (progress, partial, now, tin, tout, cost, job_id, runner),
            )

    def still_mine(self, job_id: str, runner: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT status, runner FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["status"] == "running" and row["runner"] == runner)

    def settle(self, job_id: str, runner: str, **changes: Any) -> bool:
        """Apply `changes` only if this runner still owns the running job (a cancel wins)."""
        allowed = {
            "status", "attempts", "deferrals", "next_attempt_at", "last_reason", "finished_at", "provider", "deployment",
            "cost_usd", "tokens_in", "tokens_out", "payload", "partial", "progress",
        }
        if set(changes) - allowed:
            raise QueueError("internal: unknown column")
        sets = ", ".join(f"{k}=?" for k in changes)
        with self._conn(write=True) as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {sets}, runner=NULL WHERE id=? AND runner=? AND status='running'",
                (*changes.values(), job_id, runner),
            )
            return cur.rowcount == 1


def app_for(allow_paid: bool) -> str:
    return QUEUE_PAID_APP if allow_paid else QUEUE_APP


# ---------------------------------------------------------------- snapshot stats


def stats(environ: Mapping[str, str] | None = None, now: float | None = None) -> dict[str, Any]:
    """Counts for `muse status` and the dashboard. Read-only; never creates the database."""
    ts = time.time() if now is None else now
    empty: dict[str, Any] = {
        "counts": {s: 0 for s in STATUSES}, "waiting": 0, "next_due_at": None, "oldest_queued_at": None, "hold_until": None,
    }
    path = paths.queue_path(environ)
    if not path.exists():
        return empty
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            counts = dict(empty["counts"])
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
                counts[row["status"]] = int(row["n"])
            nxt = conn.execute("SELECT MIN(next_attempt_at) AS t, MIN(created_at) AS c FROM jobs WHERE status IN ('queued','deferred')").fetchone()
            holds = [float(r["value"]) for r in conn.execute("SELECT value FROM meta WHERE key LIKE 'hold:%'")]
        finally:
            conn.close()
    except sqlite3.Error:
        return empty
    live = [h for h in holds if h > ts]
    due = nxt["t"]
    if due is not None and live:
        due = max(due, min(live))
    return {
        "counts": counts, "waiting": counts["queued"] + counts["deferred"], "next_due_at": iso(due),
        "oldest_queued_at": iso(nxt["c"]), "hold_until": iso(max(live)) if live else None,
    }


# ---------------------------------------------------------------- adding helpers


def read_text_file(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """A small UTF-8 text file, or QueueError. Nothing here includes the file's contents in a message."""
    try:
        size = path.stat().st_size
    except OSError:
        raise QueueError(f"cannot read {path}") from None
    if not path.is_file():
        raise QueueError(f"{path} is not a file")
    if size > limit:
        raise QueueError(f"{path} is {size} bytes; the limit is {limit}")
    try:
        data = path.read_bytes()
        if b"\0" in data:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise QueueError(f"{path} is not a UTF-8 text file") from None


def expand_directory(directory: Path, recursive: bool, limit: int) -> tuple[list[Path], list[str]]:
    """(files, skipped notes). Hidden files and anything not readable text are skipped, never failed on."""
    if limit < 1 or limit > HARD_DIR_FILES:
        raise QueueError(f"--max-files must be between 1 and {HARD_DIR_FILES}")
    candidates = sorted(directory.rglob("*") if recursive else directory.iterdir())
    files: list[Path] = []
    skipped: list[str] = []
    for path in candidates:
        rel = path.relative_to(directory)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file() or path.is_symlink():
            continue
        try:
            read_text_file(path)
        except QueueError as exc:
            skipped.append(str(exc))
            continue
        files.append(path)
    if len(files) > limit:
        raise QueueError(f"{directory} has {len(files)} usable files; the limit is {limit}. Raise --max-files or pick a smaller folder")
    return files, skipped


def split_batch(text: str) -> list[str]:
    """Prompts separated by a line holding only `---`."""
    items = [part.strip() for part in re.split(r"(?m)^---\s*$", text)]
    items = [i for i in items if i]
    if not items:
        raise QueueError("the batch file has no prompts")
    if len(items) > MAX_BATCH_ITEMS:
        raise QueueError(f"a batch holds at most {MAX_BATCH_ITEMS} prompts; split the file")
    return items


# ---------------------------------------------------------------- managed apps


def _env_token(env: Mapping[str, str], name: str) -> str:
    return str(env.get(TOKEN_VARS[name], "") or "").strip()


def ensure_app(name: str, environ: Mapping[str, str]) -> str:
    """The token for managed app `name`, creating or repairing the app first.

    Why: the token is shown nowhere, so the runner owns it. If the registry's hash
    does not match the stored token (first run, `muse apps rotate queue`, deleted
    app), a new token is issued and stored in the key store. The app is kept
    background; `queue` is kept non-paid and `queue-paid` paid, because paid use
    is chosen per job (`--allow-paid`), never inherited.
    """
    want_paid = name == QUEUE_PAID_APP
    token = _env_token(environ, name)
    app = apps.load_apps(environ).get(name)
    if app is not None and (app.priority != "background" or app.allow_paid != want_paid):
        app = apps.update_app(name, environ, priority="background", allow_paid=want_paid)
    if app is not None and token and apps.hash_token(token) == app.token_sha256:
        return token
    if app is None:
        _, token = apps.add_app(
            name, "background", allow_paid=want_paid, environ=environ,
            note="managed by muse queue" + (" (paid jobs only)" if want_paid else ""),
        )
    else:
        _, token = apps.rotate_token(name, environ)
    keys.set_key(TOKEN_VARS[name], token, force=True, environ=environ)
    if isinstance(environ, dict):
        environ[TOKEN_VARS[name]] = token
    return token


# ---------------------------------------------------------------- the runner


@dataclass(slots=True)
class Reply:
    status: int  # 0 = network error or timeout
    headers: dict[str, str]
    body: bytes


def _post(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Reply:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as err:
        try:
            data = err.read()
        except OSError:
            data = b""
        return Reply(err.code, {k.lower(): v for k, v in err.headers.items()}, data)
    except (urllib.error.URLError, OSError, ValueError):
        return Reply(0, {}, b"")


@dataclass(slots=True)
class RunSummary:
    done: int = 0
    failed: int = 0
    deferred: int = 0
    stopped: str = ""


def proxy_url(env: Mapping[str, str]) -> str:
    explicit = str(envget(env, "MUSE_PROXY_URL", "") or "").strip()
    return (explicit or f"http://127.0.0.1:{str(envget(env, 'MUSE_PORT', '') or '').strip() or '4000'}").rstrip("/")


def atomic_write(path: Path, text: str) -> None:
    """Whole file or nothing: temp file in the same folder, 0600, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def runner_lock(environ: Mapping[str, str] | None) -> Iterator[bool]:
    """Exclusive, non-blocking lock for the whole runner. Yields False when another runner holds it."""
    lock_path = paths.home(environ) / "queue.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    got = True
    try:
        try:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                got = False
        except ImportError:  # pragma: no cover - Windows: the compare-and-set claim still prevents double runs
            pass
        yield got
    finally:
        os.close(fd)


class Runner:
    """Drains the queue through the proxy as a background app. Clock, sleep and randomness are injectable."""

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        post: Callable[[str, Mapping[str, str], bytes, float], Reply] = _post,
        log: Callable[[str], None] = lambda line: None,
        tz: tzinfo | None = None,
    ) -> None:
        self.env = environ
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.post = post
        self.log = log
        self.tz = tz
        self.queue = Queue(environ, clock)
        self.id = f"{os.getpid()}-{secrets.token_hex(3)}"
        self.url = proxy_url(environ)
        self.tokens: dict[str, str] = {}

    # ---- main loop

    def run(self, *, once: bool = False, max_jobs: int | None = None, until_idle: bool = False) -> RunSummary:
        summary = RunSummary()
        with runner_lock(self.env) as got:
            if not got:
                summary.stopped = "locked"
                return summary
            self.queue.requeue_stale(self.clock(), timeout=0)  # holding the lock: nothing else is running
            self.token_for(False)  # creates the managed `queue` app on first run
            finished = 0
            try:
                while True:
                    now = self.clock()
                    self.queue.fail_overdue(now)
                    self.queue.requeue_stale(now)
                    if max_jobs is not None and finished >= max_jobs:
                        summary.stopped = "max-jobs"
                        return summary
                    if is_paused(self.env):
                        if once or until_idle:
                            summary.stopped = "paused"
                            return summary
                        self.sleep(SLICE_S)
                        continue
                    job = self.queue.claim_next(now, self.id)
                    if job is None:
                        wake = self.queue.next_wake(now)
                        if once or (wake is None and until_idle):
                            summary.stopped = "idle" if wake is None else "once"
                            return summary
                        wait = SLICE_S if wake is None else min(SLICE_S, max(0.05, wake - now))
                        self.sleep(wait)
                        continue
                    result = self.process(job)
                    if result == "done":
                        summary.done += 1
                        finished += 1
                    elif result == "failed":
                        summary.failed += 1
                        finished += 1
                    elif result == "dropped":
                        continue
                    elif result == "auth":
                        summary.stopped = "auth"
                        return summary
                    else:
                        summary.deferred += 1
            except KeyboardInterrupt:
                summary.stopped = "interrupted"
                return summary

    # ---- one job

    def token_for(self, allow_paid: bool, refresh: bool = False) -> str:
        name = app_for(allow_paid)
        if refresh or name not in self.tokens:
            self.tokens[name] = ensure_app(name, self.env)
        return self.tokens[name]

    def prompts_for(self, job: Job) -> list[str] | str:
        """The prompts to send, or a failure reason string. File contents are read now, not at add time."""
        if job.kind == "prompt":
            return [job.payload]
        if job.kind == "batch":
            try:
                return [str(p) for p in json.loads(job.payload)]
            except (ValueError, TypeError):
                return "bad-payload"
        try:
            text = read_text_file(Path(job.payload))
        except QueueError:
            return "file-unreadable"
        return [f"{job.instruction or 'Summarise'}\n\n---\n{text}"]

    def process(self, job: Job) -> str:
        """Run one claimed job one step. Returns done | failed | deferred | auth."""
        now = self.clock()
        if job.deadline is not None and job.deadline <= now:
            self.queue.settle(job.id, self.id, status="failed", last_reason="deadline", finished_at=now, payload="", partial=None)
            self.log(f"{job.id} failed: deadline")
            return "failed"
        prompts = self.prompts_for(job)
        if isinstance(prompts, str):
            self.queue.settle(job.id, self.id, status="failed", last_reason=prompts, finished_at=now, payload="", partial=None)
            self.log(f"{job.id} failed: {prompts}")
            return "failed"
        outputs: list[str] = json.loads(job.partial) if job.partial else []
        provider = deployment = None
        cost: float | None = job.cost_usd
        tin, tout = job.tokens_in or 0, job.tokens_out or 0
        for index in range(job.progress, len(prompts)):
            if is_paused(self.env):  # pause between the items of a batch too
                self.queue.settle(job.id, self.id, status="deferred", last_reason="paused", next_attempt_at=self.clock() + SLICE_S)
                return "deferred"
            reply = self.send(job, prompts[index])
            if reply.status == 401:
                return self.on_auth(job)
            action = self.classify(job, reply)
            if action["kind"] != "ok":
                return self.apply(job, action, reply)
            data = action["data"]
            outputs.append(data["text"])
            provider = data["provider"] or provider
            deployment = data["deployment"] or deployment
            if data["cost"] is not None:
                cost = (cost or 0.0) + data["cost"]
            tin += data["tin"] or 0
            tout += data["tout"] or 0
            if index + 1 < len(prompts):
                self.queue.save_progress(job.id, self.id, self.clock(), index + 1, json.dumps(outputs), tin, tout, cost)
        return self.finish(job, outputs, provider or job.provider, deployment or job.deployment, cost, tin, tout)

    def send(self, job: Job, prompt: str) -> Reply:
        body = {"model": job.alias, "messages": [{"role": "user", "content": prompt}], "max_tokens": job.max_tokens}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token_for(job.allow_paid)}"}
        if job.privacy:
            headers["x-muse-privacy"] = job.privacy
        if job.task:
            headers["x-muse-task"] = job.task  # the proxy replaces the model with the task's alias
        return self.post(f"{self.url}/v1/chat/completions", headers, json.dumps(body).encode(), REQUEST_TIMEOUT_S)

    def finish(self, job: Job, outputs: list[str], provider: Any, deployment: Any, cost: float | None, tin: int, tout: int) -> str:
        now = self.clock()
        if not self.queue.still_mine(job.id, self.id):  # cancelled while the request was out: drop the result
            self.log(f"{job.id} cancelled while running; result discarded")
            return "dropped"
        out = Path(job.out_path)
        text = outputs[0] if len(outputs) == 1 else "\n\n---\n\n".join(outputs)
        try:
            atomic_write(out, text if text.endswith("\n") else text + "\n")
        except OSError:
            return self.apply(job, {"kind": "retry", "reason": "output-unwritable", "count": True, "delay": None}, None)
        ok = self.queue.settle(
            job.id, self.id, status="done", finished_at=now, last_reason=None, provider=str(provider) if provider else None,
            deployment=str(deployment) if deployment else None, cost_usd=cost, tokens_in=tin, tokens_out=tout,
            payload="", partial=None, progress=len(outputs),
        )
        if not ok:
            try:
                out.unlink()
            except OSError:
                pass
            return "failed"
        self.log(f"{job.id} done via {provider or 'unknown'}")
        return "done"

    # ---- reading the proxy's answer

    def jitter(self, seconds: float) -> float:
        return self.rng.uniform(0, min(30.0, max(1.0, 0.2 * seconds)))

    def midnight(self, now: float) -> float:
        from wardmuse.ration import local_day_bounds, resolve_tz

        tz = self.tz or resolve_tz(str(envget(self.env, "MUSE_TZ", "") or "").strip())
        return local_day_bounds(now, tz)[1]

    def classify(self, job: Job, reply: Reply) -> dict[str, Any]:
        now = self.clock()
        status, headers = reply.status, reply.headers
        ration = headers.get("x-muse-ration", "").strip().lower()
        retry_after = _retry_after(headers.get("retry-after"))
        if status == 200:
            data = _read_completion(reply)
            if data is None:
                return {"kind": "retry", "reason": "bad-response", "count": True, "delay": None}
            data["provider"] = headers.get("x-muse-provider")
            data["deployment"] = headers.get("x-muse-deployment")
            cost = headers.get("x-muse-cost-usd")
            try:
                data["cost"] = float(cost) if cost else None
            except ValueError:
                data["cost"] = None
            return {"kind": "ok", "data": data}
        if status == 429 and ration:
            kind, _, code = ration.partition(":")
            code = code or kind
            if code == "paid-spend-unknown":
                return {"kind": "wait", "reason": f"{kind}:{code}", "until": now + 60 + self.jitter(60)}
            if code in ("app-requests", "app-tokens"):
                until = now + (retry_after if retry_after is not None else max(60.0, self.midnight(now) - now))
                return {"kind": "hold", "reason": f"{kind}:{code}", "until": until + self.jitter(until - now)}
            if code.startswith("paid-") and kind == "refuse":
                until = now + (retry_after if retry_after is not None else max(60.0, self.midnight(now) - now))
                return {"kind": "wait", "reason": f"{kind}:{code}", "until": until + self.jitter(until - now)}
            wait = retry_after if retry_after is not None else (DEFAULT_DEFER_S if kind == "defer" else 3600.0)
            return {"kind": "wait", "reason": f"{kind}:{code}", "until": now + wait + self.jitter(wait)}
        if status == 429:
            if headers.get("x-muse-wait", "").strip().lower() == "provider-limit" and retry_after is not None:
                # The proxy says when the provider's quota block ends: come back then, not on a guess.
                return {"kind": "retry", "reason": "http-429", "count": True, "delay": retry_after, "exact": True}
            return {"kind": "retry", "reason": "http-429", "count": True, "delay": retry_after}
        if status == 403:
            if _message(reply).startswith("[muse]"):
                return {"kind": "fail", "reason": "privacy-refused"}
            return {"kind": "fail", "reason": "http-403"}
        if status == 0:  # the proxy itself is unreachable: not the job's fault, so not an attempt
            wait = min(300.0, 15.0 * 2 ** min(job.deferrals, 5))
            return {"kind": "wait", "reason": "network", "until": now + wait + self.jitter(wait)}
        if status in (408, 425) or status >= 500:
            return {"kind": "retry", "reason": f"http-{status}", "count": True, "delay": retry_after}
        return {"kind": "fail", "reason": f"http-{status}"}

    def apply(self, job: Job, action: dict[str, Any], reply: Reply | None) -> str:
        now = self.clock()
        kind, reason = action["kind"], action["reason"]
        if kind == "fail":
            self.queue.settle(job.id, self.id, status="failed", last_reason=reason, finished_at=now, payload="", partial=None)
            self.log(f"{job.id} failed: {reason}")
            return "failed"
        if kind in ("wait", "hold"):
            until = float(action["until"])
            if kind == "hold":
                self.queue.set_hold(app_for(job.allow_paid), until)
            self.queue.settle(job.id, self.id, status="deferred", last_reason=reason, next_attempt_at=until, deferrals=job.deferrals + 1)
            self.log(f"{job.id} deferred: {reason}, next try in {int(until - now)} s")
            return "deferred"
        attempts = job.attempts + (1 if action["count"] else 0)
        if attempts >= job.max_attempts:
            self.queue.settle(
                job.id, self.id, status="failed", attempts=attempts, last_reason=f"max-attempts:{reason}", finished_at=now,
                payload="", partial=None,
            )
            self.log(f"{job.id} failed: max-attempts:{reason}")
            return "failed"
        if action.get("exact") and action.get("delay") is not None:
            wait = min(RETRY_AFTER_CAP_S, float(action["delay"]))
            delay = wait + self.jitter(wait)
        else:
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** max(0, attempts - 1))
            delay = self.rng.uniform(0.5 * delay, delay)
            if action.get("delay") is not None:
                delay = max(delay, min(RETRY_AFTER_CAP_S, float(action["delay"])))
        self.queue.settle(job.id, self.id, status="deferred", attempts=attempts, last_reason=reason, next_attempt_at=now + delay)
        self.log(f"{job.id} will retry: {reason}, in {int(delay)} s")
        return "deferred"

    def on_auth(self, job: Job) -> str:
        """401: the app token is not accepted. Repair our own app once; if it still fails, stop."""
        name = app_for(job.allow_paid)
        old = self.tokens.get(name)
        self.token_for(job.allow_paid, refresh=True)
        if self.tokens[name] != old:  # the registry had changed under us: the job was not at fault
            self.queue.settle(job.id, self.id, status="queued", last_reason="auth-repaired", next_attempt_at=self.clock())
            self.log("the queue app's token was out of date; re-issued, continuing")
            return "deferred"
        self.queue.settle(job.id, self.id, status="queued", last_reason="auth", next_attempt_at=self.clock())
        self.log("the proxy rejected the queue app's token (HTTP 401); stopping. Check the proxy's master key and `muse apps list`.")
        return "auth"


def _retry_after(value: str | None) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(1.0, min(number, RETRY_AFTER_CAP_S * 4))


def _message(reply: Reply) -> str:
    try:
        payload = json.loads(reply.body or b"{}")
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        return str(error.get("message", "") if isinstance(error, dict) else error).strip()
    except ValueError:
        return ""


def _read_completion(reply: Reply) -> dict[str, Any] | None:
    try:
        payload = json.loads(reply.body)
        text = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    usage = usage if isinstance(usage, dict) else {}

    def num(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {"text": text, "tin": num("prompt_tokens"), "tout": num("completion_tokens")}
