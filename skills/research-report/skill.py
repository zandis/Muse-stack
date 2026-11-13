"""A short research report from the person's own material, with a source behind every statement.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

Most of the work is light: only the last step needs a strong model, and that
is the step the router may hold back or offer to hand off.

  plan        light, 1 call    the question becomes 2 to 6 sub-questions (plan.py; rules when the reply is unusable)
  gather      no model         passages per sub-question from the named folders (gather.py; embeddings optional)
  extract     light, many      claims as verbatim quotes; a claim whose quote is not in its passage, or whose numbers
                               are not in its quote, is dropped at once (extract.py, quotes.py)
  synthesise  heavy, 1 call    the report, written from the verified claims only (report.py), each sentence citing
                               claim numbers, sent with `x-muse-handoff: suggest`

Every call goes through recover.py: an answer with no visible text (a reasoning model that spent its output
allowance thinking) is asked once more with more room one tier up. If it is still empty, its passages are NOT
READ: the sub-question is listed under "Not done", never under "what the sources did not answer", the run is
incomplete, and the check does not pass while anything is not read.

Before the heavy call the verified claims are handed to the runner with
`ctx.keep(...)`. If the router defers or refuses that call, the person gets the
router's choices AND the claims by sub-question, with a prompt they can paste
into their own app. The same partial result is what a budget stop returns.

index.py, retrieve.py, textwork.py, answer.py, quotes.py and recover.py are
byte-for-byte copies of the files in skills/files-answer: a skill is imported as
a private package from its own hashed bytes, so one skill cannot import another's
modules. A test fails when the copies drift.

Nothing from a file, a prompt or an answer goes into an event, a note or a check reason.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from . import extract, gather, plan, quotes, recover, report
from .textwork import mostly_chinese, weight

MAX_QUESTION_CHARS = 2000
SYNTH_REQUESTS = 1  # kept back for the report while passages are being read
MAX_CLAIMS = 36
MAX_REASONS = 12
STEPS = ("plan", "gather", "extract", "synthesise")
TIER = {"plan": "light", "gather": "embed", "extract": "light", "synthesise": "heavy"}


# ---------------------------------------------------------------- small helpers


class _Usage:
    """What each step used: requests, tokens, providers, cost, and empty answers. Counted from the job's own meter."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.steps: dict[str, dict[str, Any]] = {
            name: {"task": TIER[name], "requests": 0, "tokens": 0, "cost_usd": 0.0, "providers": [],
                   "empty": 0, "recovered": 0, "unanswered": 0} for name in STEPS}

    def ask(self, step: str, messages: list[dict[str, str]], *, keep_requests: int = 0, **kwargs: Any) -> Any:
        """One call through recover.ask: an `Answer`, or a `recover.Unanswered` when it stayed empty."""
        row = self.steps[step]
        before = self.ctx.llm.remaining()
        try:
            reply = recover.ask(self.ctx.llm, messages, keep_requests=keep_requests, **kwargs)
        finally:
            after = self.ctx.llm.remaining()
            row["requests"] += int(before["requests"] - after["requests"])
            row["tokens"] += max(0, int(before["tokens"] - after["tokens"]))
        attempts = int(getattr(reply, "attempts", 1) or 1)
        if getattr(reply, "unanswered", False):
            row["empty"] += attempts
            row["unanswered"] += 1
        elif attempts > 1:
            row["empty"] += 1
            row["recovered"] += 1
        row["cost_usd"] = round(row["cost_usd"] + (getattr(reply, "cost_usd", 0.0) or 0.0), 6)
        row["task"] = getattr(reply, "task", "") or row["task"]
        provider = getattr(reply, "provider", "")
        if provider and provider not in row["providers"]:
            row["providers"].append(provider)
        return reply


