"""LocalVault: encryption at rest, locking, validation, search, archive, deletion, concurrency."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from wardmuse.trust import crypto
from wardmuse.trust.contracts import VaultError, VaultLocked, WrongPassphrase
from wardmuse.trust.mandates import LocalMandateBook
from wardmuse.trust.receipts import LocalReceiptLog
from wardmuse.trust.vault import MAX_TEXT_BYTES, LocalVault

PW = "correct horse battery staple"
KDF = crypto.INSECURE_TEST_KDF

TEXT_MARKER = "ZQXJ-unique-lab-marker-血糖異常值7731"
TITLE_MARKER = "PLMK-title-甲狀腺追蹤"
SOURCE_MARKER = "WVBN-source-醫院名稱"
MED_MARKER = "Metforminzzqq-二甲雙胍"


@pytest.fixture
def vault(tmp_path: Path) -> LocalVault:
    return LocalVault.create(tmp_path / "v" / "vault.sqlite", PW, kdf=KDF)


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def file_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    journal = Path(str(path) + "-journal")
    if journal.exists():
        data += journal.read_bytes()
    return data


def markers_absent(data: bytes, extra: tuple[str, ...] = ()) -> None:
    for marker in (TEXT_MARKER, TITLE_MARKER, SOURCE_MARKER, MED_MARKER, PW, *extra):
        for enc in ("utf-8", "utf-16-le"):
            assert marker.encode(enc) not in data, marker
        assert marker.split("-")[0].encode() not in data, marker


# ---------------------------------------------------------------- create / open / perms


def test_create_permissions_and_refuses_existing(tmp_path: Path) -> None:
    path = tmp_path / "newdir" / "vault.sqlite"
    v = LocalVault.create(path, PW, kdf=KDF)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert v.is_unlocked() and len(v.public_key()) == 32
    with pytest.raises(VaultError):
        LocalVault.create(path, PW, kdf=KDF)


def test_create_does_not_chmod_existing_dir(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    LocalVault.create(tmp_path / "vault.sqlite", PW, kdf=KDF)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


def test_default_path_under_muse_home(_isolated_muse_home: Path) -> None:
    v = LocalVault.create(None, PW, kdf=KDF)
    assert v.path == _isolated_muse_home / "health" / "vault.sqlite"
    assert stat.S_IMODE(v.path.parent.stat().st_mode) == 0o700
    assert LocalVault.open().vault_id == v.vault_id


def test_create_rejects_weak_passphrase(tmp_path: Path) -> None:
    with pytest.raises(crypto.WeakPassphrase):
        LocalVault.create(tmp_path / "v.sqlite", "short", kdf=KDF)
    with pytest.raises(crypto.WeakPassphrase):
        LocalVault.create(tmp_path / "v.sqlite", "", kdf=KDF)
    assert not (tmp_path / "v.sqlite").exists()


def test_open_missing_and_not_a_vault(tmp_path: Path) -> None:
    with pytest.raises(VaultError):
        LocalVault.open(tmp_path / "nope.sqlite")
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(os.urandom(4096))
    with pytest.raises(VaultError):
        LocalVault.open(junk)
    empty_db = tmp_path / "empty.sqlite"
    sqlite3.connect(str(empty_db)).execute("CREATE TABLE header (id INTEGER)").connection.close()
    with pytest.raises(VaultError):
        LocalVault.open(empty_db)


def test_header_has_kdf_params(vault: LocalVault) -> None:
    row = raw(vault.path).execute("SELECT * FROM header").fetchone()
    assert json.loads(row["kdf"]) == {"alg": "scrypt", "n": KDF.n, "r": KDF.r, "p": KDF.p}
    assert row["format_version"] == 1 and row["vault_id"] == vault.vault_id
    assert bytes(row["public_key"]) == vault.public_key()
    assert len(row["salt"]) >= 16 and len(row["wrapped_key"]) == 48


# ---------------------------------------------------------------- unlock / lock


def test_wrong_passphrase_then_right(vault: LocalVault) -> None:
    v = LocalVault.open(vault.path)
    assert not v.is_unlocked()
    with pytest.raises(WrongPassphrase) as exc:
        v.unlock("not the passphrase at all")
    assert str(exc.value) == "wrong passphrase"
    assert not v.is_unlocked()
    v.unlock(PW)
    assert v.is_unlocked()


def test_locked_methods_raise(vault: LocalVault, tmp_path: Path) -> None:
    rec = vault.add("lab", "t", "x")
    item = vault.add_item("reminder", {"text": "x"})
    pk = vault.public_key()
    vault.lock()
    assert not vault.is_unlocked()
    assert vault.public_key() == pk
    calls = [
        lambda: vault.add("lab", "t", "x"), lambda: vault.get(rec.id), lambda: vault.list(),
        lambda: vault.search("x"), lambda: vault.delete(rec.id), lambda: vault.add_item("reminder", {}),
        lambda: vault.list_items(), lambda: vault.update_item(item.id, {}), lambda: vault.delete_item(item.id),
        lambda: vault.sign(b"m"), lambda: vault.export_archive(str(tmp_path / "a.arc")), lambda: vault.destroy(),
        lambda: vault.ref_token("0" * 64),
    ]
    for call in calls:
        with pytest.raises(VaultLocked):
            call()


def test_lock_wipes_key_buffers(vault: LocalVault) -> None:
    keys = vault._keys
    assert keys is not None
    vault.lock()
    assert bytes(keys.record) == bytes(32) and bytes(keys.signing_seed) == bytes(32)


def test_corrupted_header_is_vault_error_not_wrong_passphrase(vault: LocalVault) -> None:
    conn = raw(vault.path)
    wrapped = bytearray(conn.execute("SELECT wrapped_key FROM header").fetchone()[0])
    wrapped[0] ^= 0xFF
    conn.execute("UPDATE header SET wrapped_key=?", (bytes(wrapped),))
    with pytest.raises(VaultError) as exc:
        LocalVault.open(vault.path)
    assert not isinstance(exc.value, WrongPassphrase)
    assert "corrupted" in str(exc.value)


def test_forged_header_with_fixed_checksum_gives_only_wrong_passphrase(vault: LocalVault) -> None:
    from wardmuse.trust import vault as vault_mod
    conn = raw(vault.path)
    h = dict(conn.execute("SELECT * FROM header").fetchone())
    h["kdf"] = json.dumps({"alg": "scrypt", "n": 2**11, "r": 8, "p": 1})   # attacker lowers cost
    h["checksum"] = vault_mod._header_checksum(h)
    conn.execute("UPDATE header SET kdf=?, checksum=?", (h["kdf"], h["checksum"]))
    v = LocalVault.open(vault.path)
    with pytest.raises(WrongPassphrase):
        v.unlock(PW)


def test_newer_format_refused(vault: LocalVault) -> None:
    raw(vault.path).execute("UPDATE header SET format_version=99")
    with pytest.raises(VaultError) as exc:
        LocalVault.open(vault.path)
    assert "newer" in str(exc.value)


def test_unlock_detects_replaced_file(vault: LocalVault, tmp_path: Path) -> None:
    other = LocalVault.create(tmp_path / "other.sqlite", PW, kdf=KDF)
    v = LocalVault.open(vault.path)
    os.replace(other.path, vault.path)
    with pytest.raises(VaultError):
        v.unlock(PW)


def test_idle_and_auto_lock(vault: LocalVault) -> None:
    vault.touch()
    assert 0 <= vault.idle_seconds() < 5
    assert vault.lock_if_idle(3600) is False and vault.is_unlocked()
    assert vault.lock_if_idle(0) is True and not vault.is_unlocked()
    assert vault.lock_if_idle(0) is False


# ---------------------------------------------------------------- records


def test_add_get_list_traditional_chinese(vault: LocalVault) -> None:
    text = "空腹血糖 98 mg/dL\n糖化血色素 HbA1c 5.6%\n總膽固醇 180"
    rec = vault.add("lab", "抽血檢查", text, "2024-05-02", "國立臺灣大學醫學院附設醫院")
    got, body = vault.get(rec.id)
    assert body == text and got == rec
    assert rec.size == len(text)
    assert rec.content_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert rec.added_at.endswith("Z") and len(rec.id) == 32
    assert vault.list() == [rec]


def test_list_filters_and_order(vault: LocalVault) -> None:
    a = vault.add("lab", "a", "x", "2023-01-01")
    b = vault.add("visit", "b", "y", "2024-06-01")
    c = vault.add("lab", "c", "z", "2024-01-15")
    assert [r.id for r in vault.list()] == [b.id, c.id, a.id]
    assert [r.id for r in vault.list(kind="lab")] == [c.id, a.id]
    assert [r.id for r in vault.list(since="2024-01-01")] == [b.id, c.id]
    assert [r.id for r in vault.list(until="2024-01-15")] == [c.id, a.id]
    assert [r.id for r in vault.list(limit=1)] == [b.id]
    with pytest.raises(VaultError):
        vault.list(kind="evil")
    with pytest.raises(VaultError):
        vault.list(since="2024/01/01")


def test_record_validation(vault: LocalVault) -> None:
    bad = [
        dict(kind="malware", title="t", text="x"),
        dict(kind="lab", title="", text="x"),
        dict(kind="lab", title="   ", text="x"),
        dict(kind="lab", title="a" * 201, text="x"),
        dict(kind="lab", title="line\nbreak", text="x"),
        dict(kind="lab", title="bell\x07", text="x"),
        dict(kind="lab", title="rlo\u202etxt.exe", text="x"),
        dict(kind="lab", title="t", text="x", source="s\x00"),
        dict(kind="lab", title="t", text="x", source="s" * 301),
        dict(kind="lab", title="t", text="x", occurred_on="2024-02-30"),
        dict(kind="lab", title="t", text="x", occurred_on="113/05/02"),
        dict(kind="lab", title="t", text=b"bytes"),
        dict(kind="lab", title="t", text="x" * (MAX_TEXT_BYTES + 1)),
        dict(kind="lab", title="t", text="健" * (MAX_TEXT_BYTES // 3 + 1)),
    ]
    for kwargs in bad:
        with pytest.raises(VaultError):
            vault.add(**kwargs)  # type: ignore[arg-type]
    assert vault.list() == []


def test_empty_and_max_size_text(vault: LocalVault) -> None:
    empty = vault.add("note", "empty", "")
    assert vault.get(empty.id)[1] == ""
    big_text = "a" * MAX_TEXT_BYTES
    big = vault.add("document", "big", big_text)
    assert vault.get(big.id)[1] == big_text


def test_unknown_or_malformed_ids(vault: LocalVault) -> None:
    for bad in ("", "x", "' OR 1=1 --", "A" * 32):
        with pytest.raises(VaultError):
            vault.get(bad)
    with pytest.raises(VaultError):
        vault.get("0" * 32)
    with pytest.raises(VaultError):
        vault.delete("0" * 32)


def test_find_by_text(vault: LocalVault) -> None:
    rec = vault.add("lab", "t", "unique text 123")
    assert vault.find_by_text("unique text 123") == rec.id
    assert vault.find_by_text("other") is None


# ---------------------------------------------------------------- at rest


def _populate(vault: LocalVault) -> None:
    vault.add("lab", TITLE_MARKER, f"結果: {TEXT_MARKER}", "2024-05-02", SOURCE_MARKER)
    vault.add_item("medication", {"name": MED_MARKER, "dose": "500 mg", "schedule": "早晚"})


def test_raw_file_has_no_plaintext(vault: LocalVault) -> None:
    _populate(vault)
    rec = vault.list()[0]
    data = file_bytes(vault.path)
    markers_absent(data, extra=("2024-05-02", "medication"))
    # The plain SHA-256 that receipts use must not be on disk either.
    assert rec.content_sha256.encode() not in data
    assert bytes.fromhex(rec.content_sha256) not in data
    row = raw(vault.path).execute("SELECT * FROM records").fetchone()
    assert row["content_mac"] != rec.content_sha256
    assert row["added_day"] == rec.added_at[:10]
    assert row["size_bucket"] == len(row["body"])


def test_journal_has_no_plaintext(vault: LocalVault) -> None:
    _populate(vault)
    # PERSIST leaves the journal (old page images) on disk after commit, which
    # is the worst case for residue: it must still hold ciphertext only.
    conn = raw(vault.path)
    conn.execute("PRAGMA journal_mode=PERSIST")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE records SET added_day='2000-01-01'")
    conn.execute("UPDATE items SET size_bucket=size_bucket+1")
    conn.execute("DELETE FROM records")
    conn.execute("COMMIT")
    journal = Path(str(vault.path) + "-journal")
    try:
        assert journal.exists() and journal.stat().st_size > 4096
        markers_absent(journal.read_bytes())
    finally:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.close()


def test_size_bucket_hides_exact_length(vault: LocalVault) -> None:
    a = vault.add("lab", "a", "x" * 10)
    b = vault.add("lab", "b", "y" * 200)
    rows = {r["id"]: r for r in raw(vault.path).execute("SELECT id, size_bucket, body FROM records")}
    assert rows[a.id]["size_bucket"] == rows[b.id]["size_bucket"]
    assert len(rows[a.id]["body"]) == len(rows[b.id]["body"])


def test_nonce_uniqueness_across_writes(vault: LocalVault) -> None:
    for i in range(150):
        vault.add("lab", f"t{i}", "same text")
    for i in range(50):
        vault.add_item("reminder", {"text": "same"})
    conn = raw(vault.path)
    blobs = [r[0] for r in conn.execute("SELECT meta FROM records UNION ALL SELECT body FROM records UNION ALL SELECT body FROM items")]
    nonces = [bytes(b[:12]) for b in blobs]
    assert len(nonces) == 350 and len(set(nonces)) == 350
    count = int(conn.execute("SELECT value FROM meta WHERE key='enc_count'").fetchone()[0])
    assert count == 350


def test_encryption_budget_enforced(vault: LocalVault, monkeypatch: pytest.MonkeyPatch) -> None:
    raw(vault.path).execute("UPDATE meta SET value=? WHERE key='enc_count'", (str(crypto.MAX_ENCRYPTIONS_PER_KEY - 1),))
    with pytest.raises(VaultError):
        vault.add("lab", "t", "x")   # needs 2 encryptions
    assert vault.list() == []


def test_swapped_ciphertexts_between_records_fail(vault: LocalVault) -> None:
    a = vault.add("lab", "A", "text a")
    b = vault.add("lab", "B", "text b")
    conn = raw(vault.path)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM records")}
    conn.execute("UPDATE records SET body=? WHERE id=?", (rows[b.id]["body"], a.id))
    with pytest.raises(VaultError):
        vault.get(a.id)
    conn.execute("UPDATE records SET meta=?, body=? WHERE id=?", (rows[b.id]["meta"], rows[b.id]["body"], a.id))
    with pytest.raises(VaultError):
        vault.get(a.id)
    with pytest.raises(VaultError):
        vault.list()   # list decrypts metas; a moved meta fails loudly


def test_swapped_fields_within_record_fail(vault: LocalVault) -> None:
    a = vault.add("lab", "A", "text a")
    conn = raw(vault.path)
    row = conn.execute("SELECT * FROM records").fetchone()
    conn.execute("UPDATE records SET meta=?, body=? WHERE id=?", (row["body"], row["meta"], a.id))
    with pytest.raises(VaultError):
        vault.get(a.id)


def test_ciphertext_from_other_vault_fails(vault: LocalVault, tmp_path: Path) -> None:
    other = LocalVault.create(tmp_path / "other.sqlite", PW, kdf=KDF)
    a = vault.add("lab", "A", "text a")
    o = other.add("lab", "O", "text o")
    orow = raw(other.path).execute("SELECT * FROM records").fetchone()
    conn = raw(vault.path)
    conn.execute("UPDATE records SET meta=?, body=? WHERE id=?", (orow["meta"], orow["body"], a.id))
    with pytest.raises(VaultError):
        vault.get(a.id)
    # Same id in both vaults: still bound to the vault id.
    conn.execute("INSERT INTO records SELECT ?, added_day, size_bucket, content_mac, meta, body FROM records WHERE id=?", (o.id, a.id))
    conn.execute("UPDATE records SET meta=?, body=? WHERE id=?", (orow["meta"], orow["body"], o.id))
    with pytest.raises(VaultError):
        vault.get(o.id)


def test_bitflip_reports_vault_error_without_bytes(vault: LocalVault) -> None:
    a = vault.add("lab", "A", "secret text")
    conn = raw(vault.path)
    body = bytearray(conn.execute("SELECT body FROM records").fetchone()[0])
    body[20] ^= 1
    conn.execute("UPDATE records SET body=?", (bytes(body),))
    with pytest.raises(VaultError) as exc:
        vault.get(a.id)
    msg = str(exc.value)
    assert "secret" not in msg and "\\x" not in msg and len(msg) < 120


# ---------------------------------------------------------------- search


def test_search_chinese_substring_bigram_and_case(vault: LocalVault) -> None:
    lab = vault.add("lab", "抽血報告", "空腹血糖 98 mg/dL，糖化血色素 HbA1c 5.6%，肝功能 GOT 25", "2024-05-02")
    visit = vault.add("visit", "心臟科門診", "醫師說血壓控制良好，下次三個月後回診。", "2024-06-01")
    vault.add("note", "雜記", "今天天氣很好")
    hits = vault.search("血糖")
    assert [r.id for r, _ in hits] == [lab.id]
    assert "血糖" in hits[0][1] and len(hits[0][1]) < 150
    assert [r.id for r, _ in vault.search("hba1c")] == [lab.id]
    assert [r.id for r, _ in vault.search("ＨｂＡ１ｃ")] == [lab.id]      # full-width
    assert [r.id for r, _ in vault.search("空腹血糖值")] == [lab.id]      # bigram partial
    assert [r.id for r, _ in vault.search("血壓 回診")] == [visit.id]
    assert vault.search("心臟科")[0][0].id == visit.id                  # title hit
    assert vault.search("糖尿病胰島素") == []
    assert vault.search("") == [] and vault.search("   ") == []


def test_search_ranking_and_limit(vault: LocalVault) -> None:
    for i in range(12):
        vault.add("note", f"note {i}", "血壓 紀錄")
    top = vault.add("lab", "血壓追蹤", "血壓 130/80 血壓 125/78 血壓 128/82")
    hits = vault.search("血壓")
    assert len(hits) == 8 and hits[0][0].id == top.id
    assert len(vault.search("血壓", limit=3)) == 3


def test_search_snippet_in_long_text(vault: LocalVault) -> None:
    text = "前言。" * 500 + "這裡提到 Creatinine 1.1 mg/dL 屬正常" + "結語。" * 500
    vault.add("lab", "腎功能", text)
    (_, snippet), = vault.search("creatinine")
    assert "Creatinine" in snippet and snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) < 120


def test_search_never_writes_plaintext(vault: LocalVault) -> None:
    _populate(vault)
    vault.search(TEXT_MARKER)
    markers_absent(file_bytes(vault.path))
    assert sorted(p.name for p in vault.path.parent.iterdir()) == ["vault.sqlite"]


# ---------------------------------------------------------------- delete / destroy


def test_delete_removes_ciphertext_bytes(vault: LocalVault) -> None:
    keep = vault.add("lab", "keep", "keep me")
    gone = vault.add("lab", "gone", "delete me " * 100)
    body = raw(vault.path).execute("SELECT body FROM records WHERE id=?", (gone.id,)).fetchone()[0]
    assert body[40:80] in vault.path.read_bytes()
    vault.delete(gone.id)
    assert body[40:80] not in vault.path.read_bytes()
    assert [r.id for r in vault.list()] == [keep.id]
    with pytest.raises(VaultError):
        vault.get(gone.id)
    assert raw(vault.path).execute("PRAGMA secure_delete").fetchone()[0] in (0, 1)  # per-connection; ours sets it


def test_destroy(vault: LocalVault) -> None:
    vault.add("lab", "t", "x")
    path = vault.path
    vault.destroy()
    assert not path.exists()
    assert not Path(str(path) + "-journal").exists()
    with pytest.raises(VaultError):
        vault.list()
    with pytest.raises(VaultError):
        vault.unlock(PW)


# ---------------------------------------------------------------- items


def test_items_crud(vault: LocalVault) -> None:
    med = vault.add_item("medication", {"name": "Metformin 二甲雙胍", "dose": "500 mg", "schedule": "早晚飯後"})
    appt = vault.add_item("appointment", {"when": "2024-07-01T09:00", "where": "心臟科"})
    assert vault.list_items() == [med, appt]
    assert vault.list_items("medication") == [med]
    updated = vault.update_item(med.id, {"name": "Metformin", "dose": "850 mg"})
    assert updated.data == {"name": "Metformin", "dose": "850 mg"} and updated.kind == "medication"
    assert updated.created_at == med.created_at
    assert vault.list_items("medication")[0].data["dose"] == "850 mg"
    vault.delete_item(appt.id)
    assert [i.id for i in vault.list_items()] == [med.id]
    with pytest.raises(VaultError):
        vault.delete_item(appt.id)
    with pytest.raises(VaultError):
        vault.update_item(appt.id, {})


def test_item_validation(vault: LocalVault) -> None:
    deep: dict = {}
    cur = deep
    for _ in range(20):
        cur["x"] = {}
        cur = cur["x"]
    for kind, data in [
        ("weapon", {}), ("medication", "not a dict"), ("medication", {1: "x"}),
        ("medication", {"x": object()}), ("medication", {"x": float("inf")}),
        ("medication", deep), ("medication", {"x": "y" * 70000}),
    ]:
        with pytest.raises(VaultError):
            vault.add_item(kind, data)  # type: ignore[arg-type]
    with pytest.raises(VaultError):
        vault.list_items("weapon")


# ---------------------------------------------------------------- passphrase change


def test_change_passphrase(vault: LocalVault) -> None:
    rec = vault.add("lab", "t", "data")
    other = LocalVault.open(vault.path)
    other.unlock(PW)
    with pytest.raises(WrongPassphrase):
        vault.change_passphrase("wrong old passphrase", "a brand new passphrase")
    with pytest.raises(crypto.WeakPassphrase):
        vault.change_passphrase(PW, "short")
    vault.change_passphrase(PW, "a brand new passphrase", kdf=crypto.KdfParams(n=2**11, r=8, p=1))
    assert json.loads(raw(vault.path).execute("SELECT kdf FROM header").fetchone()[0])["n"] == 2**11
    fresh = LocalVault.open(vault.path)
    with pytest.raises(WrongPassphrase):
        fresh.unlock(PW)
    fresh.unlock("a brand new passphrase")
    assert fresh.get(rec.id)[1] == "data"
    assert other.get(rec.id)[1] == "data"      # master key unchanged
    assert fresh.public_key() == vault.public_key()


# ---------------------------------------------------------------- signing


def test_sign_is_domain_separated(vault: LocalVault) -> None:
    sig = vault.sign(b"hello")
    assert crypto.verify_signature(vault.public_key(), "app", b"hello", sig)
    assert not crypto.verify_signature(vault.public_key(), "receipt", b"hello", sig)
    assert not crypto.verify_signature(vault.public_key(), "mandate", b"hello", sig)


# ---------------------------------------------------------------- archive


def _full_vault(vault: LocalVault) -> None:
    _populate(vault)
    book = LocalMandateBook(vault)
    m = book.grant("bot", ("vault.read", "llm.local"), "PURPOSE-marker-回答問題")
    LocalReceiptLog(vault).append("bot", "vault.read", mandate_id=m.id, record_refs=(vault.list()[0].content_sha256,), detail="1 record")


def test_archive_roundtrip(vault: LocalVault, tmp_path: Path) -> None:
    _full_vault(vault)
    arc = tmp_path / "export.muse"
    vault.export_archive(str(arc))
    assert stat.S_IMODE(arc.stat().st_mode) == 0o600
    data = arc.read_bytes()
    assert data.startswith(b"MUSE-HEALTH-ARCHIVE/1\n")
    markers_absent(data, extra=("PURPOSE-marker",))
    dest = tmp_path / "company" / "vault.sqlite"
    imported = LocalVault.import_archive(arc, dest, passphrase=PW)
    assert not imported.is_unlocked()
    assert imported.vault_id == vault.vault_id and imported.public_key() == vault.public_key()
    imported.unlock(PW)
    assert imported.list() == vault.list()
    rec = vault.list()[0]
    assert imported.get(rec.id) == vault.get(rec.id)
    assert imported.list_items() == vault.list_items()
    assert LocalMandateBook(imported).all() == LocalMandateBook(vault).all()
    assert LocalReceiptLog(imported).verify().ok and LocalReceiptLog(imported).verify().count == 1
    # A new install can keep working with it.
    imported.add("lab", "after move", "x")
    LocalReceiptLog(imported).append("system", "vault.import", detail="imported")
    assert LocalReceiptLog(imported).verify().ok


def test_archive_import_without_passphrase_still_opens(vault: LocalVault, tmp_path: Path) -> None:
    vault.add("lab", "t", "x")
    arc = tmp_path / "a.muse"
    vault.export_archive(str(arc))
    v = LocalVault.import_archive(arc, tmp_path / "v2.sqlite")
    v.unlock(PW)
    assert len(v.list()) == 1


def test_archive_refuses_overwrite(vault: LocalVault, tmp_path: Path) -> None:
    arc = tmp_path / "a.muse"
    vault.export_archive(str(arc))
    with pytest.raises(VaultError):
        vault.export_archive(str(arc))
    with pytest.raises(VaultError):
        LocalVault.import_archive(arc, vault.path, passphrase=PW)
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"keep me")
    with pytest.raises(VaultError):
        LocalVault.import_archive(arc, existing, passphrase=PW)
    assert existing.read_bytes() == b"keep me"


def test_archive_truncated(vault: LocalVault, tmp_path: Path) -> None:
    vault.add("lab", "t", "x" * 1000)
    arc = tmp_path / "a.muse"
    vault.export_archive(str(arc))
    data = arc.read_bytes()
    for cut in (len(data) - 1, len(data) - 10, len(data) // 2, 30, 0):
        bad = tmp_path / f"cut{cut}.muse"
        bad.write_bytes(data[:cut])
        with pytest.raises(VaultError):
            LocalVault.import_archive(bad, tmp_path / f"out{cut}.sqlite")
        assert not (tmp_path / f"out{cut}.sqlite").exists()


def test_archive_tampered(vault: LocalVault, tmp_path: Path) -> None:
    vault.add("lab", "t", "x")
    vault.add("lab", "u", "y")
    arc = tmp_path / "a.muse"
    vault.export_archive(str(arc))
    data = arc.read_bytes()
    lines = data.split(b"\n")
    # 1. Flip a byte in the body: SHA-256 catches it.
    body = bytearray(lines[1])
    i = body.index(b'"b64":"') + 10
    body[i] = ord("A") if body[i] != ord("A") else ord("B")
    bad = tmp_path / "flip.muse"
    bad.write_bytes(b"\n".join([lines[0], bytes(body), lines[2], b""]))
    with pytest.raises(VaultError):
        LocalVault.import_archive(bad, tmp_path / "o1.sqlite")
    # 2. Remove a record and recompute the SHA-256: only the HMAC (passphrase) catches it.
    doc = json.loads(lines[1])
    doc["tables"]["records"] = doc["tables"]["records"][:1]
    new_body = crypto.canonical_json(doc)
    signed = lines[0] + b"\n" + new_body + b"\n"
    trailer = json.loads(lines[2])
    trailer["sha256"] = hashlib.sha256(signed).hexdigest()
    forged = tmp_path / "forged.muse"
    forged.write_bytes(signed + json.dumps(trailer).encode() + b"\n")
    with pytest.raises(VaultError) as exc:
        LocalVault.import_archive(forged, tmp_path / "o2.sqlite", passphrase=PW)
    assert "tampered" in str(exc.value)
    assert not (tmp_path / "o2.sqlite").exists()
    # 3. Wrong passphrase on import: refused, nothing written.
    with pytest.raises(WrongPassphrase):
        LocalVault.import_archive(arc, tmp_path / "o3.sqlite", passphrase="wrong passphrase!!")
    assert not (tmp_path / "o3.sqlite").exists()


def test_archive_malformed_structure(vault: LocalVault, tmp_path: Path) -> None:
    vault.export_archive(str(tmp_path / "a.muse"))
    lines = (tmp_path / "a.muse").read_bytes().split(b"\n")
    doc = json.loads(lines[1])

    def write(d: dict, name: str) -> Path:
        signed = lines[0] + b"\n" + crypto.canonical_json(d) + b"\n"
        p = tmp_path / name
        p.write_bytes(signed + json.dumps({"sha256": hashlib.sha256(signed).hexdigest()}).encode() + b"\n")
        return p

    evil = json.loads(json.dumps(doc))
    evil["tables"]["sqlite_master"] = []
    extra_col = json.loads(json.dumps(doc))
    extra_col["tables"]["meta"].append({"key": "enc_count", "value": "0", "evil": "x"})
    bad_meta = json.loads(json.dumps(doc))
    bad_meta["tables"]["meta"].append({"key": "evil", "value": "0"})
    bad_type = json.loads(json.dumps(doc))
    bad_type["tables"]["header"][0]["salt"] = "plain string"
    version = json.loads(json.dumps(doc))
    version["version"] = 2
    for i, d in enumerate((evil, extra_col, bad_meta, bad_type, version)):
        with pytest.raises(VaultError):
            LocalVault.import_archive(write(d, f"m{i}.muse"), tmp_path / f"o{i}.sqlite")
    not_archive = tmp_path / "plain.txt"
    not_archive.write_text("hello")
    with pytest.raises(VaultError):
        LocalVault.import_archive(not_archive, tmp_path / "x.sqlite")


def test_archive_size_cap(vault: LocalVault, tmp_path: Path) -> None:
    vault.export_archive(str(tmp_path / "a.muse"))
    with pytest.raises(VaultError):
        LocalVault.import_archive(tmp_path / "a.muse", tmp_path / "x.sqlite", max_bytes=100)


# ---------------------------------------------------------------- concurrency


def test_two_vault_objects_concurrent_writes(vault: LocalVault) -> None:
    other = LocalVault.open(vault.path)
    other.unlock(PW)
    errors: list[BaseException] = []

    def worker(v: LocalVault, tag: str) -> None:
        try:
            for i in range(15):
                v.add("lab", f"{tag}{i}", f"text {tag} {i}")
                v.add_item("reminder", {"t": f"{tag}{i}"})
                v.list(limit=5)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(v, t)) for v, t in ((vault, "a"), (other, "b"), (vault, "c"), (other, "d"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(vault.list(limit=1000)) == 60 and len(other.list_items()) == 60
    titles = {r.title for r in other.list(limit=1000)}
    assert {"a0", "b14", "c7", "d3"} <= titles


def test_lock_from_other_thread_is_safe(vault: LocalVault) -> None:
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            try:
                vault.add("lab", "t", "x")
            except VaultLocked:
                return
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)
                return

    t = threading.Thread(target=writer)
    t.start()
    vault.lock()
    stop.set()
    t.join()
    assert errors == []
    vault.unlock(PW)
    for rec in vault.list(limit=10000):
        vault.get(rec.id)   # every written record decrypts: no write used a wiped key
