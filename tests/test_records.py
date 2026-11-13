"""muse.records: the format, the chain, Merkle proofs, checkpoints and exports.

The cases Rust must share are in core/conformance/records.json (replayed here
too). These tests are the properties: any change to a chain is noticed, a
proof verifies only for its own record, a redacted record still verifies.
"""

from __future__ import annotations

import hashlib
import json
import random
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

import muse.records as records  # noqa: E402
from muse.records import format as fmt  # noqa: E402
from muse.records import jcs, merkle, seal, vectors  # noqa: E402
from muse.records.errors import KINDS, RecordError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED = bytes(range(32))
REF = bytes(range(64, 96))
T0 = vectors.T0


def chain_of(n: int, seed: bytes = SEED) -> list[dict]:
    return vectors.make_chain(vectors.many_entries(n), seed.hex())


def kind_of(call, *args, **kwargs) -> tuple[str, int | None]:
    with pytest.raises(RecordError) as caught:
        call(*args, **kwargs)
    return caught.value.kind, caught.value.index


# ---------------------------------------------------------------- the committed vectors


def test_committed_vectors_replay():
    committed = json.loads((ROOT / "core" / "conformance" / "records.json").read_text(encoding="utf-8"))
    assert len(committed) > 2000
    assert {v["fn"] for v in committed} == set(vectors.FUNCTIONS)
    wrong = [v["name"] for v in committed if vectors.outcome(v["fn"], v["input"]) != v["expected"]]
    assert wrong == []


def test_vectors_cover_every_kind_a_verifier_can_report():
    seen = {v["expected"]["err"]["kind"] for v in vectors.build() if "err" in v["expected"]}
    assert seen == set(KINDS)


def test_vector_keys_are_labelled_test_only():
    raw = (ROOT / "core" / "conformance" / "records.json").read_text(encoding="utf-8")
    assert '"seed"' not in raw and "test_only_seed" in raw and "test_only_ref_key" in raw


# ---------------------------------------------------------------- canonical bytes


def test_canonical_json_matches_rfc_8785_on_its_own_example():
    # RFC 8785 section 3.2.3: the expected order of these keys.
    value = {"€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With Dagesh", "1": "One",
             "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control", "ö": "Latin Small Letter O With Diaeresis"}
    text = jcs.canonicalize(value).decode("utf-8")
    order = [json.loads(text)[k] for k in json.loads(text, object_pairs_hook=lambda pairs: [k for k, _ in pairs])]
    assert order == ["Carriage Return", "One", "Control", "Latin Small Letter O With Diaeresis", "Euro Sign",
                     "Emoji: Grinning Face", "Hebrew Letter Dalet With Dagesh"]


def test_canonical_json_round_trips_and_is_stable():
    rng = random.Random(1)

    def text() -> str:
        return "".join(chr(rng.choice([rng.randrange(0x20), rng.randrange(0x20, 0x7F), rng.randrange(0xA0, 0xD800),
                                       rng.randrange(0xE000, 0x10000), rng.randrange(0x10000, 0x110000)])) for _ in range(rng.randrange(6)))

    def value(depth: int):
        pick = rng.randrange(7 if depth < 3 else 5)
        if pick == 0:
            return None
        if pick == 1:
            return rng.random() < 0.5
        if pick == 2:
            return rng.randrange(-jcs.MAX_INT, jcs.MAX_INT + 1)
        if pick in (3, 4):
            return text()
        if pick == 5:
            return [value(depth + 1) for _ in range(rng.randrange(4))]
        return {text(): value(depth + 1) for _ in range(rng.randrange(4))}

    for _ in range(300):
        v = value(0)
        raw = jcs.canonicalize(v)
        assert json.loads(raw) == v
        assert jcs.canonicalize(jcs.loads_strict(raw)) == raw
        other_order = json.loads(json.dumps(v, sort_keys=True, ensure_ascii=True))  # same data, another serialisation
        assert jcs.canonicalize(other_order) == raw
        if isinstance(v, dict) and v:
            keys = [k for k, _ in json.loads(raw, object_pairs_hook=lambda pairs: pairs)]
            assert keys == sorted(v, key=lambda k: k.encode("utf-16-be"))


