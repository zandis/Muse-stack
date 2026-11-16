"""muse.ledger: schema, blocks, usage windows, pruning, concurrency."""

from __future__ import annotations

import multiprocessing
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wardmuse import paths
from wardmuse.ledger import RETENTION_SECONDS, SCHEMA_VERSION, Ledger
from wardmuse.quota import ObservedLimit


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-11-12T07:00:30")


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture()
def db(tmp_path: Path, clock: Clock) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", clock=clock)


def call(db: Ledger, **overrides):
    args = dict(provider="gemini", deployment_id="g__fast", upstream="gemini:g", ok=True, status=200,
                alias="fast", tokens_in=10, tokens_out=5, latency_ms=12, privacy="standard")
    args.update(overrides)
    db.record_call(**args)


def test_default_path_uses_muse_home(_isolated_muse_home: Path):
    assert Ledger().path == paths.ledger_path() == _isolated_muse_home / "ledger.sqlite"


def test_schema_is_idempotent_and_versioned(tmp_path: Path):
    path = tmp_path / "l.sqlite"
    Ledger(path).recent()
    Ledger(path).recent()
    assert Ledger(path).schema_version() == SCHEMA_VERSION
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    columns = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    assert columns == {"id", "ts", "provider", "deployment_id", "upstream", "alias", "ok", "status",
                       "tokens_in", "tokens_out", "latency_ms", "privacy",
                       "app", "priority", "cost_usd", "paid", "cached"}  # no column for text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_newer_schema_is_refused(tmp_path: Path):
    path = tmp_path / "l.sqlite"
    Ledger(path).recent()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    path2 = tmp_path / "copy.sqlite"
    path2.write_bytes(path.read_bytes())
    with pytest.raises(RuntimeError, match="newer"):
        Ledger(path2).recent()


def test_record_and_recent(db: Ledger, clock: Clock):
    call(db)
    clock.now += 1
    call(db, ok=False, status=429, tokens_in=None, tokens_out=None)
    rows = db.recent(10)
    assert [r["status"] for r in rows] == [429, 200]  # newest first
    assert rows[1]["tokens_in"] == 10 and rows[1]["latency_ms"] == 12
    assert db.recent(1)[0]["status"] == 429
    assert db.last_call(upstream="gemini:g")["status"] == 429


def test_usage_windows(db: Ledger):
    call(db, now=ts("2026-11-12T07:00:10"))  # this minute
    call(db, now=ts("2026-11-12T06:59:50"))  # previous minute, same day
    call(db, now=ts("2026-11-11T23:00:00"))  # yesterday UTC, same Pacific day
    call(db, now=ts("2026-11-02T00:00:00"))  # this month
    call(db, now=ts("2026-10-31T00:00:00"))  # last month
    call(db, now=ts("2026-11-12T07:00:20"), ok=False, status=500)  # failures do not count
    call(db, now=ts("2026-11-12T07:00:20"), provider="groq", upstream="groq:q")
    usage = db.usage(provider="gemini", now=NOW)
    assert usage["minute"] == {"requests": 1, "tokens": 15}
    assert usage["day"]["requests"] == 3  # Gemini day starts 08:00Z on the 11th
    assert usage["month"]["requests"] == 4
    assert db.usage(provider="gemini", now=NOW, day_provider="groq")["day"]["requests"] == 2  # UTC day
    assert db.usage(upstream="groq:q", now=NOW)["minute"]["requests"] == 1
    assert db.usage(now=NOW)["minute"]["requests"] == 2


def test_success_rate(db: Ledger):
    assert db.success_rate("g__fast", 0) is None
    call(db)
    call(db, ok=False, status=500)
    assert db.success_rate("g__fast", 0) == 0.5
    assert db.success_rate(since=0, upstream="gemini:g") == 0.5
    assert db.success_rate("g__fast", NOW + 1) is None


def test_blocks_keep_the_longest_and_expire(db: Ledger, clock: Clock):
    assert db.set_block("gemini:g", NOW + 3600, "exhausted", "daily")
    assert not db.set_block("gemini:g", NOW + 30, "cooldown", "rpm")  # cannot shorten
    assert db.active_blocks()["gemini:g"]["kind"] == "exhausted"
    assert db.set_block("gemini:g", NOW + 7200, "exhausted", "later")
    assert db.active_blocks()["gemini:g"]["until"] == NOW + 7200
    clock.now = NOW + 8000
    assert db.active_blocks() == {}
    assert db.set_block("gemini:g", NOW + 8030, "cooldown", "new")  # expired one is replaced
    with pytest.raises(ValueError):
        db.set_block("x", NOW, "forever", "")


def test_clear_block_only_expired_cooldown(db: Ledger, clock: Clock):
    db.set_block("a", NOW + 30, "cooldown", "rpm")
    db.set_block("b", NOW + 3600, "exhausted", "day")
    assert not db.clear_block("a", only_expired_cooldown=True)  # still active
    assert not db.clear_block("b", only_expired_cooldown=True)
    clock.now = NOW + 60
    assert db.clear_block("a", only_expired_cooldown=True)
    assert not db.clear_block("b", only_expired_cooldown=True)  # exhausted never cleared by success
    assert db.clear_block("b")
    assert db.active_blocks() == {}


def test_limits_upsert(db: Ledger, clock: Clock):
    db.record_limits("groq:q", [ObservedLimit("minute", "tokens", 6000, 5900, NOW + 7)])
    clock.now += 5
    db.record_limits("groq:q", [ObservedLimit("minute", "tokens", 6000, 100, NOW + 9), ObservedLimit("day", "requests", 1000, 999, None)])
    rows = {(r["window"], r["metric"]): r for r in db.limits("groq:q")}
    assert rows[("minute", "tokens")]["remaining"] == 100 and rows[("minute", "tokens")]["seen_at"] == NOW + 5
    assert rows[("day", "requests")]["limit"] == 1000
    db.record_limits("groq:q", [])
    assert db.limits("other") == []


def test_prune_and_opportunistic_prune(db: Ledger, clock: Clock):
    call(db, now=NOW - RETENTION_SECONDS - 10)  # first write also prunes (no last_prune yet)
    assert db.recent() == []
    clock.now = NOW
    call(db)
    db.set_block("old", NOW - RETENTION_SECONDS - 1, "cooldown", "x", now=NOW - RETENTION_SECONDS - 100)
    assert db.prune(NOW - RETENTION_SECONDS) == 0
    assert len(db.recent()) == 1
    # Once a day a write prunes on its own.
    clock.now = NOW + RETENTION_SECONDS + 2 * 86400
    call(db)
    assert len(db.recent()) == 1


def _writer(path: str, n: int, worker: int) -> None:
    ledger = Ledger(path)
    for i in range(n):
        ledger.record_call(provider="p", deployment_id=f"w{worker}", upstream="p:u", ok=True, status=200, tokens_in=1, tokens_out=1)
        if i % 10 == 0:
            ledger.set_block(f"s{worker}", 9e12, "cooldown", "x")
            ledger.active_blocks()


def test_concurrent_processes(tmp_path: Path):
    path = str(tmp_path / "shared.sqlite")
    Ledger(path).recent()
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(path, 60, w)) for w in range(4)]
    for proc in procs:
        proc.start()
    reader = Ledger(path)
    while any(p.is_alive() for p in procs):
        reader.recent(5)
        reader.usage(provider="p")
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert len(reader.recent(1000)) == 240
    assert set(reader.active_blocks()) == {"s0", "s1", "s2", "s3"}
