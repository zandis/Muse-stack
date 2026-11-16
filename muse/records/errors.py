"""The one exception of muse.records."""

from __future__ import annotations

# Every way a record, chain, checkpoint or export can be wrong. The Rust twin
# (core/src/records.rs, `Kind`) has the same names.
KINDS = (
    "malformed",         # not the shape or value range the format allows
    "summary",           # append only: the summary breaks the summary rule (too long, control characters, key-shaped)
    "wrong_device",      # signed by, or naming, another device than the chain's
    "bad_signature",     # the signature does not verify
    "summary_mismatch",  # a summary is present but is not the one the record commits to
    "missing",           # a sequence number was skipped: a record was removed, or the order changed
    "duplicate",         # the same record appears twice
    "fork",              # two different validly signed records claim one sequence number
    "wrong_prev",        # a validly signed record does not link to the record before it
    "time_went_back",    # time is earlier than the previous record's by more than the tolerance
    "bad_checkpoint",    # the checkpoint is malformed or its signature does not verify
    "size_mismatch",     # a complete export does not hold exactly `checkpoint.size` records
    "root_mismatch",     # the records do not hash to the checkpoint's root
    "bad_proof",         # an inclusion proof does not lead to the checkpoint's root
)


class RecordError(Exception):
    """`kind` is one of KINDS; `index` is the position of the first bad item, when there is one."""

    def __init__(self, kind: str, index: int | None = None, detail: str = "") -> None:
        assert kind in KINDS, kind
        self.kind = kind
        self.index = index
        self.detail = detail
        where = "" if index is None else f" at {index}"
        super().__init__(f"{kind}{where}" + (f": {detail}" if detail else ""))