def test_no_float_gets_into_canonical_bytes():
    for bad in (1.0, 0.5, float("nan"), float("inf"), [1, {"a": 2.0}], 2**53, -(2**53), b"bytes", {1: "x"}, {"a": {1, 2}}):
        assert kind_of(jcs.canonicalize, bad) == ("malformed", None)


def test_strict_reader_refuses_what_parsers_disagree_on():
    for bad in ('{"a":1,"a":2}', "[1.0]", "[1e2]", "[-0]", "[9007199254740992]", '["\\ud83d"]', "﻿[]", "[] x", "", "[NaN]", b"\xff"):
        assert kind_of(jcs.loads_strict, bad) == ("malformed", None)
    assert jcs.loads_strict(b' {"b": [1, "\\u00e9"], "a": null} ') == {"a": None, "b": [1, "é"]}


# ---------------------------------------------------------------- a record


def test_a_record_holds_references_not_content():
    record = records.append(None, {"time": T0, "actor": "helper", "action": "files.write", "outcome": "ok",
                                   "summary": "Wrote the weekly report", "resource": "file:/Notes/secret-plan.md"}, SEED, REF)
    text = json.dumps(record)
    assert "secret-plan" not in text
    assert record["resource"] == records.resource_ref(REF, "file:/Notes/secret-plan.md")
    assert record["resource"] != hashlib.sha256(b"file:/Notes/secret-plan.md").hexdigest()
    assert record["resource"] != records.resource_ref(bytes(32), "file:/Notes/secret-plan.md")  # another vault's key
    assert set(record) == set(fmt.REQUIRED_FIELDS) | {"summary", "summary_salt"}
    assert all(not isinstance(v, float) for v in record.values())


def test_summary_is_normalised_before_signing_and_bound_by_the_signature():
    nfd = unicodedata.normalize("NFD", "Café résumé 한국어 が")
    record = records.append(None, {"time": T0, "actor": "person", "action": "card.answer", "outcome": "ok", "summary": nfd}, SEED, REF)
    assert record["summary"] == unicodedata.normalize("NFC", nfd) != nfd
    records.verify_record(record)
    assert kind_of(records.verify_record, {**record, "summary": nfd}) == ("summary_mismatch", None)
    assert kind_of(records.verify_record, {**record, "summary": record["summary"] + " "}) == ("summary_mismatch", None)


@pytest.mark.parametrize("summary", [
    "used sk-test0000000000000000", "token ghp_0123456789abcdefghij", hashlib.sha256(b"k").hexdigest(), "-----BEGIN PRIVATE KEY-----",
    "a\nb", "a\x00b", "‮evil", "", "x" * 201, "AKIAIOSFODNN7EXAMPLE", "eyJhbGciOiJIUzI1NiJ9.e30.sig",
])
def test_summaries_that_are_refused(summary):
    entry = {"time": T0, "actor": "helper", "action": "llm.call", "outcome": "ok", "summary": summary}
    assert kind_of(records.append, None, entry, SEED, REF) == ("summary", None)


def test_redaction_keeps_a_record_verifiable_and_hides_the_summary():
    chain = chain_of(6)
    redacted = [records.redact(r) for r in chain]
    assert all("summary" not in r and "summary_salt" not in r for r in redacted)
    assert records.verify_chain(redacted) == 6
    assert [records.hash(r) for r in redacted] == [records.hash(r) for r in chain]
    # Without the salt, the commitment cannot be tested against a guessed summary.
    guess = chain[0]["summary"]
    assert hashlib.sha256(guess.encode()).hexdigest() != chain[0]["summary_hash"]
    assert records.summary_hash(chain[0]["summary_salt"], guess) == chain[0]["summary_hash"]
    assert records.summary_hash("00" * 16, guess) != chain[0]["summary_hash"]
    # The salt differs per record, so equal summaries do not show as equal hashes.
    twins = vectors.make_chain([{"time": T0, "actor": "system", "action": "home.pause", "outcome": "ok", "summary": "Paused"}] * 2)
    assert twins[0]["summary_hash"] != twins[1]["summary_hash"]


