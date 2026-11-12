"""SQLite usage ledger shared by proxy workers, the CLI and the dashboard.

Why these choices:
- journal_mode=DELETE, not WAL: WAL needs shared memory that does not work
  reliably across a Docker bind mount (host CLI + container workers).
- A new connection per operation, a busy_timeout and short IMMEDIATE
  transactions, so several processes can write without long lock holds.
- Only counts, ids, statuses and times are stored. There is no column that
  could hold prompt or response text, and no key values.
- Every method takes `now` (or uses the injected clock) so tests control time.
- Schema changes are additive (ALTER TABLE ... ADD COLUMN inside the same
  IMMEDIATE transaction that creates the tables), so an old file opens, and two
  processes starting at once serialise on the write lock: the second sees the
  columns already there and skips them.
- "Today" for per-app caps and the paid cap is the user's local calendar day
  (MUSE_TZ, default the system zone), not a provider's quota day.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import tzinfo
from typing import Any, Callable, Iterator, Mapping

from muse import paths
from muse.quota import day_start, month_start

SCHEMA_VERSION = 2
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
    privacy TEXT NOT NULL DEFAULT 'standard',
    app TEXT,
    priority TEXT,
    cost_usd REAL,
    paid INTEGER
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
CREATE TABLE IF NOT EXISTS rations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    app TEXT,
    priority TEXT,
    alias TEXT,
    kind TEXT NOT NULL,
    code TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rations_ts ON rations (ts);
"""

# Columns added after schema 1, in order: (name, SQL type). Old files get them by ALTER.
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("app", "TEXT"),
    ("priority", "TEXT"),
    ("cost_usd", "REAL"),
    ("paid", "INTEGER"),
)

