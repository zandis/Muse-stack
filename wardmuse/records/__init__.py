"""Records: a signed, append-only chain per device of everything done in the person's name.

Format: docs/spec/interfaces/record-format.md. The same functions, with the
same names and the same answers, are in the Rust core (core/src/records.rs);
core/conformance/records.json holds the cases both are checked against.

    append, sign, canonical_bytes, hash, verify_record, verify_chain, redact   (format.py)
    merkle_root, inclusion_proof, verify_inclusion,
    consistency_proof, verify_consistency                                     (merkle.py)
    checkpoint, verify_checkpoint, export, verify_export                      (seal.py)
    RecordStore, MemoryStore, SqliteRecordStore                               (store.py)
    HomeRecords, DeviceKeyFile                                                (home.py: the home's own chain)

Signing needs the `cryptography` package (Ed25519); everything else is the
standard library.
"""

from __future__ import annotations

from wardmuse.records.errors import KINDS, RecordError
from wardmuse.records.format import (
    ACTORS,
    FORMAT_VERSION,
    MAX_SUMMARY_CHARS,
    OUTCOMES,
    TIME_TOLERANCE_MS,
    ChainVerifier,
    HmacKey,
    RefKey,
    SeedSigner,
    Signer,
    append,
    canonical_bytes,
    check_record,
    check_summary,
    genesis,
    key_shaped,
    record_hash,
    redact,
    resource_ref,
    sign,
    signing_digest,
    summary_hash,
    summary_salt,
    verify_chain,
    verify_record,
)
from wardmuse.records.jcs import MAX_INT, canonicalize, loads_strict
from wardmuse.records.merkle import (
    consistency_proof,
    inclusion_proof,
    leaf_hash,
    merkle_root,
    verify_consistency,
    verify_inclusion,
)
from wardmuse.records.seal import (
    check_checkpoint,
    checkpoint,
    export,
    export_text,
    verify_checkpoint,
    verify_export,
    verify_export_text,
)
from wardmuse.records.store import MemoryStore, RecordStore, SqliteRecordStore

hash = record_hash  # noqa: A001  (the name the format and the Rust core use)

__all__ = [
    "ACTORS", "FORMAT_VERSION", "KINDS", "MAX_INT", "MAX_SUMMARY_CHARS", "OUTCOMES", "TIME_TOLERANCE_MS",
    "ChainVerifier", "HmacKey", "MemoryStore", "RecordError", "RecordStore", "RefKey", "SeedSigner", "Signer",
    "SqliteRecordStore", "append", "canonical_bytes", "canonicalize", "check_checkpoint", "check_record",
    "check_summary", "checkpoint", "consistency_proof", "export", "export_text", "genesis", "hash",
    "inclusion_proof", "key_shaped", "leaf_hash", "loads_strict", "merkle_root", "record_hash", "redact",
    "resource_ref", "sign", "signing_digest", "summary_hash", "summary_salt", "verify_chain", "verify_checkpoint",
    "verify_consistency", "verify_export", "verify_export_text", "verify_inclusion", "verify_record",
]