def test_signature_is_domain_separated():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    record = chain_of(1)[0]
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    bare = key.sign(hashlib.sha256(fmt.canonical_bytes(record)).digest()).hex()
    raw = key.sign(fmt.canonical_bytes(record)).hex()
    for sig in (bare, raw):
        assert sig != record["sig"]
        assert kind_of(records.verify_record, {**record, "sig": sig}) == ("bad_signature", None)
    # A checkpoint signature is not a record signature and the other way round.
    cp = records.checkpoint([records.hash(record)], T0, SEED)
    assert kind_of(records.verify_record, {**record, "sig": cp["sig"]}) == ("bad_signature", None)
    assert kind_of(records.verify_checkpoint, {**cp, "sig": record["sig"]}) == ("bad_signature", None)


def test_signer_and_reference_key_can_be_objects():
    """The vault takes over by handing in objects; nothing here needs the raw keys."""
    inner = fmt.SeedSigner(SEED)

    class VaultSigner:
        device = inner.device

        def __init__(self):
            self.signed = 0

        def sign(self, message: bytes) -> bytes:
            self.signed += 1
            return inner.sign(message)

    class VaultRef:
        def mac(self, message: bytes) -> bytes:
            return fmt.HmacKey(REF).mac(message)

    signer = VaultSigner()
    entry = vectors.ENTRIES[1]
    assert records.append(None, entry, signer, VaultRef()) == records.append(None, entry, SEED, REF)
    assert signer.signed == 1


# ---------------------------------------------------------------- the chain


def test_every_single_field_change_is_detected():
    chain = chain_of(5)
    assert records.verify_chain(chain) == 5
    changes = {
        "seq": lambda v: v + 1, "time": lambda v: v + 1, "mandate": lambda v: "m-x" if v is None else None, "actor": lambda v: "system" if v != "system" else "person",
        "action": lambda v: v + "_x", "resource": lambda v: "00" * 32 if v is None else None, "summary_hash": lambda v: vectors.flip_hex(v),
        "outcome": lambda v: "failed" if v != "failed" else "ok", "cost": lambda v: {"currency": "TWD", "minor": 1} if v is None else None,
        "prev": lambda v: vectors.flip_hex(v), "sig": lambda v: vectors.flip_hex(v), "device": lambda v: vectors.flip_hex(v),
        "summary": lambda v: v + "?", "summary_salt": lambda v: vectors.flip_hex(v),
    }
    for i in range(5):
        for field, change in changes.items():
            broken = [dict(r) for r in chain]
            broken[i][field] = change(broken[i][field])
            kind, index = kind_of(records.verify_chain, broken)
            assert index == i, (i, field, kind)
        broken = [dict(r) for r in chain]
        broken[i]["links"] = {"job": "other"}
        assert kind_of(records.verify_chain, broken) == ("bad_signature", i)


def test_removed_reordered_and_repeated_records_are_detected():
    chain = chain_of(8)
    for i in range(7):
        assert kind_of(records.verify_chain, chain[:i] + chain[i + 1:]) == ("missing", i)
        swapped = chain[:i] + [chain[i + 1], chain[i]] + chain[i + 2:]
        assert kind_of(records.verify_chain, swapped) == ("missing", i)
        assert kind_of(records.verify_chain, chain[:i + 1] + [chain[i]] + chain[i + 1:]) == ("duplicate", i + 1)
    # Cutting the tail leaves a valid, shorter chain: only a checkpoint (or the next device) can show it.
    assert records.verify_chain(chain[:-1]) == 7


def test_a_fork_is_reported_as_a_fork():
    chain = chain_of(4)
    second = vectors.resign(chain[2], outcome="failed" if chain[2]["outcome"] != "failed" else "ok")
    records.verify_record(second)  # validly signed by the same device
    assert kind_of(records.verify_chain, chain[:3] + [second]) == ("fork", 3)
    assert kind_of(records.verify_chain, chain[:2] + [second, chain[3]]) == ("wrong_prev", 3)


