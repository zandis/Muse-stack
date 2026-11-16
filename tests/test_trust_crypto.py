"""Crypto primitives: KDF, key wrap, AEAD with AAD binding, padding, signatures, passphrase policy."""

from __future__ import annotations

import unicodedata

import pytest

from wardmuse.trust import crypto
from wardmuse.trust.contracts import VaultError, WrongPassphrase

KDF = crypto.INSECURE_TEST_KDF


def test_default_kdf_is_interactive_strength() -> None:
    p = crypto.DEFAULT_KDF
    p.validate()
    assert p.n >= 2**17 and p.r >= 8
    assert p.memory_bytes() == 128 * 1024 * 1024


@pytest.mark.parametrize("params", [
    crypto.KdfParams(n=2**9), crypto.KdfParams(n=3000), crypto.KdfParams(n=2**23),
    crypto.KdfParams(r=0), crypto.KdfParams(p=17), crypto.KdfParams(n=2**21, r=8),
    crypto.KdfParams(n=True, r=8, p=1),  # type: ignore[arg-type]
])
def test_kdf_params_rejected(params: crypto.KdfParams) -> None:
    with pytest.raises(VaultError):
        params.validate()


def test_kdf_params_roundtrip_and_unknown_alg() -> None:
    assert crypto.KdfParams.from_dict(KDF.to_dict()) == KDF
    with pytest.raises(VaultError):
        crypto.KdfParams.from_dict({"alg": "pbkdf2", "n": 1024, "r": 8, "p": 1})
    with pytest.raises(VaultError):
        crypto.KdfParams.from_dict("scrypt")


def test_kek_deterministic_and_salted() -> None:
    salt = b"s" * 32
    a = crypto.derive_kek("correct horse battery", salt, KDF)
    assert a == crypto.derive_kek("correct horse battery", salt, KDF)
    assert a != crypto.derive_kek("correct horse battery", b"t" * 32, KDF)
    assert a != crypto.derive_kek("correct horse batterz", salt, KDF)
    assert len(a) == 32
    with pytest.raises(VaultError):
        crypto.derive_kek("x" * 12, b"short", KDF)


def test_passphrase_unicode_normalisation() -> None:
    composed = "café 健康密碼很長的一句話"
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    salt = b"s" * 32
    assert crypto.derive_kek(composed, salt, KDF) == crypto.derive_kek(decomposed, salt, KDF)


def test_wrap_unwrap_and_wrong_passphrase() -> None:
    master = crypto.random_key()
    kek = crypto.derive_kek("right passphrase!", b"s" * 32, KDF)
    nonce, wrapped = crypto.wrap_key(kek, master, b"header")
    assert crypto.unwrap_key(kek, nonce, wrapped, b"header") == master
    bad = crypto.derive_kek("wrong passphrase!", b"s" * 32, KDF)
    with pytest.raises(WrongPassphrase) as exc:
        crypto.unwrap_key(bad, nonce, wrapped, b"header")
    assert str(exc.value) == "wrong passphrase"
    with pytest.raises(WrongPassphrase):
        crypto.unwrap_key(kek, nonce, wrapped, b"header2")   # header tampered
    with pytest.raises(VaultError):
        crypto.unwrap_key(kek, nonce[:5], wrapped, b"header")
    with pytest.raises(VaultError):
        crypto.unwrap_key(kek, nonce, wrapped[:-1], b"header")


def test_encrypt_roundtrip_empty_and_large() -> None:
    key = crypto.random_key()
    aad = crypto.record_aad("v" * 32, "records", "r" * 32, "text", 1)
    for pt in (b"", b"x", "空腹血糖 98 mg/dL".encode(), b"\x00" * 3_000_000):
        blob = crypto.encrypt(key, pt, aad)
        assert len(blob) == len(pt) + 28
        assert crypto.decrypt(key, blob, aad) == pt


def test_encrypt_refuses_oversize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "MAX_PLAINTEXT_BYTES", 10)
    with pytest.raises(VaultError):
        crypto.encrypt(crypto.random_key(), b"x" * 11, b"")


def test_aad_binds_every_component() -> None:
    key = crypto.random_key()
    base = ("v" * 32, "records", "a" * 32, "text", 1)
    blob = crypto.encrypt(key, b"secret", crypto.record_aad(*base))
    variants = [
        ("w" * 32, "records", "a" * 32, "text", 1),
        ("v" * 32, "items", "a" * 32, "text", 1),
        ("v" * 32, "records", "b" * 32, "text", 1),
        ("v" * 32, "records", "a" * 32, "meta", 1),
        ("v" * 32, "records", "a" * 32, "text", 2),
    ]
    for v in variants:
        with pytest.raises(VaultError) as exc:
            crypto.decrypt(key, blob, crypto.record_aad(*v))
        assert "secret" not in str(exc.value) and exc.value.__cause__ is None


def test_aad_is_unambiguous() -> None:
    # Concatenation-style AAD would make these equal; JSON arrays do not.
    assert crypto.record_aad("ab", "c", "d", "e", 1) != crypto.record_aad("a", "bc", "d", "e", 1)


