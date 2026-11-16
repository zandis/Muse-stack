"""The two failures of the first real run, reproduced with the fake router, and the quote rule that replaced word overlap.

1. A reasoning model on a `light` step returns HTTP 200 with no visible text (finish_reason "length"). The old
   research-report read that as "no claims", listed the sub-question under "what the sources did not answer" and
   passed its own check. Now the call is asked again with more room one tier up; when it stays empty the passages
   are NOT READ, the sub-question is "not finished", and the check does not pass.
2. The word-overlap citation check could not work across languages. A claim is now an exact quote plus what it
   says; the quote must be in its passage and every number stated must be in the quote.

No network and no real provider.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from wardmuse.skills import llm, loader, runner
from queue_fakes import ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

QUESTION = "What are the money terms and the end date of the lease?"
PLAN = ["What is the monthly rent and when is it due?", "What does the lease say about the deposit?",
        "When does the lease end and what notice is needed?"]
FACTS = [("The monthly rent is 18,500 dollars, due on the 5th of each month", "The monthly rent is 18,500 dollars, due on the 5th."),
         ("The deposit equals two months of rent", "The deposit equals two months of rent."),
         ("The lease ends on 30 June 2032 and needs 60 days of notice", "The lease ends on 30 June 2032 with 60 days of notice.")]


def module(name: str, skill: str = "files-answer"):
    return loader.import_skill(loader.find(skill), name)


@pytest.fixture()
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "lease.md").write_text(
        "# Lease\n\nThe flat is at 12 Harbour Street and the tenant is Odalys Fenwick.\n\n"
        "The monthly rent is 18,500 dollars, due on the 5th of each month.\n\n"
        "The deposit equals two months of rent and is returned within 30 days of moving out.\n\n"
        "The lease ends on 30 June 2032 and needs 60 days of notice.\n", encoding="utf-8")
    (folder / "garden.txt").write_text("Tomatoes need six hours of sun.\n\nWater the basil every morning.\n", encoding="utf-8")
    return folder


def passages_in(request: dict) -> dict[int, str]:
    user = request["messages"][1]["content"].split("\n\nQuestion:")[0]
    return {int(n): text for n, text in re.findall(r"^\[(\d+)\] (.*?)(?=^\[\d+\] |\Z)", user, flags=re.S | re.M)}


def empty_length(task: str = "light") -> tuple:
    """What a reasoning model sends when its hidden reasoning used the whole allowance (an old router adds no header)."""
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
                       "usage": {"prompt_tokens": 400, "completion_tokens": 300,
                                 "completion_tokens_details": {"reasoning_tokens": 300}}}).encode()
    status, headers, _ = chat("", task=task)
    return status, headers, body


def claims_for(request: dict) -> list[dict]:
    """Both shapes in one object, so the same fake serves the old skills ({text, cites}) and the new ({quote, n, says})."""
    return [{"quote": quote, "n": n, "says": says, "text": says, "cites": [n]}
            for n, text in passages_in(request).items() for quote, says in FACTS if quote in text]


def faithful(request: dict) -> str:
    lines = [line[2:] for line in request["messages"][1]["content"].splitlines() if line.startswith("- ")]
    return "## Lease\n" + " ".join(re.sub(r"\s*((?:\[\d+\])+)$", r" \1", line.rstrip()).replace(". [", " [") + "."
                                   for line in lines)


def scripted(proxy, *, empty_every: int = 0, stay_empty: bool = False, first: str = "everyday") -> dict:
    """Plan, extract, synthesise. `empty_every=3`: every third first-try extract call (task `first`, the extract
    tier) is empty. `stay_empty`: the larger retry one tier up is empty too."""
    seen = {"light": 0, "empty": 0}

    def respond(request: dict):
        if request["path"].endswith("/embeddings"):
            return None
        system, task = request["messages"][0]["content"], request["task"]
        if system.startswith("You plan"):
            return chat(json.dumps({"subquestions": PLAN}), task=task)
        if system.startswith("You read"):
            if task == first:
                seen["light"] += 1
                if empty_every and (seen["light"] - 1) % empty_every == 0:
                    seen["empty"] += 1
                    return empty_length(task)
            elif stay_empty:
                seen["empty"] += 1
                return empty_length(task)
            return chat(json.dumps({"claims": claims_for(request)}), task=task)
        return chat(faithful(request), task=task)

    proxy.responder = respond
    return seen


def research(env, folder: Path, **kwargs):
    return runner.run("research-report", {"question": QUESTION, "embeddings": "off"}, folders=[folder], environ=env, **kwargs)


def chats(proxy) -> list[dict]:
    return [r for r in proxy.requests if r["path"].endswith("/chat/completions")]


# ================================================================ 1. empty answers


def test_a_third_of_extract_calls_empty_is_recovered_and_counted(env, proxy, docs):
    seen = scripted(proxy, empty_every=3)
    result = research(env, docs)
    o = result.output
    assert seen["empty"] >= 1
    assert result.status == "done" and result.check["status"] == "passed", (result.notes, result.check)
    assert o["unanswered"] == [] and o["not_read"] == [] and "did not answer" not in o["text"]
    for needle in ("18,500", "two months", "30 June 2032"):
        assert needle in o["text"], needle
    extract = o["usage"]["extract"]
    assert extract["recovered"] == seen["empty"] and extract["unanswered"] == 0 and extract["empty"] == seen["empty"]
    # The recovery calls: one tier up, four times the room, and counted against the job's budget like any call.
    retries = [r for r in chats(proxy) if r["messages"][0]["content"].startswith("You read") and r["task"] == "heavy"]
    assert len(retries) == seen["empty"] and all(r["max_tokens"] == 4096 for r in retries)  # four times 1200, bounded
    assert sum(u["requests"] for u in o["usage"].values()) == result.used["requests"] == len(chats(proxy))
    assert result.used["empty_answers"] == seen["empty"]


def test_answers_that_stay_empty_are_not_read_never_did_not_answer(env, proxy, docs):
    scripted(proxy, empty_every=3, stay_empty=True)
    result = research(env, docs)
    o = result.output
    assert o["not_read"], "the empty batches' passages are marked not read"
    assert result.status != "done" and result.check["status"] == "failed"
    assert any("were not read" in r for r in result.check["reasons"])
    unfinished = [s for s in o["subquestions"] if s["state"] == "not_finished"]
    assert unfinished and all(s["n"] not in o["unanswered"] for s in unfinished)
    assert all(any(s["text"] in line and line.startswith("not finished") for line in o["not_done"]) for s in unfinished)
    did_not_answer = o["text"].split("## What the sources did not answer")[1].split("## ")[0] if "did not answer" in o["text"] else ""
    assert all(s["text"] not in did_not_answer for s in unfinished)
    assert any("returned no text" in note for note in result.notes)
    assert o["usage"]["extract"]["unanswered"] >= 1


def test_the_retry_is_not_sent_when_it_would_eat_the_request_kept_for_the_report(env, proxy, docs):
    scripted(proxy, empty_every=1, stay_empty=True)  # every extract call is empty
    result = research(env, docs, budget={"requests": 2})  # plan skipped; one extract, then only the report's request is left
    sent = chats(proxy)
    assert [r["task"] for r in sent] == ["everyday"] and result.used["requests"] == 1
    o = result.output
    assert o["not_read"] == [] and [s["state"] for s in o["subquestions"]] == ["unanswered", "unread"]
    assert o["usage"]["extract"]["unanswered"] == 1 and result.status == "partial"
    assert any("budget for this job ran out" in note for note in result.notes)


def test_the_client_helper_and_the_skill_helper_follow_the_same_rule(env, proxy):
    recover = module("recover")
    assert (recover.FACTOR, recover.MOST, recover.ORDER) == (llm.RECOVERY_FACTOR, llm.RECOVERY_MAX_TOKENS, llm.RAISE_ORDER)
    meter = llm.Meter(llm.Budget(requests=7, tokens=100_000, seconds=60, paid_usd=0))
    client = llm.Client(proxy.url, "t", meter, default_task="light")
    proxy.push(*empty_length())
    proxy.push(*chat("ok", task="everyday"))
    got = client.ask_with_recovery([{"role": "user", "content": "x"}], task="light", max_tokens=300)
    assert got.text == "ok" and got.attempts == 2 and got.recovered
    assert [r["task"] for r in proxy.requests[-2:]] == ["light", "everyday"] and proxy.requests[-1]["max_tokens"] == 1200
    proxy.push(*empty_length())
    proxy.push(*empty_length("everyday"))
    lost = client.ask_with_recovery([{"role": "user", "content": "x"}], task="light", max_tokens=300)
    assert isinstance(lost, llm.Unanswered) and lost.empty and lost.reason == "reasoning" and lost.attempts == 2
    assert meter.requests == 4 and meter.empty_answers == 3
    proxy.push(*chat("", task="light", **{"x-muse-answer": "empty-reasoning"}))
    proxy.push(*chat("fine", task="everyday"))
    got = recover.ask(client, [{"role": "user", "content": "x"}], task="light", max_tokens=300)
    assert got.text == "fine" and got.attempts == 2
    proxy.push(*chat("   ", task="light"))  # blank, no sign of reasoning; one request left: no room to ask again
    lost = recover.ask(client, [{"role": "user", "content": "x"}], task="light", max_tokens=300)
    assert lost.unanswered and lost.reason == "blank" and lost.stopped == "budget" and meter.requests == 7
    assert recover.empty_reason(llm.Answer(text="", answer="")) == "blank"


# ================================================================ 2. the quote rule


PASSAGES = {1: "The monthly rent is 18,500 dollars, due on the 5th of each month.\n\nThe deposit equals two months of rent.",
            2: "Invoice INV-3109 is due on 3 April 2031. Late payment costs 2% a month.",
            3: "第三季的營收達到 4.2 億元，比去年同期成長 12%。\n\n會議在上午8點40分開始。",
            4: "Ｔｈｅ ｂｕｓ ｌｅａｖｅｓ ａｔ ０８：４０ from Gate ７."}


@pytest.mark.parametrize("quote,n,says,code", [
    ("The monthly rent is 18,500 dollars", 1, "Rent: 18,500 dollars a month.", ""),  # exact
    ("the  monthly\nrent is 18,500   dollars", 1, "Rent is 18,500.", ""),  # case and white space
    ("The monthly rent is 18,500 dollars due on the 5th", 1, "Due on the 5th.", ""),  # a comma left out
    ("“The deposit equals two months of rent”", 1, "The deposit is two months of rent.", ""),  # curly quotes around it
    ("Invoice INV–3109 is due on 3 April 2031", 2, "INV-3109 is due on 2031-04-03.", ""),  # an en dash for a hyphen
    ("The bus leaves at 08:40", 4, "The bus leaves at 8:40 am.", ""),  # full-width letters and digits in the passage
    ("bus leaves at ０８:４０ from Gate ７", 4, "Gate 7 at 08:40.", ""),
    ("The monthly rent is 18,600 dollars", 1, "Rent is 18,600.", "quote-not-in-passage"),  # one digit changed
    ("The monthly rent is 185,00 dollars", 1, "Rent is 18,500.", "quote-not-in-passage"),  # digits moved
    ("The monthly rent is 18500 dollars", 1, "Rent is 18,500.", "quote-not-in-passage"),  # a separator is never relaxed
    ("The monthly rent is 18,500 dollars", 2, "Rent is 18,500.", "quote-not-in-passage"),  # the wrong passage
    ("The monthly rent is 18,500 dollars", 9, "Rent is 18,500.", "unknown-passage"),
    ("The monthly rent is 18,500 dollars", 1, "Rent is 18,500 and the deposit 37,000.", "number-not-in-quote"),
    ("Late payment costs 2% a month", 2, "Late payment costs 3% a month.", "number-not-in-quote"),
    ("rent", 1, "Rent.", "quote-too-short"),
    ("營收", 3, "營收。", "quote-too-short"),
    ("The monthly rent " * 30, 1, "Rent.", "quote-too-long"),
    ("The monthly rent is 18,500 dollars", 1, "", "says-nothing"),
    # Across languages: the quote stays as written, `says` is in the question's language.
    ("The monthly rent is 18,500 dollars, due on the 5th of each month", 1, "每月租金 18,500 元，每月5日繳納。", ""),
    ("Invoice INV-3109 is due on 3 April 2031", 2, "發票 INV-3109 的到期日是 2031年4月3日。", ""),
    ("Invoice INV-3109 is due on 3 April 2031", 2, "發票 INV-3109 在民國120年4月3日到期。", ""),
    ("Invoice INV-3109 is due on 3 April 2031", 2, "發票 INV-3109 在2031年4月5日到期。", "number-not-in-quote"),
    ("第三季的營收達到 4.2 億元，比去年同期成長 12%", 3, "Q3 revenue reached 4.2 億元, up 12% on a year earlier.", ""),
    ("第三季的營收達到 4.2 億元", 3, "Q3 revenue reached 42 億元.", "number-not-in-quote"),
    ("會議在上午8點40分開始", 3, "The meeting starts at 08:40.", ""),
])
def test_quote_verification(quote, n, says, code):
    quotes = module("quotes")
    got, start, end = quotes.verify(quote, n, says, PASSAGES)
    assert got == code
    if not code:
        assert 0 <= start < end <= len(PASSAGES[n])
        assert quotes.find(quote, PASSAGES[n]) == (start, end)


def test_kept_claims_carry_offsets_dedupe_and_remember_the_models_places():
    quotes = module("quotes")
    raw = [{"quote": "The deposit equals two months of rent", "n": "1", "says": "Deposit: two months."},
           {"quote": "made up words here", "n": 1, "says": "x"}, "junk",
           {"quote": "the deposit equals two months of rent", "n": [1], "says": "Deposit: two months."}]
    kept, dropped = quotes.keep(raw, PASSAGES)
    assert len(kept) == 1 and kept[0]["indexes"] == [1, 4] and dropped == {"quote-not-in-passage": 1, "not-a-claim": 1, "repeat": 1}
    c = kept[0]
    assert PASSAGES[1][c["start"]:c["end"]] == c["quote"] == "The deposit equals two months of rent"
    assert quotes.paragraph_of({"text": PASSAGES[1], "paragraphs": [4, 5]}, c["start"]) == 5


def test_sentences_must_cite_existing_claims_and_hold_only_quoted_numbers():
    quotes = module("quotes")
    found = quotes.sentences(["The rent is 18,500 dollars [1]. It is due on the 7th [1].", "Pets are fine for a fee.",
                              "## Next", "The deposit is two months of rent [4]."])
    problems = quotes.statement_problems(found, {1: "The monthly rent is 18,500 dollars, due on the 5th"}, context="")
    assert problems == [("number-not-in-quote", 2, 0), ("uncited", 3, 0), ("unknown-claim", 5, 4)]
    assert quotes.statement_problems(quotes.sentences(["Flat 4B pays 18,500 [1]."]), {1: "rent is 18,500 dollars"},
                                     context="What does flat 4B pay?") == []  # numbers from the question are allowed


# ================================================================ the copies, and no Muse import in a skill


SHARED = ("index.py", "pdf.py", "retrieve.py", "textwork.py", "answer.py", "quotes.py", "recover.py")
ROOT = Path(__file__).resolve().parents[1]


def test_shared_modules_are_byte_identical_and_import_nothing_from_muse():
    for name in SHARED:
        a, b = (ROOT / "skills/files-answer" / name).read_bytes(), (ROOT / "skills/research-report" / name).read_bytes()
        assert a == b, name
    for skill in ("files-answer", "research-report"):
        for path in (ROOT / "skills" / skill).glob("*.py"):
            assert not re.search(r"^\s*(from|import)\s+muse\b", path.read_text(encoding="utf-8"), re.M), path


# ================================================================ deferral during a recovery is still a deferral


def test_a_deferral_on_the_recovery_call_is_the_routers_choice_not_an_empty_answer(env, proxy, docs):
    def respond(request: dict):
        system, task = request["messages"][0]["content"], request["task"]
        if system.startswith("You plan"):
            return chat(json.dumps({"subquestions": PLAN}), task=task)
        if system.startswith("You read"):
            return empty_length(task) if task == "light" else ration_429("defer:reserve", 900, "wait:2026-11-14T00:00:00Z")
        return chat(faithful(request), task=task)

    proxy.responder = respond
    result = research(env, docs)
    assert result.status == "needs_choice" and result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}]
