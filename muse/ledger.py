"""SQLite usage ledger shared by proxy workers, the CLI and the dashboard.

Why these choices:
- journal_mode=DELETE, not WAL: WAL needs shared memory that does not work
  reliably across a Docker bind mount (host CLI + container workers).
- A new connection per operation, a busy_timeout and short IMMEDIATE
  transactions, so several processes can write without long lock holds.
- Only counts, ids, statuses and times are stored. There is no column that
  could hold prompt or response text, and no key values.
- Every method takes `now` (or uses the injected clock) so tests control time.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from muse import paths
from muse.quota import day_start, month_start

SCHEMA_VERSION = 1
RETENTION_SECONDS = 90 * 86400
PRUNE_EVERY_SECONDS = 86400
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    provider TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    upstream TEXT NOT NULL,
    alias TEXT,
    ok INTEGER NOT NULL,
    status INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    latency_ms INTEGER,
    privacy TEXT NOT NULL DEFAULT 'standard'
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE INDEX IF NOT EXISTS calls_upstream_ts ON calls (upstream, ts);
CREATE INDEX IF NOT EXISTS calls_provider_ts ON calls (provider, ts);
CREATE INDEX IF NOT EXISTS calls_deployment_ts ON calls (deployment_id, ts);
CREATE TABLE IF NOT EXISTS blocks (
    scope TEXT PRIMARY KEY,
    until REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('cooldown', 'exhausted')),
    reason TEXT NOT NULL,
    set_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS limits (
    scope TEXT NOT NULL,
    window TEXT NOT NULL,
    metric TEXT NOT NULL,
    lim INTEGER,
    remaining INTEGER,
    reset_at REAL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (scope, window, metric)
);
"""

_initialised: set[str] = set()
_init_lock = threading.Lock()


