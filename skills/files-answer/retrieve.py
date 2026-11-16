"""Ranking: BM25 over terms, cosine over embeddings, and reciprocal rank fusion. Standard library only."""

from __future__ import annotations

import math
from array import array
from collections import Counter
from typing import Hashable, Sequence

K1 = 1.5
B = 0.75
RRF_K = 60


def bm25(query: Sequence[str], docs: Sequence[Sequence[str]], k1: float = K1, b: float = B) -> list[float]:
    """Okapi BM25 score of every document for the query terms (each distinct query term counts once)."""
    n = len(docs)
    if not n:
        return []
    wanted = set(query)
    counts = [Counter(t for t in doc if t in wanted) for doc in docs]
    lengths = [len(doc) for doc in docs]
    average = (sum(lengths) / n) or 1.0
    scores = [0.0] * n
    for term in wanted:
        df = sum(1 for c in counts if term in c)
        if not df:
            continue
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for i, c in enumerate(counts):
            tf = c.get(term, 0)
            if tf:
                scores[i] += idf * tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * lengths[i] / average))
    return scores


def ranked(scores: Sequence[float], limit: int | None = None) -> list[int]:
    """Indexes with a positive score, best first; ties keep document order."""
    order = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (-scores[i], i))
    return order if limit is None else order[:limit]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def fuse(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K) -> list[Hashable]:
    """Reciprocal rank fusion: each list gives an item 1 / (k + rank); best total first."""
    totals: dict[Hashable, float] = {}
    first: dict[Hashable, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            totals[item] = totals.get(item, 0.0) + 1.0 / (k + rank)
            first.setdefault(item, len(first))
    return sorted(totals, key=lambda item: (-totals[item], first[item]))


def pack(vector: Sequence[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    values = array("f")
    values.frombytes(blob)
    return values.tolist()
