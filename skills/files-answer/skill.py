"""Answer a question from the person's own files, citing a passage for every statement.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

Designed for small free models: retrieval does the work on this computer and
the model only reads a handful of numbered passages.

  index     the named folders, incrementally (index.py)
  retrieve  BM25 always; when the router's `embed` alias answers and the
            budget allows, embeddings of the best lexical candidates are
            fused in by reciprocal rank (retrieve.py)
  answer    ONE chat call at the job's tier (everyday unless raised), asking
            for a small JSON object of verbatim-quote claims, read defensively
            (answer.py). An answer with no visible text is asked once more with
            more room one tier up (recover.py); if it is empty again the result
            says the question was NOT answered, never "not found".
  claims    each claim's quote must really be in the passage it names, and
            every number it states must be in its quote (quotes.py); claims
            that fail are dropped. The answer is written from the claims that
            stand, each sentence citing claim numbers.
  check     the same, again, on what is delivered: every claim's quote is in
            its passage, every sentence that states a fact cites a claim that
            exists, and its numbers are in a cited quote; "not found" carries
            no claims; an unanswered call never passes

Nothing from a file goes into an event or a note: counts only.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import answer as answer_mod
from . import index, quotes, recover, retrieve
from .textwork import mostly_chinese, tokens

TOP_K = 6
MAX_TOP_K = 12
EMBED_POOL = 64  # at most this many lexical candidates are embedded and re-ranked
EMBED_BATCH = 32
ANSWER_REQUESTS = 3  # requests kept back for the answer (its one recovery, and the runner's retry) before embedding
ANSWER_TOKENS = 12_000
MAX_CLAIMS = 8


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
    reply = recover.ask(ctx.llm, answer_mod.messages(question, [p["text"] for p in passages]),
                        max_tokens=answer_mod.MAX_OUTPUT_TOKENS)
    if getattr(reply, "unanswered", False):
        ctx.event("answer", read="empty", claims=0, dropped=0, not_found=False, empty=True, recovered=False,
                  unanswered=True, attempts=reply.attempts, reason=reply.reason, stopped=reply.stopped)
        ctx.note(_unanswered_note(reply, zh), incomplete=True)
        return {**base, "passages": passages, "answer": "", "text": _unanswered_text(zh), "not_found": False,
                "unanswered": {"reason": reply.reason, "attempts": reply.attempts, "stopped": reply.stopped}}
    parsed = answer_mod.parse_quoted(reply.text)
    kept, dropped = quotes.keep(parsed["claims"], {p["n"]: p["text"] for p in passages}, context=question, limit=MAX_CLAIMS)
    attempts = int(getattr(reply, "attempts", 1) or 1)
    ctx.event("answer", read=parsed["read"], claims=len(kept), dropped=sum(dropped.values()), not_found=parsed["not_found"],
              empty=False, recovered=attempts > 1, unanswered=False, attempts=attempts)
    return _output(base, parsed, kept, dropped, passages, question, zh, attempts)


def _unanswered_text(zh: bool) -> str:
    return ("模型沒有回答（沒有傳回任何文字），所以這個問題還沒有答案。這不代表你的檔案裡沒有答案。" if zh else
            "The model gave no answer (it returned no text), so this question is not answered yet. "
            "This does not mean the answer is not in your files.")


def _unanswered_note(reply: Any, zh: bool) -> str:
    why = ("its output allowance went on hidden reasoning" if reply.reason == recover.REASONING else "it returned no text")
    if reply.stopped == "budget":
        return f"The model gave no answer ({why}) and the budget had no room to ask again."
    return f"The model gave no answer ({why}), also when asked again with more room on a stronger tier."


def _remap(text: str, ids: dict[int, int]) -> str:
    """The model's [k] markers (its own claim places) as the ids of the claims that stand; [0] for one that does not."""
    def swap(match: re.Match[str]) -> str:
        return "".join(f"[{ids.get(int(n), 0)}]" for n in re.findall(r"\d{1,3}", match.group(1)))

    return answer_mod._MARK.sub(swap, quotes.brackets(text))


