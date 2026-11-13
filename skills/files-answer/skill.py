"""Answer a question from the person's own files, citing a passage for every statement.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

Designed for small free models: retrieval does the work on this computer and
the model only reads a handful of numbered passages.

  index     the named folders, incrementally (index.py)
  retrieve  BM25 always; when the router's `embed` alias answers and the
            budget allows, embeddings of the best lexical candidates are
            fused in by reciprocal rank (retrieve.py)
  answer    ONE chat call at the job's tier (everyday unless raised), asking
            for a small JSON object, read defensively (answer.py)
  check     every claim cites a passage that was given and shares terms with
            it; "not found" carries no claims

Nothing from a file goes into an event or a note: counts only.
"""

from __future__ import annotations

import os
from typing import Any

from . import answer as answer_mod
from . import index, retrieve
from .textwork import mostly_chinese, overlap, terms, tokens

TOP_K = 6
MAX_TOP_K = 12
EMBED_POOL = 64  # at most this many lexical candidates are embedded and re-ranked
EMBED_BATCH = 32
ANSWER_REQUESTS = 2  # requests kept back for the answer (and one retry) before any embedding is tried
ANSWER_TOKENS = 12_000

# The citation check. A claim's terms are its words / numbers / character pairs (textwork.py).
MIN_SHARE = 0.30  # of the claim's terms found in the passages it cites, taken together
MIN_SHARE_EACH = 0.15  # ... and in each single cited passage
MIN_SHARED = 2  # shared terms needed (1 when the claim has fewer than 4 terms)


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _shown(path: str, roots: list[str]) -> str:
    """The path as the person knows it: relative to the folder they named when there is one folder."""
    if len(roots) == 1 and path.startswith(roots[0].rstrip(os.sep) + os.sep):
        return os.path.relpath(path, roots[0])
    return path


def _label(shown: str, numbers: list[int], zh: bool) -> str:
    """`file path › paragraph 3`. Several paragraphs: `3, 5`; a run of them: `3-5`."""
    numbers = sorted(set(numbers))
    run = len(numbers) > 2 and numbers[-1] - numbers[0] == len(numbers) - 1
    span = f"{numbers[0]}-{numbers[-1]}" if run else ("、" if zh else ", ").join(str(n) for n in numbers)
    return f"{shown} › 第 {span} 段" if zh else f"{shown} › paragraph {span}"


def _paragraph(claim: str, passage: dict[str, Any]) -> int | None:
    """The paragraph of a cited passage that shares most with the claim (None when no single one stands out)."""
    best: tuple[int, float] = (0, 0.0)
    number = None
    for part, n in zip(passage["text"].split("\n\n"), passage["paragraphs"]):
        score = overlap(claim, part)
        if n and score > best:
            best, number = score, n
    return number


def _hybrid(ctx: Any, db: Any, question: str, chunks: list[index.Chunk], lexical: list[int]) -> tuple[list[int], str]:
    """(order of chunk indexes, mode). Falls back to the lexical order whenever embeddings are not to be had."""
    if str(ctx.inputs.get("embeddings", "auto")).strip().lower() == "off":
        return lexical, "lexical"
    pool = list(range(len(chunks))) if len(chunks) <= EMBED_POOL else lexical[:EMBED_POOL]
    if not pool:
        return lexical, "lexical"
    left = ctx.llm.remaining()
    if left["requests"] < ANSWER_REQUESTS + 2 or left["tokens"] < ANSWER_TOKENS:
        ctx.event("embed_skipped", reason="budget")
        return lexical, "lexical"
    try:
        asked = ctx.llm.embed_with_facts([question])
        query, model = asked.vectors[0], f"{asked.deployment or asked.provider or 'embed'}:{len(asked.vectors[0])}"
        have = index.embeddings(db, [chunks[i].id for i in pool], model)
        missing = [i for i in pool if chunks[i].id not in have]
        for start in range(0, len(missing), EMBED_BATCH):
            left = ctx.llm.remaining()
            if left["requests"] <= ANSWER_REQUESTS or left["tokens"] < ANSWER_TOKENS:
                break
            batch = missing[start:start + EMBED_BATCH]
            vectors = ctx.llm.embed([chunks[i].text for i in batch])
            packed = [(chunks[i].id, retrieve.pack(v)) for i, v in zip(batch, vectors) if len(v) == len(query)]
            index.store_embeddings(db, packed, model)
            have.update(dict(packed))
    except ctx.llm.Error as exc:  # deferred, refused, over budget, no embed alias: search by words only
        ctx.event("embed_skipped", reason=type(exc).__name__)
        return lexical, "lexical"
    scored = [(retrieve.cosine(query, retrieve.unpack(have[chunks[i].id])), i) for i in pool if chunks[i].id in have]
    semantic = [i for _, i in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]
    if not semantic:
        return lexical, "lexical"
    ctx.event("embedded", chunks=len(semantic), pool=len(pool))
    return [int(i) for i in retrieve.fuse([lexical, semantic])], "hybrid"


