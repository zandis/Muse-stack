"""A short research report from the person's own material, with a source behind every statement.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

Most of the work is light: only the last step needs a strong model, and that
is the step the router may hold back or offer to hand off.

  plan        light, 1 call    the question becomes 2 to 6 sub-questions (plan.py; rules when the reply is unusable)
  gather      no model         passages per sub-question from the named folders (gather.py; embeddings optional)
  extract     light, many      short claims citing passage numbers; a claim that fails the citation rule is
                               dropped at once (extract.py, verify.py)
  synthesise  heavy, 1 call    the report, written from the verified claims only (report.py), sent with
                               `x-muse-handoff: suggest`

Before the heavy call the verified claims are handed to the runner with
`ctx.keep(...)`. If the router defers or refuses that call, the person gets the
router's choices AND the claims by sub-question, with a prompt they can paste
into their own app. The same partial result is what a budget stop returns.

index.py, retrieve.py, textwork.py and answer.py are byte-for-byte copies of the
files in skills/files-answer: a skill is imported as a private package from its
own hashed bytes, so one skill cannot import another's modules. A test fails
when the copies drift.

Nothing from a file, a prompt or an answer goes into an event, a note or a check reason.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from . import extract, gather, plan, report, verify
from .textwork import mostly_chinese, overlap, weight

MAX_QUESTION_CHARS = 2000
SYNTH_REQUESTS = 1  # kept back for the report while passages are being read
MAX_CLAIMS = 36
MAX_REASONS = 12
STEPS = ("plan", "gather", "extract", "synthesise")
TIER = {"plan": "light", "gather": "embed", "extract": "light", "synthesise": "heavy"}


# ---------------------------------------------------------------- small helpers


class _Usage:
    """What each step used: requests, tokens, providers, cost. Counted from the job's own budget meter."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.steps: dict[str, dict[str, Any]] = {
            name: {"task": TIER[name], "requests": 0, "tokens": 0, "cost_usd": 0.0, "providers": []} for name in STEPS}

    def ask(self, step: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        row = self.steps[step]
        before = self.ctx.llm.remaining()
        try:
            reply = self.ctx.llm.ask(messages, **kwargs)
        finally:
            after = self.ctx.llm.remaining()
            row["requests"] += int(before["requests"] - after["requests"])
            row["tokens"] += max(0, int(before["tokens"] - after["tokens"]))
        row["cost_usd"] = round(row["cost_usd"] + (reply.cost_usd or 0.0), 6)
        row["task"] = reply.task or row["task"]
        if reply.provider and reply.provider not in row["providers"]:
            row["providers"].append(reply.provider)
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


def _paragraph(claim: str, passage: dict[str, Any]) -> int | None:
    """The paragraph of a cited passage that shares most with the claim."""
    best: tuple[int, float] = (0, 0.0)
    number = None
    for part, n in zip(passage["text"].split("\n\n"), passage["paragraphs"]):
        score = overlap(claim, part)
        if n and score > best:
            best, number = score, n
    return number


def _sources(passages: list[dict[str, Any]], claims: list[dict[str, Any]], cited: set[int], zh: bool) -> list[dict[str, Any]]:
    """Source number -> file and paragraph, for every source a claim or the report rests on."""
    by_n = {p["n"]: p for p in passages}
    used: dict[int, list[int]] = {}
    for claim in claims:
        for n in claim["cites"]:
            hit = _paragraph(claim["text"], by_n[n])
            used.setdefault(n, []).extend([hit] if hit else range(by_n[n]["paragraph"], by_n[n]["paragraph_end"] + 1))
    out = []
    for n in sorted(set(used) | (cited & set(by_n))):
        numbers = sorted(set(used.get(n) or range(by_n[n]["paragraph"], by_n[n]["paragraph_end"] + 1)))
        out.append({"n": n, "path": by_n[n]["path"], "paragraphs": numbers, "label": gather.label(by_n[n]["shown"], numbers, zh)})
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
        "passages": [], "unread": [], "dropped": 0, "usage": usage.steps,
        "retrieval": {"mode": "none", "files": 0, "chunks": 0, "passages": 0},
        "budget": {"requests": int(start["requests"]), "tokens": int(start["tokens"]), "paid_usd": float(start["paid_usd"])},
    }

    # ---- 1. plan (light). The job does not depend on it: anything unusable falls back to rules.
    subs: list[str] = []
    if start["requests"] >= 4:
        try:
            reply = usage.ask("plan", plan.messages(question, outline), task="light", max_tokens=plan.MAX_OUTPUT_TOKENS)
            subs = plan.parse(reply.text)
            ctx.event("plan", read="model" if subs else "unusable", subs=len(subs), empty=reply.answer == "empty-reasoning")
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
    seen: dict[str, dict[str, Any]] = {}
    stopped = False
    for number, sub in enumerate(subs, start=1):
        mine = 0
        read_any = False
        for batch in extract.batches(found.by_sub.get(number, [])):
            if stopped or mine >= extract.MAX_PER_SUB or len(state["claims"]) >= MAX_CLAIMS:
                break
            left = ctx.llm.remaining()
            if left["requests"] <= SYNTH_REQUESTS or left["tokens"] < synth_tokens:
                stopped = True
                break
            try:
                reply = usage.ask("extract", extract.messages(sub, [by_n[n] for n in batch]), task="light",
                                  max_tokens=extract.MAX_OUTPUT_TOKENS)
            except ctx.llm.BudgetExceeded:
                stopped = True
                break
            read_any = True
            kept, dropped, how = extract.read(reply.text, {n: by_n[n]["text"] for n in batch})
            state["dropped"] += dropped
            for claim in kept:
                key = " ".join(claim["text"].casefold().split())
                mine += 1
                if key in seen:  # the same fact answers a second sub-question: one claim, both sub-questions
                    if number not in seen[key]["subs"]:
                        seen[key]["subs"].append(number)
                    continue
                seen[key] = {"id": len(state["claims"]) + 1, "sub": number, "subs": [number], "text": claim["text"],
                             "cites": claim["cites"]}
                state["claims"].append(seen[key])
            ctx.event("extract", sub=number, read=how, kept=len(kept), dropped=dropped, empty=reply.answer == "empty-reasoning")
            ctx.keep(_output(state, "claims", "", zh))  # what the person still gets if a later call is held back
        if stopped and not read_any and found.by_sub.get(number):
            state["unread"].append(number)
    if state["unread"]:
        ctx.note(f"The budget for this job ran out before {len(state['unread'])} of {len(subs)} sub-questions were read.",
                 incomplete=True)
    if not state["claims"]:
        if state["unread"]:
            return _output(state, "claims", "", zh)
        ctx.note("The passages found do not answer this, so no report was written.")
        return _output(state, "not_found", "", zh)

    # ---- 4. synthesise (heavy, one call, hand-off suggested). The model sees claims and source numbers only.
    ctx.keep(_output(state, "claims", "", zh))
    reply = usage.ask("synthesise", report.messages(question, subs, state["claims"], length, zh), task="heavy",
                      max_tokens=report.LENGTHS[length]["max_tokens"], handoff=True)
    body = report.clean_body(reply.text)
    ctx.event("synthesise", empty=reply.answer == "empty-reasoning", handoff=bool(reply.handoff), size=weight(body))
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
    unanswered = [n for n in range(1, len(subs) + 1) if n not in answered and n not in unread]
    not_done = [words["unread"] + subs[n - 1] for n in unread]
    repaired = 0
    if stage == "claims":
        not_done.insert(0, words["the_report"])
        body = words["report_missing"] + "\n\n" + report.claim_lines(subs, claims)
    elif stage == "report":
        for n in sorted(answered):  # a sub-question the model left out: its verified claims are added as they are
            if not report.covers([c for c in claims if n in c["subs"]], report.body_sentences(body)):
                body += "\n\n" + report.claim_lines(subs, claims, only=n)
                repaired += 1
    else:
        body = words["nothing"]
    cited = set(report.cites_of(body))
    sources = _sources(passages, claims, cited, zh) if stage != "not_found" else []
    labels = {s["n"]: s["label"] for s in sources}
    for claim in claims:
        claim["sources"] = [labels[n] for n in claim["cites"] if n in labels]
    text = report.assemble(state["question"], body, [subs[n - 1] for n in unanswered] if stage != "not_found" else subs,
                           not_done, sources, zh)
    out = {
        "stage": stage, "text": text, "report": body if stage == "report" else "", "question": state["question"],
        "lang": state["lang"], "length": state["length"], "not_found": stage == "not_found",
        "subquestions": [{"n": n, "text": s, "claims": sum(1 for c in claims if n in c["subs"]),
                          "state": "unread" if n in unread else ("answered" if n in answered else "unanswered")}
                         for n, s in enumerate(subs, start=1)],
        "claims": claims, "sources": sources, "passages": passages, "unanswered": unanswered, "not_done": not_done,
        "plan": state["plan"], "retrieval": state["retrieval"], "dropped_claims": state["dropped"], "repaired_sections": repaired,
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
    reasons: list[str] = []
    for claim in claims:
        code, n = verify.supported(claim.get("text", ""), claim.get("cites") or [], passages)
        if code:
            reasons.append(f"claim {claim.get('id', '?')}: {_why(code, n)}")
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
    cited: set[int] = set()
    stated = 0
    for number, sentence in enumerate(parts["sentences"], start=1):
        if not sentence["cites"]:
            if sentence["fact"]:
                reasons.append(f"sentence {number} states a fact without a source")
            continue
        stated += 1
        cited.update(sentence["cites"])
        code, n = verify.supported(sentence["text"], sentence["cites"], passages)
        if code:
            reasons.append(f"sentence {number}: {_why(code, n)}")
    if not stated:
        reasons.append("the report makes no statement with a source")
    listed_ids = set(parts["source_ids"])
    known = {s.get("n") for s in output.get("sources", [])}
    for n in sorted(cited & set(passages)):
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


def _why(code: str, n: int) -> str:
    if code == verify.NO_CITE:
        return "cites no source"
    if code == verify.UNKNOWN:
        return f"cites source {n}, which does not exist"
    if code == verify.NOTHING:
        return "says nothing that can be checked"
    return f"shares too little with source {n}" if n else "shares too little with the sources it cites"
