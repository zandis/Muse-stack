"""Checkpoints ("sealed") and exports. Pure; see docs/spec/interfaces/record-format.md.

A checkpoint is the device's signature over the Merkle root of its first
`size` records. An export is one self-describing JSON object: the device, a
checkpoint, and either the whole chain up to the checkpoint or a subset with
an inclusion proof per record.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, Sequence

from wardmuse.records import format as fmt
from wardmuse.records.errors import RecordError
from wardmuse.records.jcs import MAX_INT, canonicalize, loads_strict
from wardmuse.records.merkle import inclusion_proof, merkle_root, verify_inclusion

D_CHECKPOINT = b"muse/checkpoint/v1/sig\x00"
EXPORT_TYPE = "muse.records.export"
EXPORT_FORMAT = 1
SPEC = "record-format/1"
MAX_PROOF_LEN = 64

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX128 = re.compile(r"[0-9a-f]{128}\Z")
_CHECKPOINT_FIELDS = {"device", "size", "root", "time", "sig"}


def _hex(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.match(value) is not None


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_INT


# ---------------------------------------------------------------- checkpoint


def check_checkpoint(cp: Any) -> None:
    if not isinstance(cp, dict) or set(cp) != _CHECKPOINT_FIELDS:
        raise RecordError("malformed", detail="a checkpoint is {device, size, root, time, sig}")
    if not (_hex(_HEX64, cp["device"]) and _hex(_HEX64, cp["root"]) and _hex(_HEX128, cp["sig"]) and _int(cp["size"]) and _int(cp["time"])):
        raise RecordError("malformed", detail="checkpoint field")


def _checkpoint_digest(cp: Mapping[str, Any]) -> bytes:
    body = {"device": cp["device"], "size": cp["size"], "root": cp["root"], "time": cp["time"]}
    return hashlib.sha256(D_CHECKPOINT + canonicalize(body)).digest()


def checkpoint(hashes: Sequence[str], time: int, signer: fmt.Signer | bytes) -> dict[str, Any]:
    """The signed checkpoint over the records with these hashes (all of them, in order) at `time` (ms)."""
    signer = fmt._signer(signer)
    cp: dict[str, Any] = {"device": signer.device, "size": len(hashes), "root": merkle_root(hashes), "time": time}
    if not _int(time):
        raise RecordError("malformed", detail="time is integer milliseconds")
    cp["sig"] = signer.sign(_checkpoint_digest(cp)).hex()
    return cp


def verify_checkpoint(cp: Any, device: str | None = None) -> None:
    """Shape, device (when given) and signature. RecordError: malformed, wrong_device or bad_signature."""
    check_checkpoint(cp)
    if device is not None and cp["device"] != device:
        raise RecordError("wrong_device")
    if not fmt._verify_sig(cp["device"], _checkpoint_digest(cp), cp["sig"]):
        raise RecordError("bad_signature")


# ---------------------------------------------------------------- export


def export(records: Sequence[Mapping[str, Any]], cp: Mapping[str, Any], select: Sequence[int] | None = None,
           redact_summaries: bool = False) -> dict[str, Any]:
    """An export of the chain `records` (from seq 0) sealed by checkpoint `cp`.

    `select` None: every record up to the checkpoint. Otherwise the records with
    those sequence numbers (ascending, each below `cp.size`), each with its
    inclusion proof. `redact_summaries` leaves the summaries out."""
    check_checkpoint(cp)
    for record in records:
        fmt.check_record(record)
    size = cp["size"]
    if len(records) < size:
        raise RecordError("size_mismatch", detail="fewer records than the checkpoint covers")
    hashes = [fmt.record_hash(r) for r in records[:size]]
    if merkle_root(hashes) != cp["root"]:
        raise RecordError("root_mismatch", detail="the checkpoint is not over these records")
    out: dict[str, Any] = {"type": EXPORT_TYPE, "format": EXPORT_FORMAT, "spec": SPEC, "device": cp["device"],
                           "checkpoint": dict(cp), "complete": select is None}
    if select is None:
        chosen = list(range(size))
    else:
        chosen = list(select)
        if any(not _int(s) or s >= size for s in chosen) or any(b <= a for a, b in zip(chosen, chosen[1:])):
            raise RecordError("malformed", detail="select is ascending sequence numbers below the checkpoint size")
        out["proofs"] = [inclusion_proof(hashes, s) for s in chosen]
    out["records"] = [fmt.redact(records[s]) if redact_summaries else dict(records[s]) for s in chosen]
    return out


def _check_export_shape(obj: Any) -> None:
    if not isinstance(obj, dict):
        raise RecordError("malformed", detail="an export is an object")
    keys = set(obj)
    base = {"type", "format", "spec", "device", "checkpoint", "complete", "records"}
    if not base <= keys or not keys <= base | {"proofs"}:
        raise RecordError("malformed", detail="missing or unknown export fields")
    if obj["type"] != EXPORT_TYPE or type(obj["format"]) is not int or obj["format"] != EXPORT_FORMAT or obj["spec"] != SPEC:
        raise RecordError("malformed", detail="not a records export of format 1")
    if not _hex(_HEX64, obj["device"]) or not isinstance(obj["complete"], bool) or not isinstance(obj["records"], list):
        raise RecordError("malformed", detail="device, complete or records")
    check_checkpoint(obj["checkpoint"])
    for record in obj["records"]:
        fmt.check_record(record)
    if obj["complete"] == ("proofs" in obj):
        raise RecordError("malformed", detail="proofs come with a subset, and only with a subset")
    if "proofs" in obj:
        proofs = obj["proofs"]
        if not isinstance(proofs, list) or len(proofs) != len(obj["records"]):
            raise RecordError("malformed", detail="one proof per record")
        for proof in proofs:
            if not isinstance(proof, list) or len(proof) > MAX_PROOF_LEN or not all(_hex(_HEX64, h) for h in proof):
                raise RecordError("malformed", detail="a proof is a list of hashes")


def verify_export(obj: Any, device: str | None = None) -> dict[str, Any]:
    """Check an export with nothing but the export. Returns {device, size, count, complete, redacted}.

    RecordError otherwise. In order: the whole file's shape (malformed); the
    device, when the caller knows which one to expect (wrong_device); the
    checkpoint (bad_checkpoint); then the records. Complete: the chain
    (verify_chain's kinds, with the index), size_mismatch, root_mismatch. Subset,
    per record: wrong_device, bad_signature, summary_mismatch; malformed when the
    sequence numbers do not ascend; bad_proof; wrong_prev between neighbours.

    The device is whatever key the file names. That the key is the person's is
    for the reader to establish: compare `device` with the key shown on the
    person's device."""
    _check_export_shape(obj)
    if device is not None and obj["device"] != device:
        raise RecordError("wrong_device")
    device = obj["device"]
    cp = obj["checkpoint"]
    try:
        verify_checkpoint(cp, device)
    except RecordError as exc:
        raise RecordError("bad_checkpoint", detail=exc.kind) from None
    records = obj["records"]
    size, root = cp["size"], cp["root"]
    if obj["complete"]:
        verifier = fmt.ChainVerifier(device)
        for record in records:
            verifier.push(record)
        if verifier.size != size:
            raise RecordError("size_mismatch")
        if merkle_root(verifier.hashes) != root:
            raise RecordError("root_mismatch")
    else:
        previous: Mapping[str, Any] | None = None
        previous_hash = ""
        for index, (record, proof) in enumerate(zip(records, obj["proofs"])):
            try:
                fmt._verify_checked(record, device)
                if previous is not None and record["seq"] <= previous["seq"]:
                    raise RecordError("malformed", detail="sequence numbers must ascend")
                this = fmt.record_hash(record)
                if not verify_inclusion(this, record["seq"], size, proof, root):
                    raise RecordError("bad_proof")
                if previous is not None and record["seq"] == previous["seq"] + 1 and record["prev"] != previous_hash:
                    raise RecordError("wrong_prev")
            except RecordError as exc:
                raise RecordError(exc.kind, index, exc.detail) from None
            previous, previous_hash = record, this
    return {"device": device, "size": size, "count": len(records), "complete": obj["complete"],
            "redacted": sum(1 for r in records if "summary" not in r)}


def export_text(obj: Mapping[str, Any]) -> str:
    """The export as the text of a file: canonical JSON and a final newline."""
    return canonicalize(obj).decode("utf-8") + "\n"


def verify_export_text(text: str | bytes, device: str | None = None) -> dict[str, Any]:
    """`verify_export` on the text of a file, read strictly (see jcs.loads_strict)."""
    return verify_export(loads_strict(text), device)