def test_single_bit_changes_in_a_stored_chain_are_all_detected():
    """Flip each bit of the canonical text of a small chain: every flip must be noticed."""
    chain = vectors.make_chain(vectors.ENTRIES[:3])
    raw = bytearray(jcs.canonicalize(chain))
    rng = random.Random(7)
    positions = list(range(len(raw) * 8))
    rng.shuffle(positions)
    undetected = []
    for bit in positions[:6000]:
        changed = bytearray(raw)
        changed[bit // 8] ^= 1 << (bit % 8)
        try:
            parsed = jcs.loads_strict(bytes(changed))
            records.verify_chain(parsed, vectors.DEVICE_A)
        except RecordError:
            continue
        undetected.append(bit)
    assert undetected == []


def test_every_bit_of_one_record_matters():
    record = vectors.make_chain(vectors.ENTRIES[1:2])[0]
    raw = bytearray(jcs.canonicalize(record))
    for bit in range(len(raw) * 8):
        changed = bytearray(raw)
        changed[bit // 8] ^= 1 << (bit % 8)
        with pytest.raises(RecordError):
            records.verify_record(jcs.loads_strict(bytes(changed)), vectors.DEVICE_A)


def test_time_may_step_back_within_the_tolerance_only():
    a = records.append(None, {"time": T0, "actor": "system", "action": "home.pause", "outcome": "ok", "summary": "a"}, SEED, REF)
    entry = {"time": T0 - records.TIME_TOLERANCE_MS, "actor": "system", "action": "home.resume", "outcome": "ok", "summary": "b"}
    b = records.append(a, entry, SEED, REF)
    assert records.verify_chain([a, b]) == 2
    assert kind_of(records.append, a, {**entry, "time": entry["time"] - 1}, SEED, REF) == ("time_went_back", None)
    late = vectors.resign(b, time=entry["time"] - 1)
    assert kind_of(records.verify_chain, [a, late]) == ("time_went_back", 1)


def test_memory_store_refuses_anything_but_the_next_record():
    store = records.MemoryStore()
    chain = chain_of(3)
    assert store.head() is None and store.len() == 0 and store.get(0) is None
    store.append(chain[0])
    assert kind_of(store.append, chain[0]) == ("fork", None)
    assert kind_of(store.append, chain[2]) == ("missing", None)
    assert kind_of(store.append, vectors.resign(chain[1], prev="00" * 32)) == ("wrong_prev", None)
    other = vectors.make_chain(vectors.many_entries(2), vectors.SEED_B)
    assert kind_of(store.append, other[1]) == ("wrong_device", None)
    store.append(chain[1])
    assert store.len() == 2 and store.head() == chain[1] and store.get(1) == chain[1] and store.get(5) is None
    assert records.verify_chain(store.records) == 2


# ---------------------------------------------------------------- Merkle tree


def leaves(n: int) -> list[str]:
    return [hashlib.sha256(f"prop {i}".encode()).hexdigest() for i in range(n)]


def test_merkle_root_matches_a_plain_reading_of_rfc_6962():
    def mth(d: list[bytes]) -> bytes:
        if not d:
            return hashlib.sha256(b"").digest()
        if len(d) == 1:
            return hashlib.sha256(b"\x00" + d[0]).digest()
        k = 1
        while k * 2 < len(d):
            k *= 2
        return hashlib.sha256(b"\x01" + mth(d[:k]) + mth(d[k:])).digest()

    for n in range(0, 80):
        assert records.merkle_root(leaves(n)) == mth([bytes.fromhex(h) for h in leaves(n)]).hex()
    assert records.merkle_root([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_inclusion_proofs_verify_only_for_the_right_leaf():
    for n in range(1, 72):
        all_leaves = leaves(n)
        root = records.merkle_root(all_leaves)
        for i in range(n):
            proof = records.inclusion_proof(all_leaves, i)
            assert records.verify_inclusion(all_leaves[i], i, n, proof, root)
            assert len(proof) <= (n - 1).bit_length()
            for j in range(n):
                if j != i:
                    assert not records.verify_inclusion(all_leaves[j], i, n, proof, root)
                    assert not records.verify_inclusion(all_leaves[i], j, n, proof, root)
            assert not records.verify_inclusion(all_leaves[i], i, n, proof, records.merkle_root(leaves(n + 1)))
            if proof:
                assert not records.verify_inclusion(all_leaves[i], i, n, proof[:-1], root)
                assert not records.verify_inclusion(all_leaves[i], i, n, [vectors.flip_hex(proof[0])] + proof[1:], root)


def test_random_inclusion_proofs_in_larger_trees():
    rng = random.Random(3)
    for _ in range(40):
        n = rng.randrange(72, 700)
        all_leaves = leaves(n)
        root = records.merkle_root(all_leaves)
        i = rng.randrange(n)
        proof = records.inclusion_proof(all_leaves, i)
        assert records.verify_inclusion(all_leaves[i], i, n, proof, root)
        j = rng.randrange(n)
        assert (j == i) == records.verify_inclusion(all_leaves[j], i, n, proof, root)
        bad = list(proof)
        k = rng.randrange(len(bad))
        bad[k] = vectors.flip_hex(bad[k], rng.randrange(64))
        assert not records.verify_inclusion(all_leaves[i], i, n, bad, root)


def test_a_leaf_cannot_pass_for_an_inner_node():
    all_leaves = leaves(4)
    left = merkle._node(merkle._leaf(bytes.fromhex(all_leaves[0])), merkle._leaf(bytes.fromhex(all_leaves[1])))
    right = merkle._node(merkle._leaf(bytes.fromhex(all_leaves[2])), merkle._leaf(bytes.fromhex(all_leaves[3])))
    root = records.merkle_root(all_leaves)
    # The two-leaf tree whose "records" are the inner nodes has another root: leaves and nodes hash differently.
    assert records.merkle_root([left.hex(), right.hex()]) != root
    assert not records.verify_inclusion(left.hex(), 0, 2, [right.hex()], root)


def test_consistency_proofs():
    for n in range(0, 72):
        all_leaves = leaves(n)
        root = records.merkle_root(all_leaves)
        for m in range(0, n + 1):
            proof = records.consistency_proof(all_leaves, m)
            old = records.merkle_root(all_leaves[:m])
            assert records.verify_consistency(m, n, old, root, proof), (m, n)
            if 0 < m < n:
                changed = list(all_leaves)
                changed[m - 1] = vectors.flip_hex(changed[m - 1])
                assert not records.verify_consistency(m, n, old, records.merkle_root(changed), records.consistency_proof(changed, m))
                assert not records.verify_consistency(m, n, old, root, proof[:-1])
                assert not records.verify_consistency(m, n, vectors.flip_hex(old), root, proof)


# ---------------------------------------------------------------- checkpoints and exports


def test_checkpoint_seals_a_size_and_a_root():
    chain = chain_of(9)
    hashes = [records.hash(r) for r in chain]
    cp = records.checkpoint(hashes, T0 + 5, SEED)
    assert cp == {"device": vectors.DEVICE_A, "size": 9, "root": records.merkle_root(hashes), "time": T0 + 5, "sig": cp["sig"]}
    records.verify_checkpoint(cp, vectors.DEVICE_A)
    for field, value in (("size", 8), ("root", vectors.flip_hex(cp["root"])), ("time", T0), ("sig", vectors.flip_hex(cp["sig"]))):
        assert kind_of(records.verify_checkpoint, {**cp, field: value}) == ("bad_signature", None)
    # Two checkpoints of one chain are consistent; a rewritten chain is not.
    later = chain + vectors.make_chain(vectors.many_entries(12))[9:]
    assert records.verify_chain(later) == 12
    later_hashes = [records.hash(r) for r in later]
    cp2 = records.checkpoint(later_hashes, T0 + 9, SEED)
    assert records.verify_consistency(cp["size"], cp2["size"], cp["root"], cp2["root"], records.consistency_proof(later_hashes, 9))


def test_export_is_verifiable_with_nothing_else():
    chain = chain_of(20)
    cp = records.checkpoint([records.hash(r) for r in chain], T0, SEED)
    text = records.export_text(records.export(chain, cp))
    report = records.verify_export_text(text)
    assert report == {"device": vectors.DEVICE_A, "size": 20, "count": 20, "complete": True, "redacted": 0}
    assert json.loads(text)["spec"] == "record-format/1" and json.loads(text)["device"] == vectors.DEVICE_A


def test_subset_export_proves_each_record_and_redaction_keeps_it_valid():
    rng = random.Random(11)
    chain = chain_of(37)
    cp = records.checkpoint([records.hash(r) for r in chain], T0, SEED)
    for _ in range(25):
        select = sorted(rng.sample(range(37), rng.randrange(0, 12)))
        redact = rng.random() < 0.5
        out = records.export(chain, cp, select, redact)
        report = records.verify_export_text(records.export_text(out))
        assert report["count"] == len(select) and report["complete"] is False and report["redacted"] == (len(select) if redact else 0)
        assert [r["seq"] for r in out["records"]] == select
        if redact:
            assert all("summary" not in r and "summary_salt" not in r for r in out["records"])
        if select:
            # A record that was not selected cannot be slipped in under another's proof.
            other = next(i for i in range(37) if i not in select)
            swapped = {**out, "records": [records.redact(chain[other]) if redact else chain[other]] + out["records"][1:]}
            kind, index = kind_of(records.verify_export, swapped)
            assert kind in ("bad_proof", "malformed") and index == (0 if kind == "bad_proof" else 1)


def test_single_bit_changes_in_an_export_are_detected():
    chain = vectors.make_chain(vectors.ENTRIES[:5])
    cp = records.checkpoint([records.hash(r) for r in chain], T0, SEED)
    for out in (records.export(chain, cp), records.export(chain, cp, [1, 3], True)):
        raw = bytearray(records.export_text(out).encode("utf-8"))
        rng = random.Random(5)
        for bit in rng.sample(range((len(raw) - 1) * 8), 2500):  # the final newline is not part of the data
            changed = bytearray(raw)
            changed[bit // 8] ^= 1 << (bit % 8)
            with pytest.raises(RecordError):
                records.verify_export_text(bytes(changed))


def test_an_export_by_another_key_is_valid_but_is_not_yours():
    mine = chain_of(3)
    theirs = vectors.make_chain(vectors.many_entries(3), vectors.SEED_B)
    cp = records.checkpoint([records.hash(r) for r in theirs], T0, bytes.fromhex(vectors.SEED_B))
    forged = records.export(theirs, cp)
    assert records.verify_export(forged)["device"] == vectors.DEVICE_B  # self-consistent...
    assert kind_of(records.verify_export, forged, vectors.DEVICE_A) == ("wrong_device", None)  # ...but not the expected device
    assert records.verify_chain(mine, vectors.DEVICE_A) == 3


def test_export_refuses_a_checkpoint_that_is_not_over_the_records():
    chain = chain_of(5)
    cp = records.checkpoint([records.hash(r) for r in chain[:4]] + ["00" * 32], T0, SEED)
    assert kind_of(seal.export, chain, cp) == ("root_mismatch", None)
    assert kind_of(seal.export, chain[:3], cp) == ("size_mismatch", None)


# ---------------------------------------------------------------- the format document


def test_the_worked_example_in_the_format_document_is_what_the_code_produces():
    doc = (ROOT / "docs" / "spec" / "interfaces" / "record-format.md").read_text(encoding="utf-8")
    chain = vectors.make_chain(vectors.ENTRIES)
    record = chain[1]
    hashes = [records.hash(r) for r in chain]
    cp = records.checkpoint(hashes, T0 + 300_000, SEED)
    must_appear = [
        vectors.SEED_A, vectors.REF_KEY, vectors.DEVICE_A, records.genesis(vectors.DEVICE_A), hashes[0], record["resource"],
        record["summary_salt"], record["summary_hash"], fmt.canonical_bytes(record).decode("utf-8"), fmt.signing_digest(record).hex(),
        record["sig"], hashes[1], record["summary"], cp["root"], cp["sig"], records.leaf_hash(hashes[1]),
        *records.inclusion_proof(hashes, 1), f"({len(fmt.canonical_bytes(record))} bytes",
        f"{len(vectors.build()):,} cases", f"tolerance of {records.TIME_TOLERANCE_MS:,} ms", "`record-format/1`",
    ]
    missing = [text for text in must_appear if text not in doc]
    assert missing == []
    assert json.dumps(vectors.ENTRIES[1]["summary"], ensure_ascii=False) in doc
    for prefix in fmt.KEY_PREFIXES:
        assert f"`{prefix}`" in doc, prefix
    for kind in KINDS:
        assert f"`{kind}`" in doc, kind
