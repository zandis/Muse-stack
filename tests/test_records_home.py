"""The home's chain: the device key file, the SQLite store, appends from several processes, and `muse records`."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

import wardmuse.records as records  # noqa: E402
from wardmuse import cli, queue  # noqa: E402
from wardmuse.records import commands, home, vectors  # noqa: E402
from wardmuse.records.errors import RecordError  # noqa: E402
from wardmuse.records.home import DeviceKeyFile, HomeRecords  # noqa: E402
from wardmuse.records.store import SqliteRecordStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self, now: float = 1_794_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def run(argv: list[str], capsys) -> tuple[int, str, str]:
    capsys.readouterr()
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------- device key


def test_device_key_is_created_once_and_is_private(_isolated_muse_home):
    path = home.device_key_path()
    assert DeviceKeyFile.load(path) is None
    first = DeviceKeyFile.load_or_create(path)
    assert mode(path) == 0o600 and path.parent == _isolated_muse_home
    again = DeviceKeyFile.load_or_create(path)
    assert again.device == first.device and len(first.device) == 64
    assert again.sign(b"m") == first.sign(b"m") and again.mac(b"m") == first.mac(b"m")
    assert sorted(p.name for p in _isolated_muse_home.iterdir()) == ["device.key"]  # no temporary file left behind
    data = json.loads(path.read_text())
    assert data["seed"] != data["ref_key"] and data["type"] == "muse.device-key"


def test_a_damaged_key_file_is_an_error_that_does_not_echo_it(_isolated_muse_home):
    path = home.device_key_path()
    path.write_text('{"type": "muse.device-key", "v": 1, "seed": "SECRETSEED", "ref_key": "00"}')
    with pytest.raises(ValueError) as caught:
        DeviceKeyFile.load(path)
    assert "SECRETSEED" not in str(caught.value)


# ---------------------------------------------------------------- store


def test_sqlite_store_keeps_a_chain_and_refuses_forks(tmp_path):
    store = SqliteRecordStore(tmp_path / "r.sqlite")
    chain = vectors.make_chain(vectors.ENTRIES)
    assert store.len() == 0 and store.head() is None and store.get(0) is None and store.all() == []
    assert mode(tmp_path / "r.sqlite") == 0o600
    for record in chain[:3]:
        store.append(record)
    assert store.len() == 3 and store.head() == chain[2] and store.get(1) == chain[1] and store.all() == chain[:3]
    for bad, kind in ((chain[2], "fork"), (chain[4], "missing"), (vectors.resign(chain[3], prev="00" * 32), "wrong_prev"),
                      ({**chain[3], "v": 2}, "malformed")):
        with pytest.raises(RecordError) as caught:
            store.append(bad)
        assert caught.value.kind == kind
    assert store.len() == 3
    # A second store object on the same file sees the same chain.
    assert SqliteRecordStore(tmp_path / "r.sqlite").head() == chain[2]
    assert records.verify_chain(store.all(), vectors.DEVICE_A) == 3


def test_append_with_signs_under_the_write_lock(tmp_path):
    store = SqliteRecordStore(tmp_path / "r.sqlite")
    seen = []

    def make(head):
        seen.append(head)
        return records.append(head, vectors.ENTRIES[len(seen) - 1], bytes.fromhex(vectors.SEED_A), bytes.fromhex(vectors.REF_KEY))

    first = store.append_with(make)
    second = store.append_with(make)
    assert seen == [None, first] and second["seq"] == 1 and second["prev"] == records.hash(first)

    def fails(head):
        raise RuntimeError("signing failed")

    with pytest.raises(RuntimeError):
        store.append_with(fails)
    assert store.len() == 2  # nothing half-written


_WORKER = """
import sys
from wardmuse.records.home import HomeRecords
home = HomeRecords()
for i in range(int(sys.argv[2])):
    home.write("files.write", f"worker {sys.argv[1]} wrote item {i}", actor="helper", resource=f"file:/w{sys.argv[1]}/{i}")