def _language(ctx: Any, question: str) -> bool:
    """True for Traditional Chinese. `lang`: zh, zh-TW, en, or auto (the question's language)."""
    asked = str(ctx.inputs.get("lang", "") or "auto").strip().lower()
    if asked in ("", "auto"):
        return mostly_chinese(question)
    if asked.startswith("zh"):
        return True
    if asked.startswith("en"):
        return False
    ctx.stop("lang must be zh, en or auto.")
    return False


def _sources(passages: list[dict[str, Any]], claims: list[dict[str, Any]], zh: bool) -> list[dict[str, Any]]:
    """Claim number -> file, paragraph and the exact words quoted, for every claim given."""
    by_n = {p["n"]: p for p in passages}
    out = []
    for claim in claims:
        p = by_n.get(claim["n"])
        if p is None:
            continue
        hit = quotes.paragraph_of(p, claim["start"])
        numbers = [hit] if hit else list(range(p["paragraph"], p["paragraph_end"] + 1))
        out.append({"n": claim["id"], "passage": p["n"], "path": p["path"], "paragraphs": numbers,
                    "label": gather.label(p["shown"], numbers, zh), "quote": claim["quote"]})
    return out


# ---------------------------------------------------------------- the pipeline


def run(ctx: Any) -> dict[str, Any]:
    question = " ".join(str(ctx.inputs.get("question") or ctx.inputs.get("topic") or "").split())
    if not question:
        ctx.stop("Give a question or a topic: --input question=\"...\"")
    if len(question) > MAX_QUESTION_CHARS:
        ctx.stop(f"The question is too long (over {MAX_QUESTION_CHARS} characters).")
    if not ctx.workspace.roots():
        ctx.stop("Name at least one folder of your own material to read: --folder PATH")
    length = str(ctx.inputs.get("length", "") or "short").strip().lower()
    if length not in report.LENGTHS:
        ctx.stop("length must be short or medium.")
    zh = _language(ctx, question)
    outline = plan.hints(ctx.inputs.get("outline"))
    usage = _Usage(ctx)
    start = ctx.llm.remaining()
    ctx.event("budget", requests=int(start["requests"]), tokens=int(start["tokens"]), paid_usd=float(start["paid_usd"]))
    synth_tokens = report.LENGTHS[length]["max_tokens"] + 3000  # kept back for the report

    state: dict[str, Any] = {
        "question": question, "lang": "zh" if zh else "en", "length": length, "subs": [], "plan": "rules", "claims": [],
        "passages": [], "unread": [], "not_read": {}, "dropped": 0, "dropped_why": {}, "usage": usage.steps,
        "report_empty": False,
        "retrieval": {"mode": "none", "files": 0, "chunks": 0, "passages": 0},
        "budget": {"requests": int(start["requests"]), "tokens": int(start["tokens"]), "paid_usd": float(start["paid_usd"])},
    }

    # ---- 1. plan (light). The job does not depend on it: anything unusable falls back to rules.
    subs: list[str] = []
    if start["requests"] >= 4:
        try:
            reply = usage.ask("plan", plan.messages(question, outline), task="light", max_tokens=plan.MAX_OUTPUT_TOKENS,
                              keep_requests=3)
            subs = plan.parse(reply.text)
            ctx.event("plan", read="model" if subs else ("empty" if getattr(reply, "unanswered", False) else "unusable"),
                      subs=len(subs), empty=bool(getattr(reply, "unanswered", False)))
        except (ctx.llm.Deferred, ctx.llm.BudgetExceeded, ctx.llm.RouterError) as exc:
            ctx.event("plan", read=type(exc).__name__, subs=0)
    if subs:
        state["plan"] = "model"
    else:
        subs = plan.by_rules(question, outline, zh)
        ctx.event("plan_rules", subs=len(subs))
    state["subs"] = subs

    # ---- 2. gather (no model; embeddings when the router has them and the budget has room)
    found = gather.gather(ctx, question, subs, zh, keep_requests=len(subs) + SYNTH_REQUESTS, keep_tokens=synth_tokens + 12_000)
    usage.steps["gather"].update(requests=found.embed.get("requests", 0), tokens=found.embed.get("tokens", 0),
                                 cost_usd=round(found.embed.get("cost_usd", 0.0), 6), providers=list(found.embed.get("providers", [])))
    state["passages"] = found.passages
    state["retrieval"] = {"mode": found.mode, "files": found.files, "chunks": found.chunks, "passages": len(found.passages)}
    if not found.passages:
        ctx.note("No passage in the folders you named is about this, so no report was written.")
        return _output(state, "not_found", "", zh)

    # ---- 3. extract (light, many small calls). Each claim is checked at once; a failing one is dropped.
    by_n = {p["n"]: p for p in found.passages}
    seen: dict[tuple[int, int, str], dict[str, Any]] = {}
    stopped = False
    for number, sub in enumerate(subs, start=1):
        mine = 0
        read_any = False
        for batch in extract.batches(found.by_sub.get(number, [])):
            if stopped or mine >= extract.MAX_PER_CALL * 2 or len(state["claims"]) >= MAX_CLAIMS:
                break
            left = ctx.llm.remaining()
            if left["requests"] <= SYNTH_REQUESTS or left["tokens"] < synth_tokens:
                stopped = True
                break
            try:
                reply = usage.ask("extract", extract.messages(question, sub, [by_n[n] for n in batch], zh), task="light",
                                  max_tokens=extract.MAX_OUTPUT_TOKENS, keep_requests=SYNTH_REQUESTS)
            except ctx.llm.BudgetExceeded:
                stopped = True
                break
            if getattr(reply, "unanswered", False):
                ctx.event("extract", sub=number, read="empty", kept=0, dropped=0, empty=True, recovered=False,
                          unanswered=True, stopped=reply.stopped)
                if reply.stopped == "budget":  # no room to ask again: the same as running out before the call
                    stopped = True
                    break
                state["not_read"].setdefault(number, []).extend(batch)  # NOT "nothing found": nothing was read
                continue
            read_any = True
            kept, dropped, how, why = extract.read(reply.text, {n: by_n[n]["text"] for n in batch}, context=question)
            state["dropped"] += dropped
            for code, count in why.items():
                state["dropped_why"][code] = state["dropped_why"].get(code, 0) + count
            for claim in kept:
                key = (claim["n"], claim["start"], " ".join(claim["says"].casefold().split()))
                mine += 1
                if key in seen:  # the same fact answers a second sub-question: one claim, both sub-questions
                    if number not in seen[key]["subs"]:
                        seen[key]["subs"].append(number)
                    continue
                seen[key] = {"id": len(state["claims"]) + 1, "sub": number, "subs": [number], **claim}
                state["claims"].append(seen[key])
            ctx.event("extract", sub=number, read=how, kept=len(kept), dropped=dropped, empty=False,
                      recovered=int(getattr(reply, "attempts", 1) or 1) > 1, unanswered=False)
            ctx.keep(_output(state, "claims", "", zh))  # what the person still gets if a later call is held back
        if stopped and not read_any and found.by_sub.get(number) and number not in state["not_read"]:
            state["unread"].append(number)
    if state["unread"]:
        ctx.note(f"The budget for this job ran out before {len(state['unread'])} of {len(subs)} sub-questions were read.",
                 incomplete=True)
    if state["not_read"]:
        passages = sum(len(set(v)) for v in state["not_read"].values())
        ctx.note(f"The model returned no text for {passages} passage(s), also when asked again with more room, so "
                 f"{len(state['not_read'])} of {len(subs)} sub-questions are not finished. That is not the same as "
                 "the files not answering them.", incomplete=True)
    if not state["claims"]:
        if state["unread"] or state["not_read"]:
            return _output(state, "claims", "", zh)
        ctx.note("The passages found do not answer this, so no report was written.")
        return _output(state, "not_found", "", zh)

    # ---- 4. synthesise (heavy, one call, hand-off suggested). The model sees claims and claim numbers only.
    ctx.keep(_output(state, "claims", "", zh))
    reply = usage.ask("synthesise", report.messages(question, subs, state["claims"], length, zh), task="heavy",
                      max_tokens=report.LENGTHS[length]["max_tokens"], handoff=True)
    if getattr(reply, "unanswered", False):
        ctx.event("synthesise", empty=True, handoff=bool(reply.handoff), size=0, unanswered=True)
        state["report_empty"] = True
        ctx.note("The model returned no text for the written report, also when asked again; the verified claims are "
                 "given instead.", incomplete=True)
        return _output(state, "claims", "", zh)
    body = report.clean_body(reply.text)
    ctx.event("synthesise", empty=False, handoff=bool(reply.handoff), size=weight(body),
              recovered=int(getattr(reply, "attempts", 1) or 1) > 1)
    output = _output(state, "report", body, zh)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    name = f"report-{stamp}-{hashlib.sha256(question.encode('utf-8')).hexdigest()[:6]}.md"
    ctx.workspace.propose_write(name, output["text"], "the research report as a Markdown file")
    return output