def _not_found(zh: bool) -> str:
    return "在你指定的資料夾裡找不到這個問題的答案。" if zh else "I could not find the answer to this in the files you named."


def _notes(ctx: Any, stats: index.Stats, pdf_ok: bool) -> None:
    if stats.pdf_skipped:
        ctx.note(f"{stats.pdf_skipped} PDF file(s) were not read: no PDF reader is installed with Muse.", incomplete=True)
    if stats.large:
        ctx.note(f"{stats.large} file(s) were too large to read (over {index.MAX_FILE_BYTES // 1_000_000} MB each).", incomplete=True)
    if stats.unreadable:
        ctx.note(f"{stats.unreadable} file(s) could not be read as text.", incomplete=True)
    if stats.hit_files:
        ctx.note(f"Only the first {stats.seen} files were searched: the limit on files was reached.", incomplete=True)
    if stats.hit_bytes:
        ctx.note("Not every new file was read: the limit on how much is read in one run was reached. Run it again to continue.",
                 incomplete=True)
    if stats.hit_chunks:
        ctx.note("Not every file was searched: the limit on the size of the index was reached.", incomplete=True)
    if ctx.workspace.skipped_links:
        ctx.note(f"{ctx.workspace.skipped_links} link(s) pointing outside the folders you named were not followed.")


def run(ctx: Any) -> dict[str, Any]:
    question = " ".join(str(ctx.inputs.get("question", "")).split())
    if not question:
        ctx.stop("Give a question: --input question=\"...\"")
    if len(question) > 2000:
        ctx.stop("The question is too long (over 2000 characters).")
    roots = ctx.workspace.roots()
    if not roots:
        ctx.stop("Name at least one folder to read: --folder PATH")
    zh = mostly_chinese(question)
    top_k = _int(ctx.inputs.get("top_k"), TOP_K, 1, MAX_TOP_K)
    caps = index.Caps(
        files=_int(ctx.inputs.get("max_files"), index.MAX_FILES, 1, index.MAX_FILES),
        file_bytes=_int(ctx.inputs.get("max_file_bytes"), index.MAX_FILE_BYTES, 1, index.MAX_FILE_BYTES),
        run_bytes=_int(ctx.inputs.get("max_bytes"), index.MAX_RUN_BYTES, 1, index.MAX_RUN_BYTES),
        chunks=_int(ctx.inputs.get("max_chunks"), index.MAX_CHUNKS, 1, index.MAX_CHUNKS),
    )
    pdf = index.pdf_reader()
    db = index.open_db(ctx.data_dir)
    try:
        stats = index.sync(db, ctx.workspace, caps, pdf)
        ctx.event("index", **stats.counts())
        _notes(ctx, stats, pdf is not None)
        chunks = index.load_chunks(db, stats.file_ids)
        base = {"question": question, "passages": [], "citations": [], "claims": [],
                "retrieval": {"mode": "none", "files": len(stats.file_ids), "chunks": len(chunks), "sent": 0}}
        if not chunks:
            ctx.note("No readable text was found in the folders you named, so no model was asked.")
            return {**base, "answer": _not_found(zh), "text": _not_found(zh), "not_found": True}
        scores = retrieve.bm25(tokens(question), [tokens(c.text) for c in chunks])
        lexical = retrieve.ranked(scores)
        order, mode = _hybrid(ctx, db, question, chunks, lexical)
    finally:
        db.close()
    chosen = [chunks[i] for i in order[:top_k]]
    ctx.event("retrieve", mode=mode, lexical_hits=len(lexical), sent=len(chosen))
    base["retrieval"].update(mode=mode, sent=len(chosen))
    if not chosen:
        ctx.note("No passage in your files shares words with the question, so no model was asked.")
        return {**base, "answer": _not_found(zh), "text": _not_found(zh), "not_found": True}

    passages = [{"n": n, "path": c.path, "shown": _shown(c.path, roots), "paragraph": c.para_start,
                 "paragraph_end": c.para_end, "paragraphs": list(c.paras), "text": c.text,
                 "label": _label(_shown(c.path, roots), list(range(c.para_start, c.para_end + 1)), zh)}
                for n, c in enumerate(chosen, start=1)]
    reply = ctx.llm.ask(answer_mod.messages(question, [p["text"] for p in passages]), max_tokens=answer_mod.MAX_OUTPUT_TOKENS)
    parsed = answer_mod.parse(reply.text)
    ctx.event("answer", read=parsed["read"], claims=len(parsed["claims"]), not_found=parsed["not_found"],
              empty=reply.answer == "empty-reasoning")
    return _output(base, parsed, passages, zh)


