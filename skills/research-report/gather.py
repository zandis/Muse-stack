"""Step 2, gather: find the passages for each sub-question. No model call (embeddings are optional).

`gather(ctx, question, subs, zh)` is the interface the pipeline uses; it returns
a `Gathered` with numbered passages. Today the only source is the folders the
person named (`from_folders`): the same index, BM25 and embedding fusion as the
files-answer skill. A later source (pages fetched through a sandboxed tool) only
has to return the same passage dictionaries; nothing after this step changes.

A passage: {"n", "path", "shown", "paragraph", "paragraph_end", "paragraphs", "text", "label", "subs": [sub numbers]}
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from . import index, retrieve
from .textwork import tokens

PER_SUB = 4  # passages read for one sub-question
PER_SOURCE = 3  # ... of which at most this many from one file
MAX_PASSAGES = 18  # distinct passages in a job
EMBED_POOL = 64
EMBED_BATCH = 32
QUESTION_WEIGHT = 0.3


@dataclass
class Gathered:
    passages: list[dict[str, Any]] = field(default_factory=list)
    by_sub: dict[int, list[int]] = field(default_factory=dict)  # sub-question number -> passage numbers, best first
    mode: str = "none"  # none | lexical | hybrid
    files: int = 0
    chunks: int = 0
    embed: dict[str, Any] = field(default_factory=dict)  # requests, tokens, cost_usd, providers of the embedding calls


Source = Callable[[Any, str, list[str], bool, int, int], Gathered]


def shown(path: str, roots: list[str]) -> str:
    """The path as the person knows it: relative to the folder they named when there is one folder."""
    if len(roots) == 1 and path.startswith(roots[0].rstrip(os.sep) + os.sep):
        return os.path.relpath(path, roots[0])
    return path


def label(name: str, numbers: list[int], zh: bool) -> str:
    """`file › paragraph 3`. Several paragraphs: `3, 5`; a run of them: `3-5`."""
    numbers = sorted(set(numbers))
    run = len(numbers) > 2 and numbers[-1] - numbers[0] == len(numbers) - 1
    span = f"{numbers[0]}-{numbers[-1]}" if run else ("、" if zh else ", ").join(str(n) for n in numbers)
    return f"{name} › 第 {span} 段" if zh else f"{name} › paragraph {span}"


def notes(ctx: Any, stats: index.Stats) -> None:
    if stats.pdf_skipped:
        ctx.note(f"{stats.pdf_skipped} PDF file(s) were not read: no PDF reader is installed with Muse.", incomplete=True)
    if stats.large:
        ctx.note(f"{stats.large} file(s) were too large to read (over {index.MAX_FILE_BYTES // 1_000_000} MB each).", incomplete=True)
    if stats.unreadable:
        ctx.note(f"{stats.unreadable} file(s) could not be read as text.", incomplete=True)
    if stats.hit_files or stats.hit_bytes or stats.hit_chunks:
        ctx.note("Not every file was searched: a limit on how much is read was reached.", incomplete=True)
    if ctx.workspace.skipped_links:
        ctx.note(f"{ctx.workspace.skipped_links} link(s) pointing outside the folders you named were not followed.")


def _semantic(ctx: Any, db: Any, queries: list[str], chunks: list[index.Chunk], lexical: list[list[int]],
              keep_requests: int, keep_tokens: int, used: dict[str, Any]) -> list[list[int]] | None:
    """One ranking of chunk indexes per query by embedding, or None when embeddings are not to be had."""
    if str(ctx.inputs.get("embeddings", "auto")).strip().lower() == "off" or not chunks:
        return None
    if len(chunks) <= EMBED_POOL:
        pool = list(range(len(chunks)))
    else:
        pool = [int(i) for i in retrieve.fuse(lexical)][:EMBED_POOL]
    left = ctx.llm.remaining()
    if not pool or left["requests"] < keep_requests + 2 or left["tokens"] < keep_tokens:
        ctx.event("embed_skipped", reason="budget")
        return None

    def count(asked: Any) -> None:
        used["requests"] += 1
        used["cost_usd"] += asked.cost_usd or 0.0
        if asked.provider and asked.provider not in used["providers"]:
            used["providers"].append(asked.provider)

    try:
        asked = ctx.llm.embed_with_facts(queries)
        count(asked)
        vectors = asked.vectors
        model = f"{asked.deployment or asked.provider or 'embed'}:{len(vectors[0])}"
        have = index.embeddings(db, [chunks[i].id for i in pool], model)
        missing = [i for i in pool if chunks[i].id not in have]
        for start in range(0, len(missing), EMBED_BATCH):
            left = ctx.llm.remaining()
            if left["requests"] <= keep_requests or left["tokens"] < keep_tokens:
                break
            batch = missing[start:start + EMBED_BATCH]
            got = ctx.llm.embed_with_facts([chunks[i].text for i in batch])
            count(got)
            packed = [(chunks[i].id, retrieve.pack(v)) for i, v in zip(batch, got.vectors) if len(v) == len(vectors[0])]
            index.store_embeddings(db, packed, model)
            have.update(dict(packed))
    except ctx.llm.Error as exc:  # deferred, refused, over budget, no embed alias: search by words only
        ctx.event("embed_skipped", reason=type(exc).__name__)
        return None
    rankings = []
    for query in vectors:
        scored = [(retrieve.cosine(query, retrieve.unpack(have[chunks[i].id])), i) for i in pool if chunks[i].id in have]
        rankings.append([i for _, i in sorted(scored, key=lambda pair: (-pair[0], pair[1]))])
    if not any(rankings):
        return None
    ctx.event("embedded", pool=len(pool), queries=len(queries))
    return rankings


def from_folders(ctx: Any, question: str, subs: list[str], zh: bool, keep_requests: int, keep_tokens: int) -> Gathered:
    """Passages from the folders named for this run. `keep_*`: budget that must stay for the later steps."""
    roots = ctx.workspace.roots()
    out = Gathered(embed={"requests": 0, "tokens": 0, "cost_usd": 0.0, "providers": []})
    pdf = index.pdf_reader()
    db = index.open_db(ctx.data_dir)
    try:
        stats = index.sync(db, ctx.workspace, index.Caps(), pdf)
        ctx.event("index", **stats.counts())
        notes(ctx, stats)
        chunks = index.load_chunks(db, stats.file_ids)
        out.files, out.chunks = len(stats.file_ids), len(chunks)
        if not chunks:
            return out
        docs = [tokens(c.text) for c in chunks]
        whole = retrieve.bm25(tokens(question), docs)
        lexical = []
        for sub in subs:  # the sub-question's own words, plus a little of the whole question to keep it on the subject
            own = retrieve.bm25(tokens(sub), docs)
            lexical.append(retrieve.ranked([s + QUESTION_WEIGHT * w if s > 0 else 0.0 for s, w in zip(own, whole)]))
        before = ctx.llm.remaining()["tokens"]
        semantic = _semantic(ctx, db, subs, chunks, lexical, keep_requests, keep_tokens, out.embed)
        out.embed["tokens"] = int(before - ctx.llm.remaining()["tokens"])
    finally:
        db.close()
    out.mode = "hybrid" if semantic else "lexical"
    numbers: dict[int, int] = {}  # chunk index -> passage number
    for number, _ in enumerate(subs, start=1):
        order = lexical[number - 1]
        if semantic:
            order = [int(i) for i in retrieve.fuse([order, semantic[number - 1]])]
        per_file: dict[str, int] = {}
        mine: list[int] = []
        for i in order:
            if len(mine) >= PER_SUB:
                break
            path = chunks[i].path
            if per_file.get(path, 0) >= PER_SOURCE or (i not in numbers and len(numbers) >= MAX_PASSAGES):
                continue
            per_file[path] = per_file.get(path, 0) + 1
            if i not in numbers:  # the same passage found for two sub-questions is read as one source
                c = chunks[i]
                numbers[i] = len(numbers) + 1
                name = shown(c.path, roots)
                out.passages.append({
                    "n": numbers[i], "path": c.path, "shown": name, "paragraph": c.para_start, "paragraph_end": c.para_end,
                    "paragraphs": list(c.paras), "text": c.text, "subs": [],
                    "label": label(name, list(range(c.para_start, c.para_end + 1)), zh)})
            out.passages[numbers[i] - 1]["subs"].append(number)
            mine.append(numbers[i])
        out.by_sub[number] = mine
    ctx.event("gather", mode=out.mode, passages=len(out.passages), subs=len(subs),
              empty=sum(1 for v in out.by_sub.values() if not v))
    return out


def gather(ctx: Any, question: str, subs: list[str], zh: bool, keep_requests: int, keep_tokens: int,
           source: Source = from_folders) -> Gathered:
    return source(ctx, question, subs, zh, keep_requests, keep_tokens)
