"""muse.ledger schema 2: migration from schema 1, per-app and paid queries on the local day, holds."""

from __future__ import annotations

import multiprocessing
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from muse.ledger import HOLD_SECONDS, SCHEMA_VERSION, Ledger

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")

SCHEMA_1 = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT NOT NULL, deployment_id TEXT NOT NULL,
    upstream TEXT NOT NULL, alias TEXT, ok INTEGER NOT NULL, status INTEGER, tokens_in INTEGER, tokens_out INTEGER,
    latency_ms INTEGER, privacy TEXT NOT NULL DEFAULT 'standard'
);
CREATE INDEX calls_ts ON calls (ts);
CREATE INDEX calls_upstream_ts ON calls (upstream, ts);
CREATE INDEX calls_provider_ts ON calls (provider, ts);
CREATE INDEX calls_deployment_ts ON calls (deployment_id, ts);
CREATE TABLE blocks (scope TEXT PRIMARY KEY, until REAL NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('cooldown', 'exhausted')),
    reason TEXT NOT NULL, set_at REAL NOT NULL);
CREATE TABLE limits (scope TEXT NOT NULL, window TEXT NOT NULL, metric TEXT NOT NULL, lim INTEGER, remaining INTEGER,
    reset_at REAL, seen_at REAL NOT NULL, PRIMARY KEY (scope, window, metric));
