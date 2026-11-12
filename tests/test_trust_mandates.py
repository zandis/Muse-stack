"""LocalMandateBook: grants, signed canonical form, check() rules, tampering, revocation."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from muse.trust import crypto
from muse.trust.contracts import Action, VaultError, VaultLocked
from muse.trust.mandates import (
    LocalMandateBook,
    canonical_mandate,
    verify,
)
from muse.trust.vault import LocalVault, iso_utc

PW = "correct horse battery staple"
T0 = 1_750_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def vault(tmp_path: Path, clock: Clock) -> LocalVault:
    return LocalVault.create(tmp_path / "vault.sqlite", PW, kdf=crypto.INSECURE_TEST_KDF, clock=clock)


@pytest.fixture
def book(vault: LocalVault) -> LocalMandateBook:
    return LocalMandateBook(vault)


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


STD = ("vault.read", "items.write", "draft.letter", "llm.local")


def test_grant_and_basic_check(book: LocalMandateBook, vault: LocalVault) -> None:
    m = book.grant("bot", STD, "回答我的問題")
    assert m.scopes == tuple(sorted(STD)) and m.revoked_at is None and m.created_at == iso_utc(T0)
    assert verify(m, vault.public_key())
    d = book.check("bot", Action("vault.read", ("lab",), "local"))
    assert d.allowed and d.mandate_id == m.id and d.reason
    assert book.all() == [m]


@pytest.mark.parametrize("egress,extra,allowed", [
    ("none", (), True), ("local", (), True), ("private", (), False), ("cloud", (), False),
    ("private", ("llm.private",), True), ("cloud", ("llm.private",), False), ("cloud", ("llm.cloud",), True),
])
def test_egress_rules(book: LocalMandateBook, egress: str, extra: tuple[str, ...], allowed: bool) -> None:
    book.grant("bot", STD + extra, "p", allow_cloud=True)
    d = book.check("bot", Action("vault.read", (), egress))
    assert d.allowed is allowed
    if not allowed:
        assert egress in d.reason


def test_egress_without_local_scope(book: LocalMandateBook) -> None:
    book.grant("bot", ("vault.read",), "p")
    assert book.check("bot", Action("vault.read", (), "none")).allowed
    assert not book.check("bot", Action("vault.read", (), "local")).allowed


def test_fail_closed_on_unknowns(book: LocalMandateBook) -> None:
    book.grant("bot", STD, "p")
    for action in (Action("vault.delete_all"), Action("vault.read", (), "satellite"), Action("vault.read", ("dna",), "none")):
        d = book.check("bot", action)
        assert not d.allowed and d.mandate_id is None and "refused" in d.reason


def test_scope_missing_and_other_grantee(book: LocalMandateBook) -> None:
    book.grant("bot", STD, "p")
    d = book.check("bot", Action("vault.write"))
    assert not d.allowed and "vault.write" in d.reason
    d = book.check("steward-1", Action("vault.read"))
    assert not d.allowed and "steward-1" in d.reason
    assert not LocalMandateBook(book.vault).check("bot", Action("exchange.grant")).allowed


def test_record_kind_coverage(book: LocalMandateBook) -> None:
    book.grant("bot", ("vault.read", "items.write"), "labs only", record_kinds=("lab", "report"))
    assert book.check("bot", Action("vault.read", ("lab",))).allowed
    assert book.check("bot", Action("vault.read", ("lab", "report"))).allowed
    d = book.check("bot", Action("vault.read", ("lab", "visit")))
    assert not d.allowed and "visit" in d.reason
    assert not book.check("bot", Action("vault.read", ())).allowed       # unspecified kinds: fail closed
    assert book.check("bot", Action("items.write", ())).allowed          # no record content involved


def test_empty_kinds_means_all(book: LocalMandateBook) -> None:
    book.grant("bot", ("vault.read",), "p")
    assert book.check("bot", Action("vault.read", ("pathology_report", "imaging_report"))).allowed


def test_grant_validation(book: LocalMandateBook, clock: Clock) -> None:
    bad = [
        dict(grantee="bot", scopes=("vault.read", "root"), purpose="p"),
        dict(grantee="bot", scopes=(), purpose="p"),
        dict(grantee="bot", scopes=("llm.cloud",), purpose="p"),
        dict(grantee="", scopes=("vault.read",), purpose="p"),
        dict(grantee="bot bot", scopes=("vault.read",), purpose="p"),
        dict(grantee="bot", scopes=("vault.read",), purpose=""),
        dict(grantee="bot", scopes=("vault.read",), purpose="x" * 501),
        dict(grantee="bot", scopes=("vault.read",), purpose="p", record_kinds=("genome",)),
        dict(grantee="bot", scopes=("vault.read",), purpose="p", expires_at=iso_utc(T0 - 1)),
        dict(grantee="bot", scopes=("vault.read",), purpose="p", expires_at="2030-01-01"),
    ]
    for kwargs in bad:
        with pytest.raises(VaultError):
            book.grant(**kwargs)  # type: ignore[arg-type]
    assert book.all() == []
    m = book.grant("bot", ("llm.cloud", "vault.read"), "p", allow_cloud=True)
    assert "llm.cloud" in m.scopes


def test_expiry_with_injected_clock(book: LocalMandateBook, clock: Clock) -> None:
    m = book.grant("bot", STD, "p", expires_at=iso_utc(T0 + 3600))
    assert book.check("bot", Action("vault.read")).allowed
    clock.t = T0 + 3599
    assert book.check("bot", Action("vault.read")).allowed
    clock.t = T0 + 3600
    assert not book.check("bot", Action("vault.read")).allowed
    assert book.all() == [m]


def test_future_dated_mandate_ignored(book: LocalMandateBook, clock: Clock) -> None:
    clock.t = T0 + 86400
    book.grant("bot", STD, "p")
    clock.t = T0
    assert not book.check("bot", Action("vault.read")).allowed


def test_revocation_immediate_signed_and_listed(book: LocalMandateBook, vault: LocalVault, clock: Clock) -> None:
    m = book.grant("bot", STD, "p")
    clock.t = T0 + 60
    book.revoke(m.id)
    assert not book.check("bot", Action("vault.read")).allowed
    (r,) = book.all()
    assert r.revoked_at == iso_utc(T0 + 60) and r.signature != m.signature
    assert verify(r, vault.public_key())
    clock.t = T0 + 120
    book.revoke(m.id)   # no-op
    assert book.all()[0].revoked_at == iso_utc(T0 + 60)
    with pytest.raises(VaultError):
        book.revoke("0" * 32)
    with pytest.raises(VaultError):
        book.revoke("not-an-id")


def test_newer_grant_after_revocation(book: LocalMandateBook) -> None:
    old = book.grant("bot", STD, "old")
    book.revoke(old.id)
    new = book.grant("bot", STD, "new")
    d = book.check("bot", Action("vault.read"))
    assert d.allowed and d.mandate_id == new.id


def test_canonical_form_and_third_party_verify(book: LocalMandateBook, vault: LocalVault) -> None:
    m = book.grant("bot", STD, "回答問題", record_kinds=("lab",))
    blob = canonical_mandate(m)
    obj = json.loads(blob)
    assert set(obj) == {"domain", "id", "grantee", "scopes", "purpose", "record_kinds", "created_at", "expires_at", "revoked_at"}
    assert blob == json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert "回答問題".encode() in blob
    pk = vault.public_key()
    assert verify(m, pk)
    for change in (dict(scopes=m.scopes + ("llm.cloud",)), dict(grantee="other"), dict(purpose="x"),
                   dict(record_kinds=()), dict(expires_at="2099-01-01T00:00:00Z"), dict(revoked_at=None, created_at="2020-01-01T00:00:00Z")):
        assert not verify(dataclasses.replace(m, **change), pk)
    assert not verify(m, crypto.public_key_from_seed(crypto.random_key()))
    assert not verify(dataclasses.replace(m, signature="garbage!"), pk)
    # A signature from the app-level sign() cannot become a mandate signature.
    forged = dataclasses.replace(m, signature=crypto.b64e(vault.sign(canonical_mandate(m))))
    assert not verify(forged, pk)


def test_tampered_row_ignored_and_reported(book: LocalMandateBook, vault: LocalVault) -> None:
    m = book.grant("bot", STD, "p")
    conn = raw(vault.path)
    conn.execute("UPDATE mandates SET scopes=? WHERE id=?", (json.dumps(sorted(STD + ("llm.cloud",))), m.id))
    d = book.check("bot", Action("vault.read", (), "cloud"))
    assert not d.allowed
    assert not book.check("bot", Action("vault.read", (), "local")).allowed   # the whole row is distrusted
    assert any(m.id in p and "signature" in p for p in book.problems())
    assert book.all() == []


def test_tampered_expiry_ignored(book: LocalMandateBook, vault: LocalVault, clock: Clock) -> None:
    m = book.grant("bot", STD, "p", expires_at=iso_utc(T0 + 10))
    raw(vault.path).execute("UPDATE mandates SET expires_at=NULL WHERE id=?", (m.id,))
    clock.t = T0 + 100
    assert not book.check("bot", Action("vault.read")).allowed


def test_undo_revocation_by_restoring_old_row_is_detected(book: LocalMandateBook, vault: LocalVault) -> None:
    m = book.grant("bot", STD, "p")
    conn = raw(vault.path)
    old = dict(conn.execute("SELECT * FROM mandates WHERE id=?", (m.id,)).fetchone())
    book.revoke(m.id)
    conn.execute("UPDATE mandates SET revoked_at=NULL, signature=? WHERE id=?", (old["signature"], m.id))
    assert verify(m, vault.public_key())   # the old row on its own is validly signed...
    d = book.check("bot", Action("vault.read"))
    assert not d.allowed and "integrity" in d.reason   # ...but the signed head catches it
    assert any("head" in p for p in book.problems())


def test_deleted_row_or_head_detected(book: LocalMandateBook, vault: LocalVault) -> None:
    a = book.grant("bot", STD, "a")
    book.grant("bot", STD, "b")
    conn = raw(vault.path)
    conn.execute("DELETE FROM mandates WHERE id=?", (a.id,))
    assert not book.check("bot", Action("vault.read")).allowed
    conn.execute("DELETE FROM meta WHERE key='mandate_head'")
    d = book.check("bot", Action("vault.read"))
    assert not d.allowed and "integrity" in d.reason


def test_swapped_purpose_ciphertext_ignored(book: LocalMandateBook, vault: LocalVault) -> None:
    a = book.grant("bot", STD, "purpose A")
    b = book.grant("bot", ("vault.read",), "purpose B")
    conn = raw(vault.path)
    pb = conn.execute("SELECT purpose FROM mandates WHERE id=?", (b.id,)).fetchone()[0]
    conn.execute("UPDATE mandates SET purpose=? WHERE id=?", (pb, a.id))
    assert [m.id for m in book.all()] == [b.id]
    assert any(a.id in p and "decrypt" in p for p in book.problems())


def test_purpose_encrypted_on_disk(book: LocalMandateBook, vault: LocalVault) -> None:
    book.grant("bot", STD, "PURPOSEMARKER 回答我的健康問題")
    data = vault.path.read_bytes()
    assert b"PURPOSEMARKER" not in data and "回答我的健康問題".encode() not in data


def test_locked_vault(book: LocalMandateBook, vault: LocalVault) -> None:
    book.grant("bot", STD, "p")
    vault.lock()
    d = book.check("bot", Action("vault.read"))
    assert not d.allowed and "locked" in d.reason
    with pytest.raises(VaultLocked):
        book.all()
    with pytest.raises(VaultLocked):
        book.grant("bot", STD, "p")


def test_two_books_same_file(book: LocalMandateBook, vault: LocalVault) -> None:
    other_vault = LocalVault.open(vault.path)
    other_vault.unlock(PW)
    other = LocalMandateBook(other_vault)
    m = book.grant("bot", STD, "p")
    assert other.check("bot", Action("vault.read")).allowed
    other.revoke(m.id)
    assert not book.check("bot", Action("vault.read")).allowed