def _output(base: dict[str, Any], parsed: dict[str, Any], passages: list[dict[str, Any]], zh: bool) -> dict[str, Any]:
    by_n = {p["n"]: p for p in passages}
    used: dict[int, list[int]] = {}  # passage number -> the paragraphs of it that claims rest on
    claims = []
    for c in parsed["claims"]:
        sources = []
        for n in c["cites"]:
            if n not in by_n:
                continue
            p = by_n[n]
            hit = _paragraph(c["text"], p)
            numbers = [hit] if hit else list(range(p["paragraph"], p["paragraph_end"] + 1))
            used.setdefault(n, []).extend(numbers)
            sources.append(_label(p["shown"], numbers, zh))
        claims.append({"text": c["text"], "cites": c["cites"], "sources": sources})
    not_found = bool(parsed["not_found"])
    body = parsed["answer"]
    if not_found:
        body = body or _not_found(zh)
    elif claims and not answer_mod._MARK.search(body):
        # The model's prose carries no markers: show the cited statements instead, so every line points somewhere.
        body = "\n".join(f"{c['text']} " + "".join(f"[{n}]" for n in c["cites"]) for c in claims).strip()
    citations = [{"n": n, "path": by_n[n]["path"], "paragraphs": sorted(set(used[n])),
                  "label": _label(by_n[n]["shown"], used[n], zh)} for n in sorted(used)]
    text = body
    if citations and not not_found:
        text += "\n\n" + ("來源：" if zh else "Sources:") + "\n" + "\n".join(f"[{c['n']}] {c['label']}" for c in citations)
    return {**base, "answer": body, "text": text, "claims": claims, "not_found": not_found, "citations": citations,
            "passages": passages, "read": parsed["read"]}


def check(output: Any, ctx: Any = None) -> list[str]:
    """Reasons the answer may not be delivered as checked; empty when it passes. Reasons name claims by number only."""
    if not isinstance(output, dict):
        return ["the skill returned no answer"]
    passages = {p["n"]: p["text"] for p in output.get("passages", [])}
    claims = output.get("claims", [])
    if output.get("not_found"):
        return ["the answer says it was not found but still makes claims"] if claims else []
    reasons: list[str] = []
    if not str(output.get("answer", "")).strip():
        reasons.append("the answer is empty")
    if not claims:
        reasons.append("the answer makes no cited claims")
    for number, claim in enumerate(claims, start=1):
        cites = claim.get("cites") or []
        if not cites:
            reasons.append(f"claim {number} cites no passage")
            continue
        missing = [n for n in cites if n not in passages]
        if missing:
            reasons.append(f"claim {number} cites passage {missing[0]}, which was not given")
            continue
        mine = terms(claim.get("text", ""))
        if not mine:
            reasons.append(f"claim {number} says nothing that can be checked")
            continue
        need = MIN_SHARED if len(mine) >= 4 else 1
        each = {n: overlap(claim["text"], passages[n]) for n in cites}
        weak = [n for n in cites if each[n][0] < need or each[n][1] < MIN_SHARE_EACH]
        if weak:
            reasons.append(f"claim {number} shares too little with passage {weak[0]}")
            continue
        together = overlap(claim["text"], "\n".join(passages[n] for n in cites))
        if together[0] < need or together[1] < MIN_SHARE:
            reasons.append(f"claim {number} shares too little with the passages it cites "
                           f"({together[1]:.2f} of its terms; {MIN_SHARE:.2f} needed)")
    return reasons