def _output(base: dict[str, Any], parsed: dict[str, Any], kept: list[dict[str, Any]], dropped: dict[str, int],
            passages: list[dict[str, Any]], question: str, zh: bool, attempts: int) -> dict[str, Any]:
    by_n = {p["n"]: p for p in passages}
    used: dict[int, list[int]] = {}  # passage number -> the paragraphs of it that claims rest on
    claims = []
    ids: dict[int, int] = {}
    for number, c in enumerate(kept, start=1):
        p = by_n[c["n"]]
        hit = quotes.paragraph_of(p, c["start"])
        paragraphs = [hit] if hit else list(range(p["paragraph"], p["paragraph_end"] + 1))
        used.setdefault(c["n"], []).extend(paragraphs)
        source = _label(p["shown"], paragraphs, zh)
        claims.append({"id": number, "says": c["says"], "text": c["says"], "quote": c["quote"], "n": c["n"],
                       "start": c["start"], "end": c["end"], "cites": [c["n"]], "sources": [source], "source": source})
        ids.update({place: number for place in c["indexes"]})
    not_found = bool(parsed["not_found"]) and not claims
    quotes_by_id = {c["id"]: c["quote"] for c in claims}
    body = ""
    written = "none"
    if not_found:
        body = _not_found(zh)
    elif claims:
        body = _remap(parsed["answer"], ids).strip()
        found = quotes.sentences(body.splitlines())
        if body and any(s["cites"] for s in found) and not quotes.statement_problems(found, quotes_by_id, question):
            written = "model"
        else:  # the model's own sentences do not stand on the claims: the claims are the answer
            body = "\n".join(f"{c['says']} [{c['id']}]" for c in claims)
            written = "claims"
    citations = [{"n": n, "path": by_n[n]["path"], "paragraphs": sorted(set(used[n])),
                  "label": _label(by_n[n]["shown"], used[n], zh)} for n in sorted(used)]
    text = body
    if claims and not not_found:
        text += "\n\n" + ("來源：" if zh else "Sources:") + "\n" + "\n".join(
            f"[{c['id']}] {c['source']}: “{c['quote']}”" for c in claims)
    return {**base, "answer": body, "text": text, "claims": claims, "not_found": not_found, "citations": citations,
            "passages": passages, "read": parsed["read"], "written": written, "dropped_claims": dict(sorted(dropped.items())),
            "attempts": attempts}


def check(output: Any, ctx: Any = None) -> list[str]:
    """Reasons the answer may not be delivered as checked; empty when it passes. Reasons name claims by number only."""
    if not isinstance(output, dict):
        return ["the skill returned no answer"]
    if output.get("unanswered"):
        return ["the model gave no answer (no visible text), so the question is not answered"]
    passages = {p["n"]: p["text"] for p in output.get("passages", [])}
    claims = output.get("claims", [])
    if output.get("not_found"):
        return ["the answer says it was not found but still makes claims"] if claims else []
    question = str(output.get("question", ""))
    reasons: list[str] = []
    if not str(output.get("answer", "")).strip():
        reasons.append("the answer is empty")
    if not claims:
        reasons.append("the answer makes no verified claims (no quote from a passage stood)")
    quotes_by_id: dict[int, str] = {}
    for number, claim in enumerate(claims, start=1):
        code, start, end = quotes.verify(str(claim.get("quote", "")), claim.get("n"), str(claim.get("says", "")), passages, question)
        if code:
            reasons.append(f"claim {number}: {_why(code)}")
            continue
        quotes_by_id[claim.get("id", number)] = str(claim.get("quote", ""))
    found = quotes.sentences(str(output.get("answer", "")).splitlines())
    for code, sentence, claim_id in quotes.statement_problems(found, quotes_by_id, question):
        reasons.append(f"sentence {sentence}: {_why(code, claim_id)}")
    return reasons


def _why(code: str, claim_id: int = 0) -> str:
    return {
        quotes.NO_PASSAGE: "cites a passage that was not given",
        quotes.NO_QUOTE: "cites no quote",
        quotes.TOO_SHORT: "its quote is too short to check",
        quotes.TOO_LONG: "its quote is too long",
        quotes.NOT_FOUND: "its quote is not in the passage it cites",
        quotes.NUMBER: "states a number that is not in the quote it cites",
        quotes.NO_SAYS: "says nothing that can be checked",
        quotes.UNCITED: "states a fact without a source",
        quotes.UNKNOWN_CLAIM: f"cites claim {claim_id}, which does not exist",
    }.get(code, code)
