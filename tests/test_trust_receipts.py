"""LocalReceiptLog: hash chain, signatures, signed head, tampering, concurrency, export."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from muse.trust import crypto
from muse.trust.contracts import VaultError, VaultLocked
from muse.trust.receipts import GENESIS, LocalReceiptLog, receipt_hash, verify_export
from muse.trust.vault import LocalVault, iso_utc

PW = "correct horse battery staple"
ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self) -> None:
        self.t = 1_750_000_000.0

    def __call__(self) -> float:
        self.t += 1
        return self.t


@pytest.fixture
def vault(tmp_path: Path) -> LocalVault:
    return LocalVault.create(tmp_path / "vault.sqlite", PW, kdf=crypto.INSECURE_TEST_KDF, clock=Clock())


@pytest.fixture
def log(vault: LocalVault) -> LocalReceiptLog:
    return LocalReceiptLog(vault)


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def fill(log: LocalReceiptLog, n: int) -> None:
    for i in range(n):
        log.append("bot", "vault.read", detail=f"{i} records", egress="local", model="local/qwen", privacy="local")


def test_append_and_chain(log: LocalReceiptLog, vault: LocalVault) -> None:
    rec = vault.add("lab", "抽血報告", "血糖 98")
    r1 = log.append("user", "vault.write", record_refs=(rec.content_sha256,), detail="1 record")
    r2 = log.append("bot", "llm.complete", egress="local", model="ollama/qwen2.5", privacy="local", detail="answered")
    assert (r1.seq, r2.seq) == (1, 2)
    assert r1.prev_hash == GENESIS and r2.prev_hash == r1.hash
    assert r1.hash == receipt_hash(vault.vault_id, r1)
    assert crypto.verify_signature(vault.public_key(), "receipt", bytes.fromhex(r1.hash), crypto.b64d(r1.signature))
    assert r1.ts == iso_utc(1_750_000_003.0)   # ticks: create, add, append
    assert log.list() == [r2, r1]
    v = log.verify()
    assert v.ok and v.count == 2 and v.bad_seq is None


def test_empty_log_verifies(log: LocalReceiptLog) -> None:
    v = log.verify()
    assert v.ok and v.count == 0


def test_record_refs_are_keyed(log: LocalReceiptLog, vault: LocalVault) -> None:
    rec = vault.add("lab", "t", "short lab result")
    r = log.append("bot", "vault.read", record_refs=(rec.content_sha256,))
    assert r.record_refs == (vault.ref_token(rec.content_sha256),)
    assert r.record_refs[0] != rec.content_sha256
    assert log.ref_for(rec.content_sha256) == r.record_refs[0]
    assert rec.content_sha256.encode() not in vault.path.read_bytes()
    plain = LocalReceiptLog(vault, keyed_refs=False).append("bot", "vault.read", record_refs=(rec.content_sha256,))
    assert plain.record_refs == (rec.content_sha256,)
    for bad in ("abc", "G" * 64, rec.content_sha256.upper()):
        with pytest.raises(VaultError):
            log.append("bot", "vault.read", record_refs=(bad,))


def test_validation(log: LocalReceiptLog) -> None:
    bad = [
        dict(actor="root", action="vault.read"), dict(actor="bot", action="Vault Read!"),
        dict(actor="bot", action="vault.read", egress="moon"), dict(actor="bot", action="vault.read", mandate_id="x"),
        dict(actor="bot", action="vault.read", model="a\nb"), dict(actor="bot", action="vault.read", record_refs=("0" * 64,) * 201),
    ]
    for kwargs in bad:
        with pytest.raises(VaultError):
            log.append(**kwargs)  # type: ignore[arg-type]
    assert log.verify().count == 0


def test_detail_rules(log: LocalReceiptLog, vault: LocalVault) -> None:
    vault.add("lab", "甲狀腺功能檢查", "TSH 2.1")
    vault.add("lab", "CBC", "WBC 6000")
    for detail in ("x" * 121, "line\nbreak", "tab\there", "read 甲狀腺功能檢查 for user"):
        with pytest.raises(VaultError):
            log.append("bot", "vault.read", detail=detail)
    log.append("bot", "vault.read", detail="3 records")          # short title "CBC" (<4 chars) not matched
    log.append("bot", "vault.read", detail="x" * 120)
    log.append("bot", "llm.complete", detail="refused: no local model")
    assert log.verify().count == 3


def test_locked_read_and_verify_but_not_append(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 3)
    vault.lock()
    assert log.verify().ok and log.verify().count == 3
    assert len(log.list()) == 3
    with pytest.raises(VaultLocked):
        log.append("bot", "vault.read")
    fresh = LocalReceiptLog(LocalVault.open(vault.path))
    assert fresh.verify().ok


def test_list_pagination(log: LocalReceiptLog) -> None:
    fill(log, 10)
    assert [r.seq for r in log.list(limit=3)] == [10, 9, 8]
    assert [r.seq for r in log.list(limit=3, before_seq=8)] == [7, 6, 5]
    assert [r.seq for r in log.list(limit=50, before_seq=2)] == [1]
    assert log.list(limit=0) == []


@pytest.mark.parametrize("column,value", [
    ("detail", "changed"), ("actor", "user"), ("egress", "cloud"), ("model", "openai/gpt"),
    ("record_refs", json.dumps(["0" * 64])), ("ts", "2020-01-01T00:00:00Z"), ("mandate_id", "f" * 32),
])
def test_modified_field_detected(log: LocalReceiptLog, vault: LocalVault, column: str, value: str) -> None:
    fill(log, 5)
    raw(vault.path).execute(f"UPDATE receipts SET {column}=? WHERE seq=3", (value,))
    v = log.verify()
    assert not v.ok and v.bad_seq == 3


def test_recomputed_hash_without_key_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 3)
    conn = raw(vault.path)
    row = dict(conn.execute("SELECT * FROM receipts WHERE seq=3").fetchone())
    row["detail"] = "forged"
    row["record_refs"] = tuple(json.loads(row["record_refs"]))
    new_hash = receipt_hash(vault.vault_id, row)
    conn.execute("UPDATE receipts SET detail='forged', hash=? WHERE seq=3", (new_hash,))
    v = log.verify()
    assert not v.ok and v.bad_seq == 3 and "signature" in v.reason


def test_deleted_middle_row_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 5)
    raw(vault.path).execute("DELETE FROM receipts WHERE seq=2")
    v = log.verify()
    assert not v.ok and v.bad_seq == 2


def test_deleted_and_renumbered_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 5)
    conn = raw(vault.path)
    conn.execute("DELETE FROM receipts WHERE seq=2")
    for s in (3, 4, 5):
        conn.execute("UPDATE receipts SET seq=? WHERE seq=?", (s - 1, s))
    v = log.verify()
    assert not v.ok and v.bad_seq == 2


def test_reordered_rows_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 5)
    conn = raw(vault.path)
    conn.execute("UPDATE receipts SET seq=-1 WHERE seq=2")
    conn.execute("UPDATE receipts SET seq=2 WHERE seq=3")
    conn.execute("UPDATE receipts SET seq=3 WHERE seq=-1")
    v = log.verify()
    assert not v.ok and v.bad_seq == 2


def test_truncated_tail_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 5)
    raw(vault.path).execute("DELETE FROM receipts WHERE seq>=4")
    v = log.verify()
    assert not v.ok and v.count == 3 and "truncated" in v.reason


def test_everything_deleted_but_head_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 2)
    raw(vault.path).execute("DELETE FROM receipts")
    assert not log.verify().ok


def test_head_removed_or_forged_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 3)
    conn = raw(vault.path)
    head = json.loads(conn.execute("SELECT value FROM meta WHERE key='receipt_head'").fetchone()[0])
    conn.execute("UPDATE meta SET value=? WHERE key='receipt_head'", (json.dumps({**head, "seq": 2}),))
    assert not log.verify().ok
    conn.execute("DELETE FROM meta WHERE key='receipt_head'")
    v = log.verify()
    assert not v.ok and "head" in v.reason


def test_replayed_receipt_detected(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 3)
    conn = raw(vault.path)
    conn.execute("INSERT INTO receipts SELECT 4, ts, actor, action, mandate_id, record_refs, egress, model, privacy,"
                 " detail, prev_hash, hash, signature FROM receipts WHERE seq=1")
    v = log.verify()
    assert not v.ok and v.bad_seq == 4


def test_transplanted_chain_detected(tmp_path: Path, vault: LocalVault, log: LocalReceiptLog) -> None:
    fill(log, 2)
    other = LocalVault.create(tmp_path / "other.sqlite", PW, kdf=crypto.INSECURE_TEST_KDF)
    conn = raw(other.path)
    conn.execute("ATTACH DATABASE ? AS src", (str(vault.path),))
    conn.execute("INSERT INTO receipts SELECT * FROM src.receipts")
    conn.execute("INSERT OR REPLACE INTO meta SELECT * FROM src.meta WHERE key='receipt_head'")
    conn.execute("DETACH DATABASE src")
    assert not LocalReceiptLog(other).verify().ok


def test_rollback_of_whole_file_is_the_documented_residual(log: LocalReceiptLog, vault: LocalVault) -> None:
    fill(log, 2)
    snapshot = vault.path.read_bytes()
    fill(log, 2)
    vault.path.write_bytes(snapshot)
    # Consistent older state: not detectable from inside the file (anchor the head elsewhere).
    v = log.verify()
    assert v.ok and v.count == 2


def test_concurrent_appends_threads_same_object(log: LocalReceiptLog) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(25):
                log.append("bot", "vault.read", detail="1 record")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    v = log.verify()
    assert v.ok and v.count == 100


def test_concurrent_appends_two_vault_objects(vault: LocalVault) -> None:
    other = LocalVault.open(vault.path)
    other.unlock(PW)
    logs = [LocalReceiptLog(vault), LocalReceiptLog(other)]
    errors: list[BaseException] = []

    def worker(lg: LocalReceiptLog) -> None:
        try:
            for _ in range(30):
                lg.append("system", "items.write", detail="1 item")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(lg,)) for lg in logs * 2]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    v = logs[1].verify()
    assert v.ok and v.count == 120


def test_concurrent_appends_two_processes(vault: LocalVault, tmp_path: Path) -> None:
    script = tmp_path / "appender.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from muse.trust.vault import LocalVault\n"
        "from muse.trust.receipts import LocalReceiptLog\n"
        "v = LocalVault.open(sys.argv[1]); v.unlock(sys.stdin.readline().rstrip('\\n'))\n"
        "lg = LocalReceiptLog(v)\n"
        "for _ in range(40): lg.append('system', 'vault.read', detail='child')\n"
    )
    proc = subprocess.Popen([sys.executable, str(script), str(vault.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    proc.stdin.write((PW + "\n").encode())
    proc.stdin.close()
    lg = LocalReceiptLog(vault)
    for _ in range(40):
        lg.append("bot", "vault.read", detail="parent")
    _, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err.decode()[-2000:]
    v = lg.verify()
    assert v.ok and v.count == 80
    assert {r.detail for r in lg.list(limit=100)} == {"child", "parent"}


def test_export_and_verify_export(log: LocalReceiptLog, vault: LocalVault, tmp_path: Path) -> None:
    fill(log, 5)
    out = tmp_path / "receipts.jsonl"
    log.export_jsonl(out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["type"] == "muse.receipts"
    v = verify_export(out, vault.public_key())
    assert v.ok and v.count == 5
    with pytest.raises(FileExistsError):
        log.export_jsonl(out)
    # wrong key
    assert not verify_export(out, crypto.public_key_from_seed(crypto.random_key())).ok

    def variant(name: str, new_lines: list[str]) -> Path:
        p = tmp_path / name
        p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return p

    tampered = [*lines]
    obj = json.loads(tampered[3])
    obj["detail"] = "nothing happened"
    tampered[3] = json.dumps(obj)
    v = verify_export(variant("t.jsonl", tampered), vault.public_key())
    assert not v.ok and v.bad_seq == 3
    v = verify_export(variant("cut.jsonl", lines[:-2]), vault.public_key())
    assert not v.ok and "truncated" in v.reason
    assert not verify_export(variant("drop.jsonl", lines[:2] + lines[3:]), vault.public_key()).ok
    swapped_key = json.loads(lines[0])
    swapped_key["public_key"] = crypto.b64e(crypto.public_key_from_seed(crypto.random_key()))
    assert not verify_export(variant("k.jsonl", [json.dumps(swapped_key)] + lines[1:]), vault.public_key()).ok
    assert not verify_export(variant("junk.jsonl", ["{not json"]), vault.public_key()).ok
    assert not verify_export(variant("empty.jsonl", []), vault.public_key()).ok
    assert not verify_export(tmp_path / "missing.jsonl", vault.public_key()).ok


def test_receipts_hold_no_content(log: LocalReceiptLog, vault: LocalVault, tmp_path: Path) -> None:
    rec = vault.add("lab", "SECRETTITLE 腫瘤標記", "CEA 12.3 ng/mL SECRETTEXT")
    log.append("bot", "vault.read", record_refs=(rec.content_sha256,), detail="1 record")
    out = tmp_path / "r.jsonl"
    log.export_jsonl(out)
    data = out.read_bytes() + vault.path.read_bytes()
    for marker in (b"SECRETTITLE", b"SECRETTEXT", "腫瘤標記".encode(), rec.content_sha256.encode()):
        assert marker not in data
    assert hashlib.sha256(b"CEA 12.3 ng/mL SECRETTEXT").hexdigest().encode() not in data