INSERT INTO meta VALUES ('schema_version', '1');
"""


def local(text: str, tz=TPE) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


NOW = local("2026-11-12T15:00:00")


def old_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_1)
    conn.execute("INSERT INTO calls (ts, provider, deployment_id, upstream, alias, ok, status, tokens_in, tokens_out, latency_ms, privacy)"
                 " VALUES (?, 'gemini', 'g__fast', 'gemini:g', 'fast', 1, 200, 10, 5, 12, 'standard')", (NOW - 60,))
    conn.execute("INSERT INTO blocks VALUES ('gemini:g', ?, 'exhausted', 'daily', ?)", (NOW + 3600, NOW))
    conn.commit()
    conn.close()
    return path


def test_migrates_a_schema_1_file(tmp_path):
    path = old_db(tmp_path / "old.sqlite")
    db = Ledger(path, clock=lambda: NOW)
    assert db.schema_version() == SCHEMA_VERSION == 2
    old = db.recent(1)[0]
    assert old["tokens_in"] == 10 and old["app"] is None and old["cost_usd"] is None and old["paid"] is None
    assert "gemini:g" in db.active_blocks()
    db.record_call(provider="p", deployment_id="d", upstream="p:u", ok=True, app="bot", priority="interactive", cost_usd=0.25, paid=True)
    assert db.recent(1)[0]["app"] == "bot"
    names = {row[1] for row in sqlite3.connect(path).execute("PRAGMA index_list(calls)")}
    assert {"calls_app_ts", "calls_paid_ts", "calls_interactive_ok"} <= names
    assert db.usage_by_app(now=NOW, tz=TPE)[None]["requests"] == 1  # pre-apps rows group under None


def test_migration_is_idempotent_and_a_newer_schema_is_still_refused(tmp_path):
    path = old_db(tmp_path / "old.sqlite")
    Ledger(path).recent()
    from muse import ledger as ledger_mod

    ledger_mod._initialised.clear()
    Ledger(path).recent()  # second init: columns exist, nothing re-added
    cols = [row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(calls)")]
    assert cols.count("app") == 1
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    ledger_mod._initialised.clear()
    with pytest.raises(RuntimeError, match="newer"):
        Ledger(path).recent()


def _open(path: str) -> None:
    Ledger(path).recent()


def test_two_processes_migrating_at_once(tmp_path):
    for attempt in range(3):
        path = old_db(tmp_path / f"race{attempt}.sqlite")
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_open, args=(str(path),)) for _ in range(4)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)
            assert proc.exitcode == 0
        cols = [row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(calls)")]
        assert [c for c in cols if c in ("app", "priority", "cost_usd", "paid")] == ["app", "priority", "cost_usd", "paid"]


@pytest.fixture()
def db(tmp_path):
    return Ledger(tmp_path / "l.sqlite", clock=lambda: NOW)


def call(db, ts, app="bot", ok=True, tokens=(10, 5), cost=None, paid=False, priority="interactive", upstream="p:u"):
    db.record_call(provider="p", deployment_id="d", upstream=upstream, ok=ok, tokens_in=tokens[0], tokens_out=tokens[1],
                   app=app, priority=priority, cost_usd=cost, paid=paid, now=ts)


def test_app_usage_is_the_local_day(db):
    call(db, local("2026-11-12T00:00:00"))  # first second of today
    call(db, local("2026-11-12T23:59:59"))  # last second of today (future relative to NOW still counts as today)
    call(db, local("2026-11-11T23:59:59"))  # yesterday
    call(db, local("2026-11-13T00:00:00"))  # tomorrow
    call(db, local("2026-11-12T10:00:00"), ok=False)  # failures do not count
    call(db, local("2026-11-12T10:00:00"), app="other")
    usage = db.app_usage("bot", now=NOW, tz=TPE)
    assert usage == {"requests": 2, "tokens": 30, "cost_usd": 0.0, "unknown_cost_calls": 0}
    assert db.app_usage("bot", now=NOW, tz=ZoneInfo("UTC"))["requests"] == 2  # UTC day: 08:00 Taipei .. 08:00 next day


@pytest.mark.parametrize("day, hours", [("2026-03-08", 23), ("2026-11-01", 25)])
def test_dst_days(tmp_path, day, hours):
    start = local(f"{day}T00:00:00", NY)
    noon = local(f"{day}T12:00:00", NY)
    db = Ledger(tmp_path / f"{day}.sqlite", clock=lambda: noon)  # retention follows the clock
    lo, hi = db.local_day(noon, NY)
    assert lo == start and hi - lo == hours * 3600
    call(db, hi - 1)  # last second of the long/short day
    call(db, hi)  # next day
    call(db, lo - 1)
    assert db.app_usage("bot", now=noon, tz=NY)["requests"] == 1


def test_local_day_follows_muse_tz(tmp_path):
    db = Ledger(tmp_path / "tz.sqlite", clock=lambda: NOW, environ={"MUSE_TZ": "Asia/Taipei", "MUSE_HOME": str(tmp_path)})
    assert db.local_day() == (local("2026-11-12T00:00:00"), local("2026-11-13T00:00:00"))


def test_paid_spend_and_unknown_costs(db):
    call(db, NOW - 10, paid=True, cost=0.25)
    call(db, NOW - 9, paid=True, cost=0.5, app="other")
    call(db, NOW - 8, paid=True, cost=None)  # unknown cost
    call(db, NOW - 7, paid=True, cost=None, ok=False)  # failed: not billed
    call(db, NOW - 6, paid=False, cost=0.0)
    call(db, local("2026-11-11T12:00:00"), paid=True, cost=9.0)  # yesterday
    spend = db.paid_spend(now=NOW, tz=TPE)
    assert spend == {"usd": 0.75, "calls": 3, "unknown_cost_calls": 1, "held_usd": 0.0}
    by_app = db.usage_by_app(now=NOW, tz=TPE)
    assert by_app["bot"]["paid_calls"] == 2 and by_app["bot"]["unknown_cost_calls"] == 1 and by_app["bot"]["failures"] == 1
    assert by_app["other"]["cost_usd"] == 0.5
    assert db.app_usage("bot", now=NOW, tz=TPE)["unknown_cost_calls"] == 1


def test_last_interactive_success(db):
    call(db, NOW - 300, upstream="a")
    call(db, NOW - 50, upstream="a")
    call(db, NOW - 40, upstream="b", ok=False)  # failure: not "used"
    call(db, NOW - 30, upstream="c", priority="background")
    call(db, NOW - 20, upstream="d", priority=None)
    assert db.last_interactive_success(NOW - 120) == {"a": NOW - 50}


def test_paid_holds_are_atomic_and_released(db):
    ok, spend = db.try_hold_paid("c1", "pp", 0.4, 1.0, now=NOW, tz=TPE)
    assert ok and spend == 0
    ok, spend = db.try_hold_paid("c2", "pp", 0.4, 1.0, now=NOW, tz=TPE)
    assert ok and spend == pytest.approx(0.4)
    ok, spend = db.try_hold_paid("c3", "pp", 0.4, 1.0, now=NOW, tz=TPE)
    assert ok and spend == pytest.approx(0.8)  # below the cap: may cross it
    ok, spend = db.try_hold_paid("c4", "pp", 0.4, 1.0, now=NOW, tz=TPE)
    assert not ok and spend == pytest.approx(1.2)
    assert db.paid_spend(now=NOW, tz=TPE)["held_usd"] == pytest.approx(1.2)
    db.record_call(provider="p", deployment_id="pp", upstream="p:u", ok=True, cost_usd=0.1, paid=True, release_hold="c1", now=NOW)
    spend = db.paid_spend(now=NOW, tz=TPE)
    assert spend["usd"] == pytest.approx(0.1) and spend["held_usd"] == pytest.approx(0.8)
    db.release_hold("c2")
    db.release_hold("c3")
    assert db.try_hold_paid("c4", "pp", 0.4, 1.0, now=NOW, tz=TPE)[0]
    # A retry of the same call replaces its own hold instead of counting twice.
    assert db.try_hold_paid("c4", "pp", 0.4, 1.0, now=NOW, tz=TPE) == (True, pytest.approx(0.1))
    # Stale holds (a crashed worker) stop counting.
    assert db.paid_spend(now=NOW + HOLD_SECONDS + 1, tz=TPE)["held_usd"] == 0


def _holder(path: str, worker: int, out) -> None:
    db = Ledger(path)
    admitted = 0
    for i in range(10):
        ok, _ = db.try_hold_paid(f"w{worker}-{i}", "pp", 0.1, 1.0, now=NOW, tz=TPE)
        admitted += ok
    out.put(admitted)


def test_holds_across_processes_never_overshoot(tmp_path):
    path = str(tmp_path / "holds.sqlite")
    Ledger(path).recent()
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_holder, args=(path, w, queue)) for w in range(4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    total = sum(queue.get() for _ in procs)
    assert total == 10  # 0.0, 0.1, ... 0.9 are all < 1.0; the 11th sees 1.0


def test_rations_and_limits_many(db):
    db.record_ration(app="batch", priority="background", alias="fast", kind="defer", code="yield", now=NOW)
    db.record_ration(app="batch", priority="background", alias="fast", kind="defer", code="yield", now=NOW)
    db.record_ration(app="bot", priority="interactive", alias="fast", kind="refuse", code="paid-cap", now=NOW - 86400)
    assert db.ration_counts(now=NOW, tz=TPE) == {("batch", "defer", "yield"): 2}
    from muse.quota import ObservedLimit

    db.record_limits("a", [ObservedLimit("day", "requests", 100, 20, NOW + 60)], now=NOW)
    db.record_limits("provider:x", [ObservedLimit("minute", "tokens", 10, 1, None)], now=NOW)
    many = db.limits_many(["a", "provider:x", "missing"])
    assert many["a"][0]["limit"] == 100 and many["provider:x"][0]["remaining"] == 1 and "missing" not in many
    assert db.limits_many([]) == {}


def test_queries_use_indexes(db):
    conn = sqlite3.connect(db.path)
    db.recent()
    def plan(sql, *args):
        return " ".join(str(row[-1]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "calls_app_ts" in plan("SELECT COUNT(*) FROM calls WHERE app=? AND ok=1 AND ts>=? AND ts<?", "x", 0, 1)
    assert "calls_paid_ts" in plan("SELECT SUM(cost_usd) FROM calls WHERE paid=1 AND ok=1 AND ts>=? AND ts<?", 0, 1)
    assert "calls_interactive_ok" in plan(
        "SELECT upstream, MAX(ts) FROM calls WHERE priority='interactive' AND ok=1 AND ts>=? GROUP BY upstream", 0)
