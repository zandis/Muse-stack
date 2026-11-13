"""The shipped skill `research-report`: plan, gather, extract, synthesise, the check, and what happens when the heavy step is held back.

No network and no real provider: the router is queue_fakes.FakeProxy answering by step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from muse.skills import loader, runner
from queue_fakes import ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

CANARY = "CANARY-FILE-CONTENT-7a31be"
CANARY_REPLY = "CANARY-MODEL-REPLY-c09d44"
ROOT = Path(__file__).resolve().parents[1]
SHARED = ("index.py", "retrieve.py", "textwork.py", "answer.py", "quotes.py", "recover.py")


def module(name: str, skill: str = "research-report"):
    return loader.import_skill(loader.find(skill), name)


@pytest.fixture()
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "lease.md").write_text(
        "# Lease\n\nThe flat is at 12 Harbour Street and the tenant is Odalys Fenwick.\n\n"
        "The monthly rent is 18,500 dollars, due on the 5th of each month.\n\n"
        "The deposit equals two months of rent and is returned within 30 days of moving out.\n\n"
        f"The lease ends on 30 June 2032 and needs 60 days of notice. {CANARY}\n", encoding="utf-8")
    (folder / "garden.txt").write_text("Tomatoes need six hours of sun.\n\nWater the basil every morning.\n", encoding="utf-8")
    (folder / "report.md").write_text(
        "# 第三季報告\n\n第三季的營收達到 4.2 億元，比去年同期成長 12%。\n\n"
        "主要成長來自海外市場，尤其是日本與東南亞。\n\n下一季的重點是降低物流成本。\n", encoding="utf-8")
    return folder


# Facts a scripted "small model" finds: (the exact words it quotes from a passage, what the claim then says).
EN_FACTS = [
    ("The monthly rent is 18,500 dollars, due on the 5th of each month", "The monthly rent is 18,500 dollars, due on the 5th of each month."),
    ("The deposit equals two months of rent", "The deposit equals two months of rent."),
    ("The lease ends on 30 June 2032 and needs 60 days of notice", "The lease ends on 30 June 2032 and needs 60 days of notice."),
]
ZH_FACTS = [
    ("第三季的營收達到 4.2 億元，比去年同期成長 12%", "第三季的營收達到 4.2 億元，比去年同期成長 12%。"),
    ("主要成長來自海外市場", "主要成長來自海外市場，尤其是日本與東南亞。"),
    ("下一季的重點是降低物流成本", "下一季的重點是降低物流成本。"),
]


def quoted(request: dict, facts: list[tuple[str, str]] | None = None) -> list[dict]:
    """The quote claims a faithful small model makes for the passages of one extract request."""
    return [{"quote": quote, "n": n, "says": says} for n, text in passages_in(request).items()
            for quote, says in (facts or EN_FACTS) if quote in text]
EN_PLAN = ["What is the monthly rent and when is it due?", "What does the lease say about the deposit?",
           "When does the lease end and what notice is needed?"]
ZH_PLAN = ["第三季的營收是多少？", "成長主要來自哪些市場？", "下一季的重點是什麼？"]


def passages_in(request: dict) -> dict[int, str]:
    user = request["messages"][1]["content"].split("\n\nQuestion:")[0]
    return {int(n): text for n, text in re.findall(r"^\[(\d+)\] (.*?)(?=^\[\d+\] |\Z)", user, flags=re.S | re.M)}


def notes_in(request: dict) -> list[tuple[str, str, list[int]]]:
    """(sub-question, claim, source numbers) for every note line the heavy step was given."""
    out, heading = [], ""
    for line in request["messages"][1]["content"].splitlines():
        if line.startswith("## "):
            heading = line[3:]
        elif line.startswith("- "):
            out.append((heading, re.sub(r"\s*(\[\d+\])+$", "", line[2:]), [int(n) for n in re.findall(r"\[(\d+)\]", line)]))
    return out


def faithful(request: dict) -> str:
    """A report that restates each note under its heading, with its source."""
    parts: dict[str, list[str]] = {}
    for heading, claim, cites in notes_in(request):
        parts.setdefault(heading, []).append(f"{claim.rstrip('.。')} " + "".join(f"[{n}]" for n in cites) + ("。" if re.search("[一-鿿]", claim) else "."))
    return "\n\n".join(f"## {h}\n" + " ".join(lines) for h, lines in parts.items())


def script(proxy, plan: Any = None, facts: list[tuple[str, str]] | None = None, synth: Callable[[dict], Any] | None = None,
           extract: Callable[[dict], Any] | None = None) -> None:
    """Answer each request by the step it belongs to (told apart by the system prompt)."""

    def respond(request: dict):
        if request["path"].endswith("/embeddings"):
            return None
        system = request["messages"][0]["content"]
        task = request["task"]
        if system.startswith("You plan"):
            reply = plan if plan is not None else EN_PLAN
            return chat(reply if isinstance(reply, str) else json.dumps({"subquestions": reply}, ensure_ascii=False), task=task)
        if system.startswith("You read"):
            if extract is not None:
                made = extract(request)
                return made if isinstance(made, tuple) else chat(made, task=task)
            return chat(json.dumps({"claims": quoted(request, facts)}, ensure_ascii=False), task=task)
        made = (synth or faithful)(request)
        return made if isinstance(made, tuple) else chat(made, task=task)

    proxy.responder = respond


def research(env, question: str, folder: Path, budget: dict | None = None, **inputs):
    inputs.setdefault("embeddings", "off")
    return runner.run("research-report", {"question": question, **inputs}, folders=[folder], environ=env, budget=budget)


def chats(proxy) -> list[dict]:
    return [r for r in proxy.requests if r["path"].endswith("/chat/completions")]


def step_of(request: dict) -> str:
    system = request["messages"][0]["content"]
    return "plan" if system.startswith("You plan") else "extract" if system.startswith("You read") else "synthesise"


QUESTION = "What are the money terms and the end date of the lease?"


# ================================================================ the copies and the shared rule


def test_the_shared_modules_are_exact_copies_of_files_answer():
    for name in SHARED:
        assert (ROOT / "skills/research-report" / name).read_bytes() == (ROOT / "skills/files-answer" / name).read_bytes(), name


def test_the_quote_rule_is_the_same_in_both_checks():
    rr, fa = module("skill"), module("skill", "files-answer")
    passages = {1: "The monthly rent is 18,500 dollars, due on the 5th of each month.", 2: "Water the basil every morning.",
                3: "第三季的營收達到 4.2 億元，比去年同期成長 12%。"}
    cases = [("The monthly rent is 18,500 dollars", 1, "The rent is 18,500 dollars a month."),  # stands
             ("The monthly rent is 18,500 dollars", 2, "The rent is 18,500 dollars a month."),  # wrong passage
             ("The monthly rent is 19,500 dollars", 1, "The rent is 19,500 dollars a month."),  # a changed digit
             ("The monthly rent is 18,500 dollars", 1, "The rent is 18,900 dollars a month."),  # says a number not quoted
             ("第三季的營收達到 4.2 億元", 3, "Revenue in Q3 reached 4.2 億元."),  # zh quote, en statement
             ("營收", 3, "Revenue."), ("The monthly rent", 9, "The rent.")]
    for quote, n, says in cases:
        claim = {"id": 1, "quote": quote, "n": n, "says": says, "text": says, "sub": 1, "subs": [1], "cites": [n]}
        fa_out = {"question": "q", "answer": f"{says} [1]", "claims": [claim], "not_found": False,
                  "passages": [{"n": k, "text": t} for k, t in passages.items()]}
        rr_out = {"stage": "claims", "question": "q", "claims": [claim], "not_done": ["the written report"],
                  "passages": fa_out["passages"]}
        assert (fa.check(fa_out) == []) == (rr.check(rr_out) == []), (quote, n, says)
    assert [fa.check({"question": "q", "answer": f"{s} [1]", "claims": [{"id": 1, "quote": q, "n": n, "says": s}],
                      "passages": [{"n": k, "text": t} for k, t in passages.items()]}) == [] for q, n, s in cases] == [
        True, False, False, False, True, False, False]


# ================================================================ step 1: plan


def test_plan_reads_objects_lists_and_numbered_lines_and_refuses_the_rest():
    plan = module("plan")
    assert plan.parse('{"subquestions": ["What is the rent?", "When is it due?", "Who pays it?"]}') == [
        "What is the rent?", "When is it due?", "Who pays it?"]
    assert plan.parse('Sure!\n```json\n["What is the rent?", "When is it due?"]\n```') == ["What is the rent?", "When is it due?"]
    assert plan.parse("1. What is the rent?\n2) When is it due?\n- Who pays it?") == ["What is the rent?", "When is it due?", "Who pays it?"]
    assert plan.parse('{"questions": [{"question": "租金多少？"}, {"question": "何時到期？"}]}') == ["租金多少？", "何時到期？"]
    assert plan.parse('{"subquestions": ["a", "b", "c", "d", "e", "f", "g", "h"]}') == []  # one letter each: not questions
    assert len(plan.parse(json.dumps({"subquestions": [f"Question number {i}?" for i in range(9)]}))) == 6  # capped
    for unusable in ("", "I cannot help with that.", '{"subquestions": "none"}', '{"subquestions": ["only one"]}', "{{{", "[1, 2, 3]"):
        assert plan.parse(unusable) == [], unusable
    assert plan.parse('{"subquestions": ["Same one?", "same one?", "Other?"]}') == ["Same one?", "Other?"]  # repeats folded


def test_plan_by_rules_uses_hints_then_the_questions_own_parts_then_fixed_angles():
    plan = module("plan")
    assert plan.hints("rent; deposit\n- end date | ") == ["rent", "deposit", "end date"]
    assert plan.by_rules("The lease", ["rent", "deposit"], False) == ["The lease: rent", "The lease: deposit"]
    parts = plan.by_rules("What is the monthly rent of the flat and when does the lease of the flat end?", [], False)
    assert parts == ["What is the monthly rent of the flat", "when does the lease of the flat end"]
    angles = plan.by_rules("Lease terms", [], False)
    assert angles[0] == "Lease terms" and len(angles) == 3 and all(a.startswith("Lease terms") for a in angles)
    assert plan.by_rules("第三季的營收以及下一季的重點", [], True) == ["第三季的營收", "下一季的重點"]
    assert plan.by_rules("租約的租金、押金和滯納金？", [], True) == ["租約的租金", "押金", "滯納金"]
    assert plan.by_rules("Lease: rent, deposit and late fee?", [], False) == ["Lease: rent", "deposit", "late fee"]
    assert len(plan.by_rules("營收", [], True)) == 3


# ================================================================ the whole job


def test_end_to_end_report_with_tiers_handoff_header_usage_and_a_proposed_file(env, proxy, docs, tmp_path):
    script(proxy)
    out = tmp_path / "out"
    result = runner.run("research-report", {"question": QUESTION, "embeddings": "off"}, folders=[docs], environ=env,
                        out_dir=out, apply_writes=True)
    assert result.status == "done" and result.check == {"status": "passed", "reasons": []}, (result.notes, result.check)
    o = result.output
    assert o["stage"] == "report" and o["plan"] == "model" and not o["not_found"] and o["lang"] == "en"
    for needle in ("18,500", "two months", "30 June 2032", "## Sources", "lease.md › paragraph"):
        assert needle in o["text"], needle
    assert o["text"].startswith(f"# {QUESTION}\n")
    assert [s["state"] for s in o["subquestions"]] == ["answered"] * 3 and o["unanswered"] == [] and o["not_done"] == []
    assert all(c["sources"] and c["sources"][0].startswith("lease.md › paragraph") for c in o["claims"])
    assert {s["path"] for s in o["sources"]} == {str((docs / "lease.md").resolve())}

    # Tiers per step, and the hand-off suggestion on the heavy call only.
    sent = chats(proxy)
    steps = [step_of(r) for r in sent]
    assert steps[0] == "plan" and steps[-1] == "synthesise" and steps.count("synthesise") == 1 and steps.count("extract") == 3  # one call per sub-question
    assert sent[0]["task"] == "light" and all(r["task"] == "everyday" for r in sent[1:-1]) and sent[-1]["task"] == "heavy"
    assert sent[-1]["headers"].get("x-muse-handoff") == "suggest"
    assert all("x-muse-handoff" not in r["headers"] for r in sent[:-1])
    assert all(r["privacy"] == "standard" and "x-muse-allow-paid" not in r["headers"] for r in sent)
    # The heavy step sees claims and source numbers, never the documents again.
    heavy = sent[-1]["messages"][1]["content"]
    assert "18,500" in heavy and CANARY not in heavy and "Harbour Street" not in heavy
    assert sent[0]["max_tokens"] == 300 and all(r["max_tokens"] == 1200 for r in sent[1:-1]) and sent[-1]["max_tokens"] == 700

    # Per-step usage adds up to the job's.
    usage = o["usage"]
    assert usage["plan"]["requests"] == 1 and usage["synthesise"]["requests"] == 1 and usage["gather"]["requests"] == 0
    assert usage["extract"]["requests"] == steps.count("extract") > usage["plan"]["requests"]
    assert sum(u["requests"] for u in usage.values()) == result.used["requests"] == len(sent)
    assert sum(u["tokens"] for u in usage.values()) == result.used["tokens"]
    assert usage["extract"]["providers"] == ["freea"] and usage["synthesise"]["task"] == "heavy" and result.used["cost_usd"] == 0
    assert o["budget"] == {"requests": 40, "tokens": 150000, "paid_usd": 0.0}
    assert [e for e in result.events if e["kind"] == "skill.budget"][0]["requests"] == 40

    # The report file: proposed, and written because the check passed and the caller agreed.
    assert len(result.writes) == 1 and result.writes[0].applied
    written = list(out.iterdir())
    assert len(written) == 1 and re.fullmatch(r"report-\d{8}-\d{6}-[0-9a-f]{6}\.md", written[0].name)
    assert written[0].read_text(encoding="utf-8") == o["text"]


def test_unusable_plan_reply_falls_back_to_rules(env, proxy, docs):
    script(proxy, plan="I am not sure what you mean. " + CANARY_REPLY)
    result = research(env, "What is the monthly rent of the flat and when does the lease of the flat end?", docs)
    assert result.status == "done", (result.notes, result.check)
    assert result.output["plan"] == "rules"
    assert [s["text"] for s in result.output["subquestions"]] == ["What is the monthly rent of the flat", "when does the lease of the flat end"]
    kinds = [e["kind"] for e in result.events]
    assert "skill.plan_rules" in kinds and [e for e in result.events if e["kind"] == "skill.plan"][0]["read"] == "unusable"
    assert "18,500" in result.output["text"] and "30 June 2032" in result.output["text"]


def test_outline_hints_reach_the_planner_and_shape_the_rule_fallback(env, proxy, docs):
    script(proxy, plan="")
    result = research(env, "The lease", docs, outline="monthly rent; deposit")
    assert "- monthly rent\n- deposit" in chats(proxy)[0]["messages"][1]["content"]
    assert [s["text"] for s in result.output["subquestions"]] == ["The lease: monthly rent", "The lease: deposit"]
    assert result.status == "done" and "18,500" in result.output["text"]


def test_claims_that_fail_the_citation_rule_are_dropped_at_once(env, proxy, docs):
    def extract(request: dict) -> str:
        numbers = list(passages_in(request))
        good = quoted(request)
        bad = [{"quote": "The landlord owns three other buildings", "n": numbers[0],
                "says": "The landlord owns three other buildings in the city centre."},  # not in the passage
               {"quote": "The monthly rent is 18,500 dollars", "n": 99, "says": "The rent is 18,500 dollars."},  # not given
               {"text": "Pets are allowed with a fee.", "cites": []},  # the old shape: no quote
               {"quote": "The monthly rent is 18,500 dollars", "n": numbers[0], "says": "The rent is 18,900 dollars."}]  # a number
        return json.dumps({"claims": bad + good})

    script(proxy, extract=extract)
    result = research(env, QUESTION, docs)
    assert result.status == "done", (result.notes, result.check)
    texts = " ".join(c["text"] for c in result.output["claims"])
    assert "three other buildings" not in texts and "Pets" not in texts and "18,500" in texts
    assert result.output["dropped_claims"] >= 3
    assert "three other buildings" not in chats(proxy)[-1]["messages"][1]["content"]  # the heavy step never sees them
    assert sum(e["dropped"] for e in result.events if e["kind"] == "skill.extract") == result.output["dropped_claims"]


def test_heavy_step_deferred_gives_the_routers_options_the_claims_and_a_handoff_prompt(env, proxy, docs):
    script(proxy, synth=lambda request: ration_429("defer:reserve", 900, "wait:2026-11-14T00:00:00Z,handoff,pay:0.041000"))
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice"
    assert result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}, {"choice": "handoff"},
                              {"choice": "pay", "about_usd": 0.041}]
    o = result.output
    assert o["stage"] == "claims" and result.check == {"status": "passed", "reasons": []} and result.writes == []
    assert o["not_done"] == ["the written report"] and "Not done yet: the written report." in o["text"]
    assert len(o["claims"]) == 3 and all(c["text"] in o["text"] for c in o["claims"])
    assert [s["state"] for s in o["subquestions"]] == ["answered"] * 3
    prompt = o["handoff"]["prompt"]
    assert QUESTION in prompt and all(c["text"] in prompt for c in o["claims"]) and "lease.md › paragraph" in prompt
    assert CANARY not in prompt and "Harbour Street" not in prompt  # claims and sources, not the documents
    assert o["usage"]["synthesise"]["requests"] == 1 and o["usage"]["synthesise"]["providers"] == []
    assert chats(proxy)[-1]["task"] == "heavy" and step_of(chats(proxy)[-1]) == "synthesise"
    assert any("not free right now" in note for note in result.notes)


def test_heavy_step_deferred_with_only_a_wait_offers_only_the_wait(env, proxy, docs):
    script(proxy, synth=lambda request: ration_429("defer:pace", 300, "wait:2026-11-14T00:00:00Z"))
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice" and result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}]
    script(proxy, synth=lambda request: ration_429("defer:pace", 300, "none"))
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice" and result.options == [] and result.output["stage"] == "claims"


def test_light_step_deferred_mid_extract_keeps_the_claims_so_far(env, proxy, docs):
    calls = {"n": 0}

    def extract(request: dict):
        calls["n"] += 1
        if calls["n"] >= 2:
            return ration_429("defer:reserve", 600, "wait:2026-11-14T00:00:00Z")
        return json.dumps({"claims": quoted(request)})

    script(proxy, extract=extract)
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice" and result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}]
    assert result.output["stage"] == "claims" and result.output["claims"] and "synthesise" not in [step_of(r) for r in chats(proxy)]


def test_request_budget_stops_extraction_but_keeps_one_request_for_the_report(env, proxy, docs):
    script(proxy)
    result = research(env, QUESTION, docs, budget={"requests": 4})
    steps = [step_of(r) for r in chats(proxy)]
    assert steps == ["plan", "extract", "extract", "synthesise"] and result.used["requests"] == 4
    assert result.status == "partial" and result.check["status"] == "passed", (result.notes, result.check)
    o = result.output
    assert o["stage"] == "report" and "## Not done" in o["text"]
    unread = [s for s in o["subquestions"] if s["state"] == "unread"]
    assert unread and all(any(s["text"] in line for line in o["not_done"]) for s in unread)
    assert any("budget for this job ran out" in note for note in result.notes)


def test_time_budget_stop_mid_extract_returns_the_claims_as_a_partial_result(env, proxy, docs):
    script(proxy)
    clock = {"t": 1_800_000_000.0}
    proxy.on_request = lambda n: clock.__setitem__("t", clock["t"] + 100.0)  # every call takes 100 seconds
    result = runner.run("research-report", {"question": QUESTION, "embeddings": "off"}, folders=[docs], environ=env,
                        budget={"seconds": 250}, clock=lambda: clock["t"])
    assert [step_of(r) for r in chats(proxy)] == ["plan", "extract", "extract"]  # the fourth call would start too late
    assert result.status == "partial" and result.check["status"] == "passed", (result.notes, result.check)
    o = result.output
    assert o["stage"] == "claims" and o["claims"] and o["not_done"][0] == "the written report" and "handoff" in o
    assert result.writes == [] and any("budget of 250 seconds was used up" in note for note in result.notes)


def test_tiny_budget_skips_the_plan_call(env, proxy, docs):
    script(proxy)
    result = research(env, "What is the monthly rent of the flat and when does the lease of the flat end?", docs, budget={"requests": 3})
    assert [step_of(r) for r in chats(proxy)] == ["extract", "extract", "synthesise"] and result.output["plan"] == "rules"


def test_a_subquestion_the_sources_do_not_answer_is_listed(env, proxy, docs):
    script(proxy, plan=EN_PLAN + ["Which basil watering schedule suits winter?"],
           facts=EN_FACTS)  # nothing is extracted about the garden
    result = research(env, QUESTION, docs)
    assert result.status == "done", (result.notes, result.check)
    o = result.output
    assert o["unanswered"] == [4] and "## What the sources did not answer\n- Which basil watering schedule suits winter?" in o["text"]
    assert o["subquestions"][3]["state"] == "unanswered"


def test_a_subquestion_the_model_leaves_out_is_added_from_its_claims(env, proxy, docs):
    def synth(request: dict) -> str:
        heading, claim, cites = notes_in(request)[0]
        return f"## Rent\n{claim.rstrip('.')} [{cites[0]}]."

    def extract(request: dict) -> str:  # one fact per sub-question, so each sub-question stands on its own claim
        asked = request["messages"][1]["content"].split("Question: ")[1].split("\n")[0]
        return json.dumps({"claims": quoted(request, [EN_FACTS[EN_PLAN.index(asked)]])})

    script(proxy, synth=synth, extract=extract)
    result = research(env, QUESTION, docs)
    assert result.status == "done" and result.output["repaired_sections"] == 2, (result.notes, result.check)
    assert "30 June 2032" in result.output["text"] and "two months" in result.output["text"]


def test_nothing_in_the_files_means_no_report_and_no_heavy_call(env, proxy, docs):
    script(proxy, plan=["Which submarine cables reach Antarctica?", "Who operates Antarctic submarine cables?"])
    result = research(env, "Submarine cables reaching Antarctica", docs)
    assert result.status == "done" and result.check["status"] == "passed"
    o = result.output
    assert o["stage"] == "not_found" and o["not_found"] and o["claims"] == [] and o["sources"] == []
    assert [step_of(r) for r in chats(proxy)] == ["plan"] and result.writes == []
    # Passages are found but hold nothing that helps: still no heavy call.
    script(proxy, extract=lambda request: json.dumps({"claims": []}))
    result = research(env, QUESTION, docs)
    assert result.output["stage"] == "not_found" and "synthesise" not in [step_of(r) for r in chats(proxy)]
    assert "The files you named do not answer this." in result.output["text"]


def test_missing_inputs_stop_with_a_plain_sentence(env, proxy, docs):
    assert runner.run("research-report", {}, folders=[docs], environ=env).notes == ['Give a question or a topic: --input question="..."']
    assert runner.run("research-report", {"topic": "x"}, environ=env).status == "failed"
    assert "length must be" in research(env, "x", docs, length="epic").notes[0]
    assert "lang must be" in research(env, "x", docs, lang="fr").notes[0]
    assert chats(proxy) == []


def test_topic_is_accepted_and_medium_length_raises_the_output_cap(env, proxy, docs):
    script(proxy)
    result = runner.run("research-report", {"topic": QUESTION, "length": "medium", "embeddings": "off"}, folders=[docs], environ=env)
    assert result.status == "done" and chats(proxy)[-1]["max_tokens"] == 1400 and "400 to 600 words" in chats(proxy)[-1]["messages"][1]["content"]


# ================================================================ the check


def test_check_catches_an_uncited_factual_sentence_then_needs_a_choice(env, proxy, docs):
    def synth(request: dict) -> str:
        return faithful(request) + "\n\nThe landlord also charges a cleaning fee of 900 dollars at the end."

    script(proxy, synth=synth)
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice" and result.check["status"] == "failed"
    assert any(re.fullmatch(r"sentence \d+: states a fact without a source", r) for r in result.check["reasons"])
    assert result.output["stage"] == "report" and result.writes == []  # the draft is returned, marked, and nothing is written
    # One retry, one tier stronger for the light steps; the heavy step stays heavy.
    tasks = [(step_of(r), r["task"]) for r in chats(proxy)]
    assert ("plan", "light") in tasks and ("plan", "everyday") in tasks and ("extract", "everyday") in tasks
    assert [t for s, t in tasks if s == "synthesise"] == ["heavy", "heavy"]
    assert not any("cleaning fee" in note for note in result.notes)


def test_check_catches_a_wrong_and_a_missing_citation(env, proxy, docs):
    def synth(request: dict) -> str:
        notes = notes_in(request)
        wrong = next(n for n in range(1, 30) if n not in {c for _, _, cites in notes for c in cites})
        return (faithful(request) + f"\n\nThe monthly rent rose to 19,750 dollars last year [{notes[0][2][0]}]."
                f" The deposit equals two months of rent [{wrong + 40}].")

    script(proxy, synth=synth)
    result = research(env, QUESTION, docs)
    assert result.status == "needs_choice" and result.check["status"] == "failed"
    reasons = " | ".join(result.check["reasons"])
    assert re.search(r"sentence \d+: states a number that is not in the quote it cites", reasons)
    assert re.search(r"sentence \d+: cites source \d+, which does not exist", reasons)
    assert "19,750" not in reasons and "deposit" not in reasons  # reasons never quote the text


def good_output(env, proxy, docs) -> dict:
    script(proxy)
    result = research(env, QUESTION, docs)
    assert result.status == "done"
    return result.output


def test_check_directly_on_edited_outputs(env, proxy, docs):
    skill = module("skill")
    o = good_output(env, proxy, docs)
    assert skill.check(o) == []
    assert skill.check(None) == ["the skill returned no report"] and skill.check({"stage": "odd"}) == ["the skill returned no report"]

    def edited(**changes: Any) -> dict:
        return {**o, **changes}

    # The Sources list must map every cited number.
    cut = edited(text=o["text"].split("## Sources")[0])
    assert any("is cited but is not in the Sources list" in r for r in skill.check(cut))
    # A sub-question that is neither covered nor listed.
    extra = edited(subquestions=o["subquestions"] + [{"n": 4, "text": "What about parking?", "claims": 0, "state": "unanswered"}])
    assert "sub-question 4 is neither covered by the report nor listed as not answered" in skill.check(extra)
    # Too long for the length asked for.
    padded = edited(text=o["text"].replace("## Sources", ("The monthly rent is 18,500 dollars [1]. " * 400) + "\n\n## Sources"))
    assert any(r.startswith("the report is too long") for r in skill.check(padded))
    # The wrong language.
    assert "the report is not in the language that was asked for" in skill.check(edited(lang="zh"))
    # An empty report.
    empty = edited(text=f"# {QUESTION}\n\n## Sources\n")
    assert "the report makes no statement with a source" in skill.check(empty)
    # A claim in the table that no longer stands on its source.
    claims = [dict(o["claims"][0], quote="Tomatoes need six hours of sun every day."), *o["claims"][1:]]
    assert any(r.startswith("claim 1: its quote is not in the passage") for r in skill.check(edited(claims=claims)))
    # A passage that was not read is never a pass, even when everything else stands.
    assert any("were not read" in r for r in skill.check(edited(not_read=[2])))
    # Partial and not-found shapes.
    assert skill.check(edited(stage="claims", not_done=[])) == ["the result is partial but does not say what was not done"]
    assert skill.check(edited(stage="not_found")) == ["the result says nothing was found but still carries claims"]
    assert skill.check(edited(stage="not_found", claims=[])) == []


def test_sentence_reader_handles_markers_bullets_headings_and_connectives():
    report = module("report")
    lines = ["## Rent", "- The rent is 18,500 dollars a month [1]. It is due on the 5th. [2] In short:", "",
             "**Deposit**", "The deposit is two months 【3】【4】。Is that usual?", "Version 4.2 costs 1,200 dollars per seat [5, 6]"]
    got = report.sentences(lines[1:])
    assert [(s["cites"], s["fact"]) for s in got] == [([1], True), ([2], True), ([], False), ([3, 4], True), ([], False), ([5, 6], True)]
    assert got[5]["text"] == "Version 4.2 costs 1,200 dollars per seat"
    assert report.cites_of("a [1][2] b [2, 3]") == [1, 2, 3]
    body = report.clean_body("```markdown\n# My title\n\n## Rent\nText [1].\n\n## Sources\n[1] made up\n```")
    assert body == "## Rent\nText [1]."
    assert report.clean_body("Text [1].\n\nSources:\n[1] x") == "Text [1]."


# ================================================================ zh-TW


def test_zh_tw_end_to_end(env, proxy, docs):
    script(proxy, plan=ZH_PLAN, facts=ZH_FACTS)
    result = research(env, "第三季的營收、成長來源以及下一季的重點是什麼？", docs)
    assert result.status == "done" and result.check["status"] == "passed", (result.notes, result.check)
    o = result.output
    assert o["lang"] == "zh" and o["stage"] == "report"
    for needle in ("4.2 億元", "海外市場", "物流成本", "## 來源", "report.md › 第"):
        assert needle in o["text"], needle
    assert "Write in Traditional Chinese" in chats(proxy)[-1]["messages"][1]["content"]
    assert all(c["sources"][0].startswith("report.md › 第") for c in o["claims"])


def test_zh_tw_deferred_partial_and_rule_fallback(env, proxy, docs):
    script(proxy, plan="抱歉，我不確定。", facts=ZH_FACTS, synth=lambda request: ration_429("defer:reserve", 900, "wait:2026-11-14T00:00:00Z,handoff"))
    result = research(env, "第三季的營收以及下一季的重點", docs)
    assert result.status == "needs_choice" and result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}, {"choice": "handoff"}]
    o = result.output
    assert o["plan"] == "rules" and [s["text"] for s in o["subquestions"]] == ["第三季的營收", "下一季的重點"]
    assert "尚未完成：書面報告。" in o["text"] and o["not_done"] == ["書面報告"] and "4.2 億元" in o["text"]
    assert "請用繁體中文寫" in o["handoff"]["prompt"] and "4.2 億元" in o["handoff"]["prompt"]


def test_lang_input_overrides_the_questions_language(env, proxy, docs):
    script(proxy, plan=ZH_PLAN, facts=ZH_FACTS)
    result = research(env, "Q3 revenue 營收 and next quarter 重點", docs, lang="zh-TW")
    assert result.output["lang"] == "zh" and "## 來源" in result.output["text"]


# ================================================================ gather


def test_gather_dedupes_passages_caps_per_source_and_takes_another_source(env, proxy, tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    (folder / "big.md").write_text("\n\n".join(f"Section {i}. " + "The harbour ferry timetable changes every season. " * 40
                                               for i in range(8)), encoding="utf-8")
    (folder / "small.md").write_text("The harbour ferry costs 12 dollars.\n", encoding="utf-8")
    script(proxy, plan=["When does the harbour ferry timetable change?", "How much does the harbour ferry cost?"],
           extract=lambda request: json.dumps({"claims": []}))
    result = research(env, "The harbour ferry", folder)
    passages = result.output["passages"]
    gather = module("gather")
    assert len({p["n"] for p in passages}) == len(passages) <= gather.MAX_PASSAGES
    by_sub: dict[int, list[str]] = {}
    for p in passages:
        for sub in p["subs"]:
            by_sub.setdefault(sub, []).append(Path(p["path"]).name)
    assert all(names.count("big.md") <= gather.PER_SOURCE and len(names) <= gather.PER_SUB for names in by_sub.values())
    assert any("small.md" in names for names in by_sub.values())
    assert any(len(p["subs"]) > 1 for p in passages)  # one passage found for both sub-questions is one source

    # The interface a later web source plugs into: anything returning a Gathered.
    def other(ctx, question, subs, zh, keep_requests, keep_tokens):
        return gather.Gathered(passages=[{"n": 1, "text": "x"}], by_sub={1: [1]}, mode="lexical")

    assert gather.gather(None, "q", ["s"], False, 1, 1, source=other).passages == [{"n": 1, "text": "x"}]


def test_embeddings_are_used_when_the_router_has_them_and_counted_under_gather(env, proxy, docs):
    script(proxy)
    result = runner.run("research-report", {"question": QUESTION}, folders=[docs], environ=env)
    assert result.status == "done" and result.output["retrieval"]["mode"] == "hybrid"
    embeds = [r for r in proxy.requests if r["path"].endswith("/embeddings")]
    assert len(embeds) == 2 and result.output["usage"]["gather"]["requests"] == 2
    assert sum(u["requests"] for u in result.output["usage"].values()) == result.used["requests"]


# ================================================================ nothing leaks


def test_no_document_or_reply_text_in_events_notes_or_reasons(env, proxy, docs):
    seen: list[dict] = []

    def synth(request: dict) -> str:
        return faithful(request) + f"\n\nThe landlord also keeps a spare key in the office downstairs {CANARY_REPLY}."

    script(proxy, synth=synth)
    result = runner.run("research-report", {"question": QUESTION, "embeddings": "off"}, folders=[docs], environ=env,
                        on_event=seen.append)
    assert result.status == "needs_choice" and result.check["status"] == "failed"
    public = result.public()
    outside = json.dumps({k: v for k, v in public.items() if k != "output"}, ensure_ascii=False) + json.dumps(seen, ensure_ascii=False)
    for needle in (CANARY, CANARY_REPLY, "18,500", "Harbour Street", "Odalys", "spare key", "lease.md"):
        assert needle not in outside, needle
    assert CANARY_REPLY in result.output["text"]  # the draft itself is the person's content and is returned
    assert all(isinstance(v, (int, float, bool, str, list, type(None))) for e in seen for v in e.values())


# ================================================================ the runner's two small additions


def test_runner_returns_what_a_skill_kept_when_a_later_step_is_deferred(env, proxy):
    from test_skills_support import write_skill
    code = """
    def run(ctx):
        ctx.keep({"text": "GOOD so far"})
        ctx.llm.ask([{"role": "user", "content": "x"}], task="heavy", handoff=True)
        return {"text": "GOOD done"}


    def check(output, ctx):
        return [] if "GOOD" in output["text"] else ["bad"]
    """
    write_skill(Path(env["MUSE_HOME"]), "keeper", code)
    proxy.script.append(ration_429("defer:reserve", 600, "wait:2026-11-14T00:00:00Z,handoff"))
    result = runner.run("keeper", {}, environ=env)
    assert result.status == "needs_choice" and result.output == {"text": "GOOD so far"} and result.check["status"] == "passed"
    assert result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}, {"choice": "handoff"}]
    assert proxy.requests[-1]["headers"]["x-muse-handoff"] == "suggest"
    result = runner.run("keeper", {}, environ=env, budget={"requests": 0})
    assert result.status == "partial" and result.output == {"text": "GOOD so far"}
    result = runner.run("keeper", {}, environ=env)  # nothing held back: the kept output is not used
    assert result.status == "done" and result.output == {"text": "GOOD done"}