def test_tampered_or_truncated_ciphertext() -> None:
    key = crypto.random_key()
    blob = bytearray(crypto.encrypt(key, b"hello world", b"aad"))
    blob[15] ^= 1
    with pytest.raises(VaultError):
        crypto.decrypt(key, bytes(blob), b"aad")
    with pytest.raises(VaultError):
        crypto.decrypt(key, b"short", b"aad")
    with pytest.raises(VaultError):
        crypto.decrypt(crypto.random_key(), crypto.encrypt(key, b"x", b"aad"), b"aad")


def test_nonce_uniqueness_many_encryptions() -> None:
    key = crypto.random_key()
    blobs = [crypto.encrypt(key, b"same plaintext", b"aad") for _ in range(5000)]
    nonces = {b[:12] for b in blobs}
    assert len(nonces) == 5000
    assert len(set(blobs)) == 5000


def test_padding_buckets() -> None:
    assert crypto.bucket_size(0) == 256
    assert crypto.bucket_size(256) == 256
    assert crypto.bucket_size(257) == 512
    assert crypto.bucket_size(65536) == 65536
    assert crypto.bucket_size(65537) == 131072
    assert crypto.bucket_size(200000) == 4 * 65536
    for data in (b"", b"a", b"x" * 1000, b"y" * 70000):
        padded = crypto.pad(data)
        assert len(padded) == crypto.bucket_size(len(data) + 4)
        assert crypto.unpad(padded) == data
    assert len(crypto.pad(b"a" * 10)) == len(crypto.pad(b"b" * 200))
    with pytest.raises(VaultError):
        crypto.unpad(b"\x00\x00\x10\x00abc")
    with pytest.raises(VaultError):
        crypto.unpad(b"ab")


def test_keyset_labels_distinct_and_bound_to_vault() -> None:
    master = crypto.random_key()
    ks = crypto.KeySet(master, "a" * 32)
    keys = {bytes(ks.record), bytes(ks.index), bytes(ks.signing_seed), bytes(ks.archive)}
    assert len(keys) == 4 and bytes(master) not in keys
    again = crypto.KeySet(master, "a" * 32)
    assert bytes(again.record) == bytes(ks.record)
    other = crypto.KeySet(master, "b" * 32)
    assert bytes(other.record) != bytes(ks.record)
    pk = ks.public_key()
    ks.wipe()
    assert bytes(ks.record) == bytes(32) and bytes(ks.signing_seed) == bytes(32)
    assert len(pk) == 32


def test_signatures_and_domain_separation() -> None:
    seed = crypto.random_key()
    pk = crypto.public_key_from_seed(seed)
    sig = crypto.sign(seed, "receipt", b"msg")
    assert crypto.verify_signature(pk, "receipt", b"msg", sig)
    assert not crypto.verify_signature(pk, "mandate", b"msg", sig)
    assert not crypto.verify_signature(pk, "receipt", b"msg2", sig)
    assert not crypto.verify_signature(pk, "receipt", b"msg", sig[:-1])
    assert not crypto.verify_signature(b"x" * 31, "receipt", b"msg", sig)
    assert not crypto.verify_signature(crypto.public_key_from_seed(crypto.random_key()), "receipt", b"msg", sig)
    with pytest.raises(VaultError):
        crypto.sign(seed, "", b"msg")


def test_keyed_hash_and_ct_equal() -> None:
    k1, k2 = crypto.random_key(), crypto.random_key()
    assert crypto.keyed_hash(k1, "content", b"x") != crypto.keyed_hash(k2, "content", b"x")
    assert crypto.keyed_hash(k1, "content", b"x") != crypto.keyed_hash(k1, "other", b"x")
    assert crypto.keyed_hash(k1, "content", b"x") != crypto.sha256_hex(b"x")
    assert crypto.ct_equal("abc", b"abc") and not crypto.ct_equal("abc", "abd")


def test_canonical_json() -> None:
    assert crypto.canonical_json({"b": 1, "a": [1, "健"]}) == '{"a":[1,"健"],"b":1}'.encode()
    with pytest.raises(ValueError):
        crypto.canonical_json({"x": float("nan")})


def test_b64d_strict() -> None:
    assert crypto.b64d(crypto.b64e(b"\x00\xff")) == b"\x00\xff"
    for bad in ("!!!", "abc", "健康"):
        with pytest.raises(VaultError):
            crypto.b64d(bad)


@pytest.mark.parametrize("pw,ok,hint", [
    ("", False, "too_short"),
    ("short", False, "too_short"),
    ("123456789", False, "too_short"),
    ("aaaaaaaaaaaa", True, "weak"),
    ("1234567890", True, "weak"),
    ("abcdefghij", True, "weak"),
    ("abcdefghijklmn", True, "fair"),
    ("correct horse battery staple", True, "strong"),
    ("我家的貓叫做小白很愛睡", True, "strong"),
])
def test_passphrase_policy(pw: str, ok: bool, hint: str) -> None:
    result = crypto.check_passphrase(pw)
    assert (result.ok, result.hint) == (ok, hint)
    assert result.message
    if not ok:
        with pytest.raises(crypto.WeakPassphrase):
            crypto.require_passphrase(pw)
    else:
        crypto.require_passphrase(pw)


def test_wipe() -> None:
    buf = bytearray(b"secret")
    crypto.wipe(buf)
    assert buf == bytearray(6)
    crypto.wipe(None)