"""


def test_processes_appending_at_once_never_fork_the_chain(_isolated_muse_home):
    """Real processes, a fresh home (so they also race to create the key): one device, one unbroken chain."""
    workers, each = 5, 20
    env = {**os.environ, "PYTHONPATH": str(ROOT), "MUSE_HOME": str(_isolated_muse_home)}
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(n), str(each)], env=env, stderr=subprocess.PIPE) for n in range(workers)]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode()
    chain = HomeRecords().records()
    assert len(chain) == workers * each
    assert records.verify_chain(chain, HomeRecords().device()) == workers * each
    assert [r["seq"] for r in chain] == list(range(workers * each))
    assert len({r["device"] for r in chain}) == 1
    summaries = sorted(r["summary"] for r in chain)
    assert summaries == sorted(f"worker {n} wrote item {i}" for n in range(workers) for i in range(each))
    for n in range(workers):  # each worker's own records are in its order
        mine = [r["summary"] for r in chain if r["summary"].startswith(f"worker {n} ")]
        assert mine == [f"worker {n} wrote item {i}" for i in range(each)]
    assert HomeRecords().verify()["size"] == workers * each


def test_threads_appending_at_once(_isolated_muse_home):
    errors = []

    def work(n: int) -> None:
        try:
            h = HomeRecords()
            for i in range(10):
                h.write("llm.call", f"thread {n} call {i}", actor="helper")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert HomeRecords().verify()["size"] == 60


# ---------------------------------------------------------------- the home's chain


def test_home_write_verify_seal(_isolated_muse_home):
    clock = Clock()
    h = HomeRecords(clock=clock)
    assert h.verify() == {"size": 0, "device": None, "checkpoint": None}
    assert not home.device_key_path().exists()  # verifying does not create a key
    first = h.write("home.pause", "Paused the queue")
    assert (first["seq"], first["actor"], first["mandate"], first["resource"], first["time"]) == (0, "person", None, None, 1_794_000_000_000)
    assert mode(home.records_path()) == 0o600 and mode(home.device_key_path()) == 0o600
    clock.now -= 3600  # the clock is set back an hour: the chain must stay valid
    second = h.write("home.resume", "Resumed the queue")
    assert second["time"] == first["time"]
    clock.now += 7200
    h.write("pay.spend", "Paid NT$30 for a lookup", actor="helper", mandate="m-1", resource="payee:example", cost={"currency": "TWD", "minor": 3000},
            links={"job": "job-1"})
    state = h.verify()
    assert state["size"] == 3 and state["device"] == first["device"] and state["checkpoint"] is None
    cp = h.seal()
    assert cp["size"] == 3 and h.verify()["checkpoint"] == cp
    with pytest.raises(RecordError) as caught:
        h.write("llm.call", "used sk-test0000000000000000", actor="helper")
    assert caught.value.kind == "summary" and h.verify()["size"] == 3


def _edit(sql: str, *args) -> None:
    conn = sqlite3.connect(str(home.records_path()))
    with conn:
        conn.execute(sql, args)
    conn.close()


def test_tampering_with_the_file_is_found(_isolated_muse_home):
    h = HomeRecords()
    for i in range(6):
        h.write("files.write", f"Wrote file {i}", actor="helper", resource=f"file:/{i}")
    h.seal()

    def problem() -> tuple[str, int | None]:
        with pytest.raises(RecordError) as caught:
            HomeRecords().verify()
        return caught.value.kind, caught.value.index

    body = lambda seq: sqlite3.connect(str(home.records_path())).execute("SELECT body FROM records WHERE seq=?", (seq,)).fetchone()[0]  # noqa: E731
    original = body(2)
    _edit("UPDATE records SET body=? WHERE seq=2", original.replace("Wrote file 2", "Wrote file 9"))
    assert problem() == ("summary_mismatch", 2)
    _edit("UPDATE records SET body=? WHERE seq=2", original.replace('"outcome":"ok"', '"outcome":"failed"'))
    assert problem() == ("bad_signature", 2)
    _edit("UPDATE records SET body=? WHERE seq=2", "not json")
    assert problem() == ("malformed", 2)
    _edit("UPDATE records SET body=? WHERE seq=2", original)
    assert HomeRecords().verify()["size"] == 6
    _edit("DELETE FROM records WHERE seq=3")
    assert problem() == ("missing", 3)
    _edit("INSERT INTO records (seq, time, action, hash, body) VALUES (3, 0, 'x.y', '', ?)", body(4))
    assert problem() == ("missing", 3)
    _edit("DELETE FROM records WHERE seq>=3")
    assert problem() == ("size_mismatch", None)  # the tail was cut below the seal
    with pytest.raises(RecordError):
        HomeRecords().seal()


def test_a_chain_cut_before_any_seal_cannot_be_noticed(_isolated_muse_home):
    """Stated limit: without a checkpoint kept somewhere, removing the newest records leaves a valid chain."""
    h = HomeRecords()
    for i in range(4):
        h.write("home.pause", f"Paused {i}")
    _edit("DELETE FROM records WHERE seq>=2")
    assert HomeRecords().verify()["size"] == 2


def test_home_export_filters_and_redaction(_isolated_muse_home, tmp_path):
    h = HomeRecords()
    h.write("home.pause", "Paused the queue")
    h.write("llm.call", "Asked a question", actor="helper", mandate="m-1", cost={"currency": "TWD", "minor": 30})
    h.write("llm.call", "Asked another", actor="helper", mandate="m-1")
    everything = h.export()
    assert records.verify_export(everything, h.device())["count"] == 3
    some = h.export(commands.make_filter(None, "llm", False), redact_summaries=True)
    report = records.verify_export(some)
    assert (report["count"], report["size"], report["complete"], report["redacted"]) == (2, 3, False, 2)
    paid = h.export(commands.make_filter(None, None, True))
    assert [r["summary"] for r in paid["records"]] == ["Asked a question"]


# ---------------------------------------------------------------- CLI


def test_pause_and_resume_leave_records(capsys):
    assert run(["records", "list"], capsys)[1] == "No records yet.\n"
    code, out, err = run(["pause"], capsys)
    assert code == 0 and "Paused." in out and err == "" and queue.is_paused()
    code, out, err = run(["resume"], capsys)
    assert code == 0 and err == "" and not queue.is_paused()
    assert run(["resume"], capsys)[0] == 0  # was not paused: nothing happened, nothing recorded
    chain = HomeRecords().records()
    assert [(r["action"], r["actor"], r["outcome"], r["summary"]) for r in chain] == [
        ("home.pause", "person", "ok", "Paused the queue"), ("home.resume", "person", "ok", "Resumed the queue")]
    code, out, _ = run(["records", "list"], capsys)
    assert code == 0 and out.index("Resumed the queue") < out.index("Paused the queue") and "home.pause, by you" in out
    code, out, _ = run(["records", "list", "--action", "home.resume", "--json"], capsys)
    assert code == 0 and [r["seq"] for r in json.loads(out)] == [1]


def test_pause_works_when_records_cannot_be_written(capsys, monkeypatch, _isolated_muse_home):
    def broken(*args, **kwargs):
        raise OSError("disk full at /secret/path")

    monkeypatch.setattr(commands, "note_home_event", broken)
    code, out, err = run(["pause"], capsys)
    assert code == 0 and "Paused." in out and queue.is_paused()
    assert err == "Note: no record was written (OSError).\n"  # the type only
    # Also when the records file itself is unusable.
    monkeypatch.undo()
    monkeypatch.setenv("MUSE_HOME", str(_isolated_muse_home))
    home.records_path().write_text("this is not a database")
    code, out, err = run(["resume"], capsys)
    assert code == 0 and "Resumed." in out and not queue.is_paused()
    assert err.startswith("Note: no record was written (") and "not a database" not in err


def test_records_verify_and_seal(capsys):
    code, out, _ = run(["records", "verify"], capsys)
    assert code == 0 and "All records check out (0 records)." in out and "Not sealed yet" in out
    run(["pause"], capsys)
    run(["resume"], capsys)
    code, out, _ = run(["records", "seal"], capsys)
    assert code == 0 and "Sealed 2 records" in out
    code, out, _ = run(["records", "verify"], capsys)
    assert code == 0 and "All records check out (2 records)." in out and "Last sealed " in out and HomeRecords().device() in out
    code, out, _ = run(["records", "verify", "--json"], capsys)
    assert json.loads(out)["ok"] is True and json.loads(out)["sealed"]["size"] == 2
    _edit("UPDATE records SET body=replace(body, 'Resumed', 'Resumee') WHERE seq=1")
    code, out, _ = run(["records", "verify"], capsys)
    assert code == 1 and out.startswith("Record 1 does not match") and "What to do" in out
    code, out, _ = run(["records", "verify", "--json"], capsys)
    assert code == 1 and json.loads(out) == {"ok": False, "kind": "summary_mismatch", "index": 1}
    code, out, _ = run(["records", "list"], capsys)
    assert code == 1 and "Record 1 does not match" in out and "Paused the queue" in out
    code, out, _ = run(["records", "export", "x.json"], capsys)
    assert code == 1 and "Nothing was exported" in out


def test_records_list_filters(capsys, monkeypatch):
    clock = Clock()
    h = HomeRecords(clock=clock)
    h.write("home.pause", "Paused the queue")
    clock.now += 3 * 86400
    h.write("llm.call", "Asked a question", actor="helper", mandate="m-7", cost={"currency": "TWD", "minor": 3000})
    h.write("files.write", "Could not write", actor="helper", outcome="failed")
    monkeypatch.setattr(commands.time, "time", lambda: clock.now + 60)
    out = run(["records", "list"], capsys)[1]
    assert "under m-7, TWD 30.00" in out and "[failed]" in out and "by the helper" in out and out.count("\n") == 5  # two days, three records
    assert run(["records", "list", "--cost-only"], capsys)[1].count("#") == 1
    assert run(["records", "list", "--since", "1d"], capsys)[1].count("#") == 2
    assert run(["records", "list", "--since", "2026-01-01"], capsys)[1].count("#") == 3
    assert run(["records", "list", "--action", "files"], capsys)[1].count("#") == 1
    assert run(["records", "list", "--action", "file"], capsys)[1] == "No records match.\n"
    code, _, err = run(["records", "list", "--since", "yesterday"], capsys)
    assert code == 2 and "--since" in err


def test_records_list_left_device(capsys):
    h = HomeRecords(clock=Clock())
    assert run(["records", "list", "--left-device"], capsys)[1] == "No records yet.\n"
    h.write("home.pause", "Paused the queue", egress="none")
    h.write("llm.call", "Asked Gemini a question", actor="helper", egress="service:gemini")
    h.write("llm.call", "Answered on the home", actor="helper", egress="home")
    h.write("skill.run", "Ran a skill for a host bot", actor="helper", mandate="device:bot-tools", egress="host:bot-tools")
    h.write("files.write", "Wrote a file", actor="helper")  # an older writer: it does not say
    everything = run(["records", "list"], capsys)[1]
    assert everything.count("#") == 5 and "sent to gemini" in everything and "given to the host bot bot-tools" in everything
    assert "sent to your home" in everything and "Paused the queue  (home.pause, by you)" in everything
    code, out, _ = run(["records", "list", "--left-device"], capsys)
    assert code == 0 and out.count("#") == 2 and "Asked Gemini" in out and "Ran a skill" in out
    assert "Paused" not in out and "Answered on the home" not in out and "Wrote a file" not in out
    assert "1 record does not say where the content went" in out
    code, out, _ = run(["records", "list", "--left-device", "--json"], capsys)
    assert [r["egress"] for r in json.loads(out)] == ["service:gemini", "host:bot-tools"]
    assert run(["records", "list", "--left-device", "--action", "skill"], capsys)[1].count("#") == 1
    assert commands.make_filter(None, None, False, left_device=False) is None


def test_pause_and_resume_say_that_nothing_left(capsys):
    run(["pause"], capsys)
    run(["resume"], capsys)
    assert [(r["action"], r["egress"]) for r in HomeRecords().records()] == [("home.pause", "none"), ("home.resume", "none")]
    assert home.record_pause(None, True)["summary"] == "Paused the queue"  # the function the home API uses too


def test_export_and_check_file(capsys, tmp_path):
    for _ in range(3):
        run(["pause"], capsys)
        run(["resume"], capsys)
    full, part = tmp_path / "all.json", tmp_path / "part.json"
    code, out, _ = run(["records", "export", str(full)], capsys)
    device = HomeRecords().device()
    assert code == 0 and "Exported all 6 of 6 records" in out and device in out and mode(full) == 0o600
    code, out, err = run(["records", "export", str(full)], capsys)
    assert code == 1 and "exists" in err
    code, out, _ = run(["records", "export", str(part), "--action", "home.pause", "--redact-summaries"], capsys)
    assert code == 0 and "Exported the matching 3 of 6 records" in out and "summaries removed" in out
    assert "Paused" not in part.read_text() and "Paused" in full.read_text()
    code, out, _ = run(["records", "check-file", str(full)], capsys)
    assert code == 0 and "The file checks out: all 6 records." in out and device in out and "Compare that key" in out
    code, out, _ = run(["records", "check-file", str(part), "--device", device], capsys)
    assert code == 0 and "3 of 6 records" in out and "3 records have the summary text removed" in out and "Compare" not in out
    code, out, _ = run(["records", "check-file", str(part), "--device", "00" * 32], capsys)
    assert code == 1 and "another device" in out
    code, out, _ = run(["records", "check-file", str(part), "--json"], capsys)
    assert json.loads(out) == {"ok": True, "device": device, "size": 6, "count": 3, "complete": False, "redacted": 3}
    # Someone edits the file.
    tampered = tmp_path / "t.json"
    tampered.write_text(full.read_text().replace("Resumed the queue", "Resumed the Queue", 1))
    code, out, _ = run(["records", "check-file", str(tampered)], capsys)
    assert code == 1 and "does not check out" in out and "summary_mismatch" in out
    data = json.loads(full.read_text())
    data["records"].pop(2)
    tampered.write_text(json.dumps(data))
    code, out, _ = run(["records", "check-file", str(tampered)], capsys)
    assert code == 1 and "Entry 2" in out and "(missing)" in out
    tampered.write_text("{}")
    assert run(["records", "check-file", str(tampered)], capsys)[0] == 1
    code, _, err = run(["records", "check-file", str(tmp_path / "absent.json")], capsys)
    assert code == 1 and "cannot read" in err


def test_an_export_checks_out_on_another_home(capsys, tmp_path, monkeypatch):
    """`check-file` needs nothing from the home that made the file."""
    run(["pause"], capsys)
    target = tmp_path / "mine.json"
    assert run(["records", "export", str(target)], capsys)[0] == 0
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "another-home"))
    code, out, _ = run(["records", "check-file", str(target)], capsys)
    assert code == 0 and "checks out" in out
    assert not (tmp_path / "another-home" / "device.key").exists()


def test_format_cost():
    assert commands.format_cost({"currency": "TWD", "minor": 3000}) == "TWD 30.00"
    assert commands.format_cost({"currency": "JPY", "minor": 1500}) == "JPY 1,500"
    assert commands.format_cost({"currency": "USDC", "minor": 1_250_000}) == "USDC 1.250000"
    assert commands.format_cost({"currency": "XTS", "minor": 5}) == "5 minor units of XTS"
    assert commands.format_cost(None) == ""