class Ledger:
    """Thin, process-safe access to the ledger file."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else paths.ledger_path(environ)
        self.clock = clock

    # ------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if fresh:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        key = str(self.path.resolve())
        if key not in _initialised or fresh:
            with _init_lock:
                self._init_schema(conn)
                _initialised.add(key)
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"ledger schema {row['value']} is newer than this code ({SCHEMA_VERSION})")
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

    def _now(self, now: float | None) -> float:
        return float(self.clock() if now is None else now)

    def schema_version(self) -> int:
        with self._read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"])

    # ------------------------------------------------------------ calls

    def record_call(
        self,
        *,
        provider: str,
        deployment_id: str,
        upstream: str,
        ok: bool,
        status: int | None = None,
        alias: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
        privacy: str = "standard",
        now: float | None = None,
    ) -> None:
        """One upstream attempt. Counts only; never content."""
        ts = self._now(now)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO calls (ts, provider, deployment_id, upstream, alias, ok, status,"
                " tokens_in, tokens_out, latency_ms, privacy) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, str(provider), str(deployment_id), str(upstream), alias, 1 if ok else 0,
                    _opt_int(status), _opt_int(tokens_in), _opt_int(tokens_out), _opt_int(latency_ms), str(privacy),
                ),
            )
            last = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
            due = last is None or ts - float(last["value"]) >= PRUNE_EVERY_SECONDS
        if due:
            self.prune(ts - RETENTION_SECONDS, now=ts)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM calls ORDER BY ts DESC, id DESC LIMIT ?", (max(0, int(n)),)).fetchall()
        return [dict(row) for row in rows]

    def success_rate(self, deployment: str | None = None, since: float = 0.0, *, upstream: str | None = None) -> float | None:
        """Share of ok calls since `since` for a deployment id or an upstream; None if no calls."""
        column, value = ("upstream", upstream) if upstream is not None else ("deployment_id", deployment)
        with self._read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n, COALESCE(SUM(ok), 0) AS good FROM calls WHERE {column}=? AND ts>=?",
                (value, since),
            ).fetchone()
        return None if not row["n"] else row["good"] / row["n"]

    def last_call(self, *, upstream: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT ts, status, ok FROM calls WHERE upstream=? ORDER BY ts DESC, id DESC LIMIT 1", (upstream,)
            ).fetchone()
        return dict(row) if row else None

    def usage(
        self,
        *,
        provider: str | None = None,
        upstream: str | None = None,
        now: float | None = None,
        day_provider: str | None = None,
    ) -> dict[str, dict[str, int]]:
        """Successful requests and tokens in the current minute, quota day and calendar month.

        The quota day starts at midnight Pacific for Gemini and UTC midnight
        otherwise (pass `day_provider`, default `provider`).
        """
        ts = self._now(now)
        starts = {
            "minute": ts - (ts % 60),
            "day": day_start(ts, day_provider or provider),
            "month": month_start(ts),
        }
        where, args = ["ok=1"], []
        if provider is not None:
            where.append("provider=?")
            args.append(provider)
        if upstream is not None:
            where.append("upstream=?")
            args.append(upstream)
        out: dict[str, dict[str, int]] = {}
        with self._read() as conn:
            for window, start in starts.items():
                row = conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS t"
                    f" FROM calls WHERE {' AND '.join(where)} AND ts>=? AND ts<=?",
                    (*args, start, ts),
                ).fetchone()
                out[window] = {"requests": int(row["n"]), "tokens": int(row["t"])}
        return out

    # ------------------------------------------------------------ blocks

    def set_block(self, scope: str, until: float, kind: str, reason: str, now: float | None = None) -> bool:
        """Block `scope` until `until`. An active block that lasts longer is kept,
        so a short cooldown can never shorten an exhausted quota. Returns True if written."""
        if kind not in ("cooldown", "exhausted"):
            raise ValueError(f"bad block kind {kind!r}")
        ts = self._now(now)
        with self._write() as conn:
            row = conn.execute("SELECT until FROM blocks WHERE scope=?", (scope,)).fetchone()
            if row is not None and row["until"] > ts and row["until"] >= until:
                return False
            conn.execute(
                "INSERT INTO blocks (scope, until, kind, reason, set_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET until=excluded.until, kind=excluded.kind,"
                " reason=excluded.reason, set_at=excluded.set_at",
                (scope, float(until), kind, str(reason)[:300], ts),
            )
        return True

    def clear_block(self, scope: str, *, only_expired_cooldown: bool = False, now: float | None = None) -> bool:
        """Remove a block. With only_expired_cooldown, an active block or an
        `exhausted` one is left alone (a late success must not reopen it)."""
        ts = self._now(now)
        with self._write() as conn:
            if only_expired_cooldown:
                cur = conn.execute("DELETE FROM blocks WHERE scope=? AND kind='cooldown' AND until<=?", (scope, ts))
            else:
                cur = conn.execute("DELETE FROM blocks WHERE scope=?", (scope,))
            return cur.rowcount > 0

    def active_blocks(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        """scope -> {until, kind, reason, set_at} for blocks still in force."""
        ts = self._now(now)
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM blocks WHERE until>? ORDER BY until", (ts,)).fetchall()
        return {row["scope"]: dict(row) for row in rows}

    # ------------------------------------------------------------ observed limits

    def record_limits(self, scope: str, limits: list[Any], now: float | None = None) -> None:
        """Store ObservedLimit-like objects (window, metric, limit, remaining, reset_at)."""
        if not limits:
            return
        ts = self._now(now)
        with self._write() as conn:
            for item in limits:
                conn.execute(
                    "INSERT INTO limits (scope, window, metric, lim, remaining, reset_at, seen_at) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(scope, window, metric) DO UPDATE SET lim=excluded.lim,"
                    " remaining=excluded.remaining, reset_at=excluded.reset_at, seen_at=excluded.seen_at",
                    (scope, item.window, item.metric, _opt_int(item.limit), _opt_int(item.remaining), item.reset_at, ts),
                )

    def limits(self, scope: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM limits WHERE scope=? ORDER BY window, metric", (scope,)).fetchall()
        return [{**dict(row), "limit": row["lim"]} for row in rows]

    # ------------------------------------------------------------ housekeeping

    def prune(self, older_than: float, now: float | None = None) -> int:
        """Delete calls, stale limits and long-expired blocks older than `older_than`."""
        ts = self._now(now)
        with self._write() as conn:
            removed = conn.execute("DELETE FROM calls WHERE ts<?", (older_than,)).rowcount
            conn.execute("DELETE FROM limits WHERE seen_at<?", (older_than,))
            conn.execute("DELETE FROM blocks WHERE until<?", (older_than,))
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_prune', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (repr(ts),),
            )
        return removed


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