def _output(state: dict[str, Any], stage: str, body: str, zh: bool) -> dict[str, Any]:
    """The result as data. stage: report | claims (the report is not written yet) | not_found."""
    words = report.WORDS[zh]
    subs: list[str] = state["subs"]
    claims: list[dict[str, Any]] = [dict(c, subs=list(c["subs"])) for c in state["claims"]]
    passages: list[dict[str, Any]] = state["passages"]
    answered = {n for c in claims for n in c["subs"]}
    unread = list(state["unread"])
    not_read = {int(k): sorted(set(v)) for k, v in state["not_read"].items()}
    unfinished = sorted(not_read)
    unanswered = [n for n in range(1, len(subs) + 1) if n not in answered and n not in unread and n not in not_read]
    not_done = [words["unread"] + subs[n - 1] for n in unread] + [words["no_text"] + subs[n - 1] for n in unfinished]
    repaired = 0
    if stage == "claims":
        not_done.insert(0, words["no_report_text"] if state.get("report_empty") else words["the_report"])
        body = words["report_missing"] + "\n\n" + report.claim_lines(subs, claims)
    elif stage == "report":
        found = report.body_sentences(body)
        for n in sorted(answered):  # a sub-question the model left out: its verified claims are added as they are
            if not report.covers([c for c in claims if n in c["subs"]], found):
                body += "\n\n" + report.claim_lines(subs, claims, only=n)
                repaired += 1
    else:
        body = words["nothing"]
    cited = set(report.cites_of(body))
    if stage == "report":
        sources = _sources(passages, [c for c in claims if c["id"] in cited], zh)
    elif stage == "claims":
        sources = _sources(passages, claims, zh)
    else:
        sources = []
    labels = {s["n"]: s["label"] for s in sources}
    for claim in claims:
        claim["sources"] = [labels[claim["id"]]] if claim["id"] in labels else []
    text = report.assemble(state["question"], body, [subs[n - 1] for n in unanswered] if stage != "not_found" else subs,
                           not_done, sources, zh)

    def state_of(n: int) -> str:
        if n in not_read:
            return "not_finished"
        if n in unread:
            return "unread"
        return "answered" if n in answered else "unanswered"

    out = {
        "stage": stage, "text": text, "report": body if stage == "report" else "", "question": state["question"],
        "lang": state["lang"], "length": state["length"], "not_found": stage == "not_found",
        "subquestions": [{"n": n, "text": s, "claims": sum(1 for c in claims if n in c["subs"]), "state": state_of(n),
                          "not_read": not_read.get(n, [])}
                         for n, s in enumerate(subs, start=1)],
        "claims": claims, "sources": sources, "passages": passages, "unanswered": unanswered, "not_done": not_done,
        "not_read": sorted({p for v in not_read.values() for p in v}),
        "plan": state["plan"], "retrieval": state["retrieval"], "dropped_claims": state["dropped"],
        "dropped_why": dict(sorted(state["dropped_why"].items())), "repaired_sections": repaired,
        "usage": state["usage"], "budget": state["budget"],
    }
    if stage == "claims" and claims:
        out["handoff"] = {"prompt": report.handoff_prompt(state["question"], subs, claims, sources, zh), "claims": len(claims)}
    return out