# Indexes on added columns; created after the ALTERs so they exist on old files too.
_INDEXES = """
CREATE INDEX IF NOT EXISTS calls_app_ts ON calls (app, ts);
CREATE INDEX IF NOT EXISTS calls_paid_ts ON calls (ts) WHERE paid=1;
CREATE INDEX IF NOT EXISTS calls_interactive_ok ON calls (upstream, ts) WHERE priority='interactive' AND ok=1;
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
        self.environ = environ

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
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone() else None
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError(f"ledger schema {row['value']} is newer than this code ({SCHEMA_VERSION})")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
            for name, sql_type in _ADDED_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {sql_type}")
            for statement in _INDEXES.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
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
        app: str | None = None,
        priority: str | None = None,
        cost_usd: float | None = None,
        paid: bool | None = None,
    ) -> None:
        """One upstream attempt. Counts only; never content. cost_usd None = unknown."""
        ts = self._now(now)
        with self._write() as conn:
            conn.execute(
                "INSERT INTO calls (ts, provider, deployment_id, upstream, alias, ok, status,"
                " tokens_in, tokens_out, latency_ms, privacy, app, priority, cost_usd, paid)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, str(provider), str(deployment_id), str(upstream), alias, 1 if ok else 0,
                    _opt_int(status), _opt_int(tokens_in), _opt_int(tokens_out), _opt_int(latency_ms), str(privacy),
                    None if app is None else str(app), None if priority is None else str(priority),
                    _opt_float(cost_usd), None if paid is None else (1 if paid else 0),
                ),
            )
            last = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
            clock_now = self._now(None)  # retention follows the clock, not a back-dated record
            due = last is None or clock_now - float(last["value"]) >= PRUNE_EVERY_SECONDS
        if due:
            self.prune(clock_now - RETENTION_SECONDS, now=clock_now)

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

    # ------------------------------------------------------------ per-app and paid (local day)

    def _tz(self, tz: tzinfo | None) -> tzinfo:
        if tz is not None:
            return tz
        from muse.ration import resolve_tz

        env = os.environ if self.environ is None else self.environ
        return resolve_tz(env.get("MUSE_TZ", "").strip())

    def local_day(self, now: float | None = None, tz: tzinfo | None = None) -> tuple[float, float]:
        """[start, end) of the user's local calendar day (MUSE_TZ) containing `now`."""
        from muse.ration import local_day_bounds

        return local_day_bounds(self._now(now), self._tz(tz))

    def app_usage(self, app: str, *, now: float | None = None, tz: tzinfo | None = None) -> dict[str, Any]:
        """Today's successful calls for one app: {requests, tokens, cost_usd, unknown_cost_calls}."""
        start, end = self.local_day(now, tz)
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),0) AS t,"
                " COALESCE(SUM(cost_usd),0) AS c, COALESCE(SUM(CASE WHEN paid=1 AND cost_usd IS NULL THEN 1 ELSE 0 END),0) AS u"
                " FROM calls WHERE app=? AND ok=1 AND ts>=? AND ts<?",
                (app, start, end),
            ).fetchone()
        return {"requests": int(row["n"]), "tokens": int(row["t"]), "cost_usd": float(row["c"]), "unknown_cost_calls": int(row["u"])}

    def usage_by_app(self, *, now: float | None = None, tz: tzinfo | None = None) -> dict[str | None, dict[str, Any]]:
        """Today's usage for every app that made a call (None = calls from before apps existed).

        {app: {requests, failures, tokens, cost_usd, paid_calls, unknown_cost_calls, last_ts}}
        """
        start, end = self.local_day(now, tz)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT app, SUM(ok) AS n, SUM(1-ok) AS f,"
                " COALESCE(SUM(CASE WHEN ok=1 THEN COALESCE(tokens_in,0)+COALESCE(tokens_out,0) ELSE 0 END),0) AS t,"
                " COALESCE(SUM(cost_usd),0) AS c, COALESCE(SUM(CASE WHEN paid=1 AND ok=1 THEN 1 ELSE 0 END),0) AS p,"
                " COALESCE(SUM(CASE WHEN paid=1 AND ok=1 AND cost_usd IS NULL THEN 1 ELSE 0 END),0) AS u, MAX(ts) AS last"
                " FROM calls WHERE ts>=? AND ts<? GROUP BY app ORDER BY app",
                (start, end),
            ).fetchall()
        return {
            row["app"]: {
                "requests": int(row["n"] or 0), "failures": int(row["f"] or 0), "tokens": int(row["t"]),
                "cost_usd": float(row["c"]), "paid_calls": int(row["p"]), "unknown_cost_calls": int(row["u"]),
                "last_ts": row["last"],
            }
            for row in rows
        }

    def paid_spend(self, *, now: float | None = None, tz: tzinfo | None = None) -> dict[str, Any]:
        """Today's paid spend across all apps: {usd, calls, unknown_cost_calls}.

        `usd` sums known costs only; `unknown_cost_calls` are paid calls whose
        cost could not be computed (shown separately, never guessed).
        """
        start, end = self.local_day(now, tz)
        with self._read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c,"
                " COALESCE(SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END),0) AS u"
                " FROM calls WHERE paid=1 AND ts>=? AND ts<?",
                (start, end),
            ).fetchone()
        return {"usd": float(row["c"]), "calls": int(row["n"]), "unknown_cost_calls": int(row["u"])}

    def last_interactive_success(self, since: float) -> dict[str, float]:
        """upstream -> time of the latest successful call by an interactive app since `since`."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT upstream, MAX(ts) AS last FROM calls WHERE priority='interactive' AND ok=1 AND ts>=?"
                " GROUP BY upstream",
                (since,),
            ).fetchall()
        return {row["upstream"]: float(row["last"]) for row in rows}

    def record_ration(
        self, *, app: str | None, priority: str | None, alias: str | None, kind: str, code: str, now: float | None = None
    ) -> None:
        """One request rationing turned away (defer or refuse). Counts only."""
        with self._write() as conn:
            conn.execute(
                "INSERT INTO rations (ts, app, priority, alias, kind, code) VALUES (?,?,?,?,?,?)",
                (self._now(now), app, priority, alias, str(kind), str(code)),
            )

    def ration_counts(self, *, now: float | None = None, tz: tzinfo | None = None) -> dict[tuple[str | None, str, str], int]:
        """Today's turned-away requests: (app, kind, code) -> count."""
        start, end = self.local_day(now, tz)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT app, kind, code, COUNT(*) AS n FROM rations WHERE ts>=? AND ts<? GROUP BY app, kind, code",
                (start, end),
            ).fetchall()
        return {(row["app"], row["kind"], row["code"]): int(row["n"]) for row in rows}

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
            conn.execute("DELETE FROM rations WHERE ts<?", (older_than,))
            conn.execute("DELETE FROM limits WHERE seen_at<?", (older_than,))
            conn.execute("DELETE FROM blocks WHERE until<?", (older_than,))
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_prune', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (repr(ts),),
            )
        return removed


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None
