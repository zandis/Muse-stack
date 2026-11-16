"""The Merkle tree over record hashes: RFC 6962 section 2.1, verified as in RFC 9162 section 2.1.

Leaf i is the hash of record i (`record_hash`, 32 bytes). As in RFC 6962:
    leaf node  = SHA-256(0x00 || record hash)
    inner node = SHA-256(0x01 || left || right)
    empty tree = SHA-256("")
and a tree of n > 1 leaves splits at the largest power of two below n.

Hashes are lower-case hex strings here (bytes in the Rust twin). Pure.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Sequence

from wardmuse.records.errors import RecordError

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _raw(value: object) -> bytes:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise RecordError("malformed", detail="a hash is 64 lower-case hex characters")
    return bytes.fromhex(value)


def _leaf(record_hash: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + record_hash).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """The largest power of two strictly below n (n > 1)."""
    return 1 << ((n - 1).bit_length() - 1)


def _root(leaves: Sequence[bytes], lo: int, hi: int) -> bytes:
    n = hi - lo
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[lo]
    k = _split(n)
    return _node(_root(leaves, lo, lo + k), _root(leaves, lo + k, hi))


def _leaves(hashes: Sequence[str]) -> list[bytes]:
    return [_leaf(_raw(h)) for h in hashes]


def leaf_hash(record_hash: str) -> str:
    return _leaf(_raw(record_hash)).hex()


def merkle_root(hashes: Sequence[str]) -> str:
    """The root over the record hashes, in order."""
    leaves = _leaves(hashes)
    return _root(leaves, 0, len(leaves)).hex()


def inclusion_proof(hashes: Sequence[str], index: int) -> list[str]:
    """The audit path for record `index` in the tree over all of `hashes`."""
    leaves = _leaves(hashes)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(leaves):
        raise RecordError("malformed", detail="no such record")

    def path(m: int, lo: int, hi: int) -> list[bytes]:
        n = hi - lo
        if n == 1:
            return []
        k = _split(n)
        if m < k:
            return path(m, lo, lo + k) + [_root(leaves, lo + k, hi)]
        return path(m - k, lo + k, hi) + [_root(leaves, lo, lo + k)]

    return [h.hex() for h in path(index, 0, len(leaves))]


def verify_inclusion(record_hash: str, index: int, size: int, proof: Sequence[str], root: str) -> bool:
    """Is the record with this hash number `index` of the `size` records under `root`? Never raises."""
    try:
        if isinstance(index, bool) or isinstance(size, bool) or not isinstance(index, int) or not isinstance(size, int):
            return False
        if not 0 <= index < size or not isinstance(proof, (list, tuple)) or len(proof) > 64:
            return False
        fn, sn, r = index, size - 1, _leaf(_raw(record_hash))
        for item in proof:
            p = _raw(item)
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                r = _node(p, r)
                if not fn & 1:
                    while fn and not fn & 1:
                        fn >>= 1
                        sn >>= 1
            else:
                r = _node(r, p)
            fn >>= 1
            sn >>= 1
        return sn == 0 and hmac.compare_digest(r, _raw(root))
    except RecordError:
        return False


def consistency_proof(hashes: Sequence[str], old_size: int) -> list[str]:
    """The proof that the first `old_size` records are a prefix of all of `hashes`.

    Empty when old_size is 0 or equals len(hashes) (nothing needs proving)."""
    leaves = _leaves(hashes)
    if isinstance(old_size, bool) or not isinstance(old_size, int) or not 0 <= old_size <= len(leaves):
        raise RecordError("malformed", detail="no such size")
    if old_size == 0 or old_size == len(leaves):
        return []

    def sub(m: int, lo: int, hi: int, whole: bool) -> list[bytes]:
        n = hi - lo
        if m == n:
            return [] if whole else [_root(leaves, lo, hi)]
        k = _split(n)
        if m <= k:
            return sub(m, lo, lo + k, whole) + [_root(leaves, lo + k, hi)]
        return sub(m - k, lo + k, hi, False) + [_root(leaves, lo, lo + k)]

    return [h.hex() for h in sub(old_size, 0, len(leaves), True)]


def verify_consistency(old_size: int, new_size: int, old_root: str, new_root: str, proof: Sequence[str]) -> bool:
    """Does the tree of `new_size` records under `new_root` extend the tree of `old_size` under `old_root`? Never raises."""
    try:
        if any(isinstance(n, bool) or not isinstance(n, int) for n in (old_size, new_size)):
            return False
        if not 0 <= old_size <= new_size or not isinstance(proof, (list, tuple)) or len(proof) > 128:
            return False
        first, second = _raw(old_root), _raw(new_root)
        path = [_raw(item) for item in proof]
        if old_size == new_size:
            return not path and hmac.compare_digest(first, second)
        if old_size == 0:
            return not path and hmac.compare_digest(first, hashlib.sha256(b"").digest())
        if old_size & (old_size - 1) == 0:
            path.insert(0, first)
        if not path:
            return False
        fn, sn = old_size - 1, new_size - 1
        while fn & 1:
            fn >>= 1
            sn >>= 1
        fr = sr = path[0]
        for c in path[1:]:
            if sn == 0:
                return False
            if fn & 1 or fn == sn:
                fr = _node(c, fr)
                sr = _node(c, sr)
                if not fn & 1:
                    while fn and not fn & 1:
                        fn >>= 1
                        sn >>= 1
            else:
                sr = _node(sr, c)
            fn >>= 1
            sn >>= 1
        return sn == 0 and hmac.compare_digest(fr, first) and hmac.compare_digest(sr, second)
    except RecordError:
        return False