# ---------------------------------------------------------------- the check


def check(output: Any, ctx: Any = None) -> list[str]:
    """Reasons the result may not be delivered as checked; empty when it passes.

    Reasons name claims, sentences, sources and sub-questions by number only: never their text.
    """
    if not isinstance(output, dict) or output.get("stage") not in ("report", "claims", "not_found"):
        return ["the skill returned no report"]
    passages = {p["n"]: p["text"] for p in output.get("passages", [])}
    claims = output.get("claims", [])
    question = str(output.get("question", ""))
    reasons: list[str] = []
    if output.get("not_read"):
        reasons.append(f"{len(output['not_read'])} passage(s) were not read: the model returned no text for them")
    quotes_by_id: dict[int, str] = {}
    for claim in claims:
        code, _, _ = quotes.verify(str(claim.get("quote", "")), claim.get("n"), str(claim.get("says", claim.get("text", ""))),
                                   passages, question)
        if code:
            reasons.append(f"claim {claim.get('id', '?')}: {_why(code)}")
        else:
            quotes_by_id[claim.get("id")] = str(claim.get("quote", ""))
    stage = output["stage"]
    if stage == "not_found":
        if claims:
            reasons.append("the result says nothing was found but still carries claims")
        return reasons[:MAX_REASONS]
    if stage == "claims":
        if not output.get("not_done"):
            reasons.append("the result is partial but does not say what was not done")
        return reasons[:MAX_REASONS]

    zh = output.get("lang") == "zh"
    parts = report.split_report(str(output.get("text", "")), zh)
    for code, sentence, claim_id in quotes.statement_problems(parts["sentences"], quotes_by_id, question):
        reasons.append(f"sentence {sentence}: {_why(code, claim_id)}")
    cited = {n for s in parts["sentences"] for n in s["cites"]}
    if not cited:
        reasons.append("the report makes no statement with a source")
    listed_ids = set(parts["source_ids"])
    known = {s.get("n") for s in output.get("sources", [])}
    for n in sorted(cited & set(quotes_by_id)):
        if n not in listed_ids or n not in known:
            reasons.append(f"source {n} is cited but is not in the Sources list")
    listed = "\n".join(parts["listed"])
    for sub in output.get("subquestions", []):
        mine = [c for c in claims if sub["n"] in (c.get("subs") or [c.get("sub")])]
        if not report.covers(mine, parts["sentences"]) and sub.get("text", "") not in listed:
            reasons.append(f"sub-question {sub['n']} is neither covered by the report nor listed as not answered")
    size = weight(parts["body"])
    most = report.LENGTHS.get(output.get("length"), report.LENGTHS["short"])["max_weight"]
    if size > most:
        reasons.append(f"the report is too long (about {size} tokens; at most {most} for this length)")
    if parts["body"].strip() and mostly_chinese(report._MARK.sub("", parts["body"])) != zh:
        reasons.append("the report is not in the language that was asked for")
    return reasons[:MAX_REASONS]


def _why(code: str, claim_id: int = 0) -> str:
    return {
        quotes.NO_PASSAGE: "cites a source that does not exist",
        quotes.NO_QUOTE: "cites no quote",
        quotes.TOO_SHORT: "its quote is too short to check",
        quotes.TOO_LONG: "its quote is too long",
        quotes.NOT_FOUND: "its quote is not in the passage it cites",
        quotes.NUMBER: "states a number that is not in the quote it cites",
        quotes.NO_SAYS: "says nothing that can be checked",
        quotes.UNCITED: "states a fact without a source",
        quotes.UNKNOWN_CLAIM: f"cites source {claim_id}, which does not exist",
    }.get(code, code)
