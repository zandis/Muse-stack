"""scripts/chores_check.py: the task sets, the independent scorer, and the harness against a fake router.

No network and no real provider. The router is queue_fakes.FakeProxy; the skills are the shipped ones.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

import chores_check as cc
from queue_fakes import ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
CANARY_DOC = "CANARY-DOC-TEXT-e41f07"
CANARY_REPLY = "CANARY-REPLY-TEXT-9b2c55"
CANARY_QUESTION = "CANARY-QUESTION-3d77a0"
KINDS = {"lookup", "multi-hop", "numeric", "date", "not-found", "cross-lang", "long-file"}


# ================================================================ the shipped task sets


def test_every_shipped_task_file_parses_and_every_expected_fact_is_in_its_fixture():
    found, problems = cc.load_tasks(cc.TASKS_DIR)
    assert problems == []
    assert set(found) >= {"files-answer", "research-report"}
    for skill in ("files-answer", "research-report"):
        tasks = found[skill]
        assert len(tasks) >= 50, skill
        assert len({t["id"] for t in tasks}) == len(tasks)
        questions = [t["inputs"]["question"] for t in tasks]
        assert len(set(questions)) == len(questions), "tasks must be distinct"
        assert {t["kind"] for t in tasks} == KINDS
        assert {t["lang"] for t in tasks} == {"en", "zh"} and sum(t["lang"] == "zh" for t in tasks) >= 20
        for task in tasks:
            assert cc.task_problems(task, cc.TASKS_DIR, skill) == [] and cc.fixture_problems(task, cc.TASKS_DIR) == []
            if task["kind"] == "not-found":
                assert task["expect"] == {"not_found": True}
            else:
                facts = task["expect"]["facts"]
                assert facts and all(cc.sources_of(f) for f in facts), task["id"]  # every fact names the file to cite
            if task["kind"] == "multi-hop":
                assert len({n for f in task["expect"]["facts"] for n in cc.sources_of(f)}) >= 2, task["id"]


def test_fixture_documents_say_they_are_fictional_and_cover_the_kinds_of_document():
    for folder, word in (("larkspur-en", "fictional"), ("qingfeng-zh", "虛構")):
        files = sorted((cc.TASKS_DIR / "fixtures" / folder).iterdir())
        assert len(files) == 7 and sum(f.suffix == ".csv" for f in files) == 1
        for path in files:
            if path.suffix == ".md":
                assert word in path.read_text(encoding="utf-8").lower(), path.name
    long_file = (cc.TASKS_DIR / "fixtures/larkspur-en/residents-handbook.md").read_text(encoding="utf-8")
    assert len(long_file.split()) > 900


def test_only_skills_that_can_be_run_have_task_files():
    folders = {p.name for p in cc.TASKS_DIR.iterdir() if p.is_dir()}
    assert folders == {"fixtures", "files-answer", "research-report"}
    readme = (cc.TASKS_DIR / "README.md").read_text(encoding="utf-8")
    assert "setup-services" in readme and "recurring-chore" in readme and "tasks.jsonl" in readme


def test_task_problems_and_fixture_problems_catch_bad_definitions(tmp_path):
    (tmp_path / "fixtures" / "f").mkdir(parents=True)
    (tmp_path / "fixtures" / "f" / "a.md").write_text("The rent is 1,450 dollars.", encoding="utf-8")
    good = {"id": "x-1", "skill": "s", "kind": "lookup", "lang": "en", "folders": ["f"], "inputs": {"question": "q"},
            "expect": {"facts": [{"number": 1450, "source": "a.md"}]}}
    assert cc.task_problems(good, tmp_path, "s") == [] and cc.fixture_problems(good, tmp_path) == []

    def bad(**changes: Any) -> list[str]:
        return cc.task_problems({**good, **changes}, tmp_path, "s")

    assert any("skill" in p for p in bad(skill="other"))
    assert any("fixture folder not found" in p for p in bad(folders=["missing"]))
    assert any("folders" in p for p in bad(folders=["../f"]))
    assert any("inputs" in p for p in bad(inputs={}))
    assert any("needs `facts`" in p for p in bad(expect={"facts": []}))
    assert any("both" in p for p in bad(expect={"not_found": True, "facts": [{"number": 1}]}))
    assert any("exactly one" in p for p in bad(expect={"facts": [{"any": ["a"], "number": 1}]}))
    assert any("regex" in p for p in bad(expect={"facts": [{"regex": "("}]}))
    assert cc.task_problems("nope", tmp_path, "s") == ["not a JSON object"]
    wrong = {**good, "expect": {"facts": [{"number": 999, "source": "a.md"}, {"any": ["x"], "source": "b.md"}]}}
    assert cc.fixture_problems(wrong, tmp_path) == ["fact 1 is not in a.md", "fact 2: source file b.md is not in the task's folders"]
    # A bad line is reported with its place and is not run.
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "tasks.jsonl").write_text(json.dumps(good) + "\n{not json\n" + json.dumps(good) + "\n", encoding="utf-8")
    found, problems = cc.load_tasks(tmp_path)
    assert len(found["s"]) == 1 and problems == ["s/tasks.jsonl line 2: not JSON", "s/tasks.jsonl line 3: duplicate id x-1"]


# ================================================================ the scorer and the interval


def test_fact_matching_ignores_case_spacing_and_thousands_commas():
    assert cc.fact_in({"any": ["31 August 2033"]}, "It ends on 31  august 2033.")
    assert cc.fact_in({"any": ["2033 年 3 月 31 日"]}, "租期到2033年3月31日為止")
    assert cc.fact_in({"any": ["nope", "１，４５０"]}, "rent 1450")  # full-width forms and commas fold
    assert not cc.fact_in({"any": ["1,450"]}, "rent 14,500")
    assert cc.fact_in({"number": 1450}, "the rent is $1,450.00 a month") and cc.fact_in({"number": 97.65}, "97.65 dollars")
    assert not cc.fact_in({"number": 96}, "built in 1996") and not cc.fact_in({"number": 45}, "1,450")
    assert cc.fact_in({"regex": r"\b4\s*to\s*1\b"}, "voted 4 to 1") and not cc.fact_in({}, "anything")
    assert cc.numbers("1,450 and 2,900.50 and 3") == [1450.0, 2900.5, 3.0]


def test_score_needs_every_fact_and_the_right_file_cited():
    task = {"expect": {"facts": [{"number": 1450, "source": "lease.md"}, {"any": ["Corvid"], "source": ["notes.md", "handbook.md"]}]}}
    right = {"text": "Rent is 1,450. Corvid won.", "citations": [{"path": "/x/lease.md"}, {"path": "/x/handbook.md"}]}
    assert cc.score(task, right) == {"pass": True, "facts_found": 2, "facts": 2, "sources_ok": True, "not_found": False}
    assert cc.score(task, {**right, "text": "Rent is 1,450."})["pass"] is False  # a fact is missing
    wrong_file = {**right, "citations": [{"path": "/x/garden.md"}, {"path": "/x/notes.md"}]}
    assert cc.score(task, wrong_file) == {"pass": False, "facts_found": 2, "facts": 2, "sources_ok": False, "not_found": False}
    assert cc.score(task, {"text": "Rent is 1,450. Corvid won.", "sources": [{"path": "lease.md"}, {"path": "notes.md"}]})["pass"]
    assert cc.score(task, {"text": "Rent is 1,450. Corvid won."})["sources_ok"] is None  # a skill with no citation list
    assert cc.score(task, {**right, "not_found": True})["pass"] is False and cc.score(task, None)["pass"] is False
    nothing = {"expect": {"not_found": True}}
    assert cc.score(nothing, {"text": "not in the files", "not_found": True})["pass"] is True
    assert cc.score(nothing, {"text": "It is 12.", "not_found": False})["pass"] is False


def test_wilson_interval_maths():
    low, high = cc.wilson(40, 50)
    assert low == pytest.approx(0.6696, abs=5e-4) and high == pytest.approx(0.8876, abs=5e-4)
    low, high = cc.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.2775, abs=5e-4)
    low, high = cc.wilson(10, 10)
    assert low == pytest.approx(0.7225, abs=5e-4) and high == pytest.approx(1.0)
    low, high = cc.wilson(1, 2)
    assert low == pytest.approx(0.0945, abs=5e-4) and high == pytest.approx(0.9055, abs=5e-4)
    assert cc.wilson(0, 0) is None
    for n in (5, 50, 500):  # the interval narrows with more runs and always holds the observed rate
        low, high = cc.wilson(int(0.8 * n), n)
        assert low < 0.8 < high
    assert cc.wilson(400, 500)[1] - cc.wilson(400, 500)[0] < cc.wilson(40, 50)[1] - cc.wilson(40, 50)[0]


def test_reason_codes_never_carry_wording():
    assert cc.reason_code("claim 2 shares too little with passage 1") == "weak-overlap"
    assert cc.reason_code("sentence 4 states a fact without a source") == "uncited"
    assert cc.reason_code("claim 1 cites passage 9, which was not given") == "unknown-source"
    assert cc.reason_code("the report is not in the language that was asked for") == "language"
    assert cc.reason_code(f"something new {CANARY_REPLY}") == "other"


def test_sample_is_spread_over_kinds_and_repeatable():
    tasks = [{"id": f"t{i}", "kind": kind} for i, kind in enumerate(["a"] * 10 + ["b"] * 3 + ["c"] * 1)]
    one, again, other = cc.choose(tasks, 6, 7), cc.choose(tasks, 6, 7), cc.choose(tasks, 6, 8)
    assert one == again and len(one) == 6 and [t["kind"] for t in one].count("c") == 1 and [t["kind"] for t in one].count("b") == 2
    assert one != other and cc.choose(tasks, None, 0) == tasks and cc.choose(tasks, 99, 0) == tasks


# ================================================================ the harness against the fake router


LEASE = ("The flat is at 12 Harbour Street.\n\nThe monthly rent is 18,500 dollars, due on the 5th of each month.\n\n"
         f"The deposit equals two months of rent. {CANARY_DOC}\n")
TASKS = [
    ("t-right", "lookup", "How much is the monthly rent?", {"facts": [{"number": 18500, "source": "lease.md"}]}),
    ("t-wrong", "lookup", "What are the deposit terms of the flat?", {"facts": [{"any": ["12 Harbour Street"], "source": "lease.md"}]}),
    ("t-none", "not-found", "What is the door code?", {"not_found": True}),
    ("t-held", "numeric", "When is the rent due each month?", {"facts": [{"any": ["5th"], "source": "lease.md"}]}),
    ("t-bad", "date", f"Where is the flat? {CANARY_QUESTION}", {"facts": [{"any": ["12 Harbour Street"], "source": "lease.md"}]}),
]


def claim(text: str, cites: list[int], not_found: bool = False) -> str:
    """A model reply whose one claim quotes `text` from passage cites[0] (the quote rule decides whether it stands)."""
    return json.dumps({"answer": text + "".join(" [1]" for _ in cites[:1]),
                       "claims": [{"quote": text, "n": cites[0], "says": text}] if cites else [], "not_found": not_found})


ANSWERS: dict[str, Callable[[], Any]] = {
    "t-right": lambda: chat(claim("The monthly rent is 18,500 dollars.", [1])),
    "t-wrong": lambda: chat(claim("The deposit equals two months of rent.", [1])),  # passes its own check; half the answer is missing
    "t-none": lambda: chat(claim("", [], not_found=True)),
    "t-held": lambda: ration_429("defer:reserve", 3600, "wait:2026-11-14T00:00:00Z,handoff"),
    "t-bad": lambda: chat(claim(f"Tomatoes need six hours of sun {CANARY_REPLY}.", [1])),  # fails its own check, twice
}


@pytest.fixture()
def tasks_dir(tmp_path) -> Path:
    base = tmp_path / "chores"
    (base / "fixtures" / "home").mkdir(parents=True)
    (base / "fixtures" / "home" / "lease.md").write_text(LEASE, encoding="utf-8")
    for skill in ("files-answer", "ghost-skill"):
        (base / skill).mkdir()
        rows = [{"id": tid, "skill": skill, "kind": kind, "lang": "en", "folders": ["home"],
                 "inputs": {"question": question, "embeddings": "off"}, "expect": expect} for tid, kind, question, expect in TASKS]
        (base / skill / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return base


def route(proxy, answers: dict[str, Callable[[], Any]] | None = None) -> None:
    by_question = {question: tid for tid, _, question, _ in TASKS}
    table = {**ANSWERS, **(answers or {})}

    def respond(request: dict):
        if request["path"].endswith("/embeddings"):
            return None
        asked = request["messages"][1]["content"].split("Question: ")[1].split("\n\nJSON:")[0]
        return table[by_question[asked]]()

    proxy.responder = respond


def check(env, tasks_dir: Path, *argv: str) -> int:
    return cc.main(["--tasks-dir", str(tasks_dir), "--delay", "0", "--max-wait", "0", *argv], environ=env)


def report_of(env) -> dict:
    return json.loads((Path(env["MUSE_HOME"]) / "chores-report.json").read_text(encoding="utf-8"))


def test_plan_is_printed_and_nothing_is_sent_without_confirmation(env, proxy, tasks_dir, capsys, monkeypatch):
    route(proxy)
    assert check(env, tasks_dir) == cc.EXIT_USAGE  # not a terminal and no --yes
    out, err = capsys.readouterr()
    assert "Muse chores check: what this will send" in out and "Not a terminal: pass --yes to confirm." in err
    assert re.search(r"files-answer\s+5 runs \(5 tasks x 1\): at most 60 requests and 300000 tokens", out)
    assert "worst case in all: 5 runs, 60 requests, 300000 tokens" in out
    assert "Paid providers are not used; the run stops if anything is charged." in out
    assert "app: skills (never paid)   privacy: standard" in out
    assert re.search(r"ghost-skill\s+skipped: this skill is not installed here, so its 5 tasks were not run", out)
    assert "no document text, prompts or answers" in out
    assert proxy.requests == [] and not (Path(env["MUSE_HOME"]) / "chores-report.json").exists()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert check(env, tasks_dir) == cc.EXIT_OK
    assert "Nothing was sent." in capsys.readouterr().out and proxy.requests == []

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert check(env, tasks_dir, "--sample", "1") in (cc.EXIT_OK, cc.EXIT_NOT_MET)
    assert proxy.requests != []


def test_full_run_report_shape_deferred_accounting_and_no_leaks(env, proxy, tasks_dir, capsys):
    route(proxy)
    code = check(env, tasks_dir, "--yes")
    out = capsys.readouterr().out
    assert code == cc.EXIT_NOT_MET  # 3 of 4 completed runs passed their own check: under 80%
    report = report_of(env)
    assert report["tool"] == "chores_check" and report["privacy"] == "standard" and report["paid_usd"] == 0 and report["stopped"] is None
    assert report["skipped"] == [{"skill": "ghost-skill", "reason": "this skill is not installed here, so its 5 tasks were not run"}]
    assert set(report["skills"]) == {"files-answer"} and set(report["runs"]) == {f"files-answer/{tid}#1" for tid, *_ in TASKS}

    runs = {key.split("/")[1].split("#")[0]: value for key, value in report["runs"].items()}
    assert [runs[t]["outcome"] for t in ("t-right", "t-wrong", "t-none", "t-held", "t-bad")] == ["passed", "passed", "passed", "deferred", "failed"]
    assert runs["t-right"]["independent"] == {"pass": True, "facts_found": 1, "facts": 1, "sources_ok": True, "not_found": False}
    assert runs["t-wrong"]["own_check"] == "passed" and runs["t-wrong"]["independent"]["pass"] is False
    assert runs["t-none"]["independent"]["pass"] is True and runs["t-none"]["independent"]["not_found"] is True
    held = runs["t-held"]
    assert held["status"] == "needs_choice" and held["independent"] is None and held["deferrals"] == 1
    assert held["offered"] == ["wait+handoff"] and held["options"] == ["wait", "handoff"]
    bad = runs["t-bad"]
    assert bad["status"] == "needs_choice" and bad["failure"] == "check-failed" and bad["check_reasons"] == {"empty": 1, "unknown-source": 1}
    assert bad["requests"] == 2  # the runner's one stronger retry
    assert all(r["cost_usd"] == 0 and r["providers"] in ({"freea": r["requests"]}, {}) for r in runs.values())

    s = report["skills"]["files-answer"]["summary"]
    assert (s["planned"], s["runs"], s["completed"], s["deferred"], s["not_run"]) == (5, 5, 4, 1, 0)
    assert (s["passed_own"], s["passed_independent"], s["passed_both"], s["own_passed_but_wrong"], s["failed"]) == (3, 2, 2, 1, 1)
    assert s["rate_own"] == 0.75 and s["interval_own"] == [round(v, 4) for v in cc.wilson(3, 4)]
    assert s["rate_independent"] == 0.5 and s["failures"] == {"check-failed": 1} and s["check_reasons"] == {"empty": 1, "unknown-source": 1}
    assert s["cost_usd"] == 0 and s["providers"] == {"freea": 4} and s["offered"] == {"wait+handoff": 1}
    assert s["by_kind"]["lookup"] == {"completed": 2, "passed_own": 2, "passed_independent": 1}
    assert s["g1"] == {"met": False, "completed": 4, "enough_runs": False, "nothing_spent": True, "counts": "right",
                       "own_check_would_say": False}
    assert report["verdict"]["met"] is False and report["verdict"]["cost_usd"] == 0

    # The readable summary.
    assert "files-answer 0.1.0: 5 planned, 4 completed, 1 deferred, 0 not run" in out
    assert re.search(r"passed own check\s+3 of 4\s+75\.0%\s+\(95% interval 30\.1% to 95\.4%\)", out)
    assert "G1: at least 80% of completed runs RIGHT by the expected facts with nothing spent: no (2 of 4)" in out
    assert "Own check (second line, not the gate): 3 of 4 passed (75.0%); it overstates the right-rate by 25.0 points" in out
    assert "only 4 completed runs; the gate asks for 50 per skill" in out
    assert "CAVEAT: the skill's own check passed answers that were wrong 1 time(s) by the expected facts" in out
    assert "reported apart: neither passes nor failures" in out and "ghost-skill: skipped" in out

    # One CSV line per run, and nothing from a document, a prompt, an answer or a key anywhere.
    csv_path = Path(env["MUSE_HOME"]) / "chores-runs.csv"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 5 and tuple(rows[0]) == cc.CSV_FIELDS
    assert {r["task"]: r["outcome"] for r in rows}["t-held"] == "deferred" and all(r["cost_usd"] == "0.000000" for r in rows)
    assert {r["task"]: r["independent"] for r in rows} == {"t-right": "pass", "t-wrong": "fail", "t-none": "pass", "t-held": "", "t-bad": "fail"}
    stored = (Path(env["MUSE_HOME"]) / "chores-report.json").read_text(encoding="utf-8") + csv_path.read_text(encoding="utf-8") + out
    token = env.get("MUSE_SKILLS_APP_TOKEN", "")
    for needle in (CANARY_DOC, CANARY_REPLY, CANARY_QUESTION, "Harbour Street", "18,500", "Tomatoes", "door code", "Bearer", "kept"):
        assert needle not in stored, needle
    assert proxy.requests and proxy.requests[0]["auth"].startswith("Bearer ")
    for request in proxy.requests:
        assert request["auth"][7:] not in stored and "x-muse-allow-paid" not in request["headers"] and request["privacy"] == "standard"
    assert not token or token not in stored


def test_keep_outputs_stores_answers_only_when_asked(env, proxy, tasks_dir):
    route(proxy)
    check(env, tasks_dir, "--yes", "--keep-outputs", "--quiet")
    kept = report_of(env)["runs"]["files-answer/t-bad#1"]["kept"]
    assert CANARY_REPLY in kept["text"] and kept["reasons"] and kept["notes"]


def test_resume_keeps_finished_runs_and_tries_deferred_ones_again(env, proxy, tasks_dir, capsys):
    route(proxy)
    check(env, tasks_dir, "--yes", "--quiet")
    before = len(proxy.requests)
    route(proxy, {"t-held": lambda: chat(claim("due on the 5th of each month", [1]))})
    code = check(env, tasks_dir, "--yes", "--resume")
    out = capsys.readouterr().out
    assert len(proxy.requests) - before == 1  # only the run that was deferred
    assert re.search(r"files-answer\s+1 runs \(5 tasks x 1\), 4 already done: at most 12 requests", out)
    report = report_of(env)
    assert report["runs"]["files-answer/t-held#1"]["outcome"] == "passed" and len(report["runs"]) == 5
    s = report["skills"]["files-answer"]["summary"]
    assert (s["completed"], s["deferred"], s["passed_own"], s["passed_independent"]) == (5, 0, 4, 3) and s["rate_own"] == 0.8
    # 80% passed their own check, but only 60% are right: the gate, which counts right answers, is not met.
    assert s["g1"]["met"] is False and s["g1"]["own_check_would_say"] is True
    assert code == cc.EXIT_NOT_MET and "nothing spent: no (3 of 5)" in out
    assert "By the own check alone this skill would have looked like it met the line; it did not." in out
    # Everything done: a further resume sends nothing.
    before = len(proxy.requests)
    assert check(env, tasks_dir, "--yes", "--resume") == cc.EXIT_NOT_MET and len(proxy.requests) == before
    assert "Nothing to run." in capsys.readouterr().out
    # Without --resume the report starts again.
    route(proxy)
    check(env, tasks_dir, "--yes", "--quiet", "--only", "files-answer", "--sample", "2")
    assert len(report_of(env)["runs"]) == 2


def test_a_cost_above_zero_stops_everything_at_once(env, proxy, tasks_dir, capsys):
    route(proxy, {"t-wrong": lambda: chat(claim("The deposit equals two months of rent.", [1]), cost="0.002100", provider="paidco")})
    code = check(env, tasks_dir, "--yes")
    out, err = capsys.readouterr()
    assert code == cc.EXIT_SPENT
    assert "!! STOPPED: the router reported a cost of US$0.002100 for files-answer t-wrong." in err and "must cost nothing" in err
    report = report_of(env)
    assert report["stopped"] == "spend" and set(report["runs"]) == {"files-answer/t-right#1", "files-answer/t-wrong#1"}
    assert len(proxy.requests) == 2  # no further run was started
    s = report["skills"]["files-answer"]["summary"]
    assert s["cost_usd"] == 0.0021 and s["not_run"] == 3 and s["g1"]["met"] is False and s["g1"]["nothing_spent"] is False
    assert "WAS SPENT" in out and report["verdict"]["met"] is False


def test_deferred_runs_in_a_row_stop_the_skill_and_the_rest_is_not_run(env, proxy, tasks_dir, capsys):
    held = lambda: ration_429("defer:reserve", 3600, "wait:2026-11-14T00:00:00Z")  # noqa: E731
    route(proxy, {tid: held for tid in ANSWERS})
    code = check(env, tasks_dir, "--yes", "--stop-after", "2")
    out = capsys.readouterr().out
    report = report_of(env)
    s = report["skills"]["files-answer"]["summary"]
    assert (s["runs"], s["deferred"], s["completed"], s["not_run"]) == (2, 2, 0, 3) and len(proxy.requests) == 2
    assert s["rate_own"] is None and s["interval_own"] is None and s["g1"]["met"] is False and code == cc.EXIT_NOT_MET
    assert "stopped after 2 deferred runs in a row" in out and "stopped early: 2 runs in a row were deferred" in out
    assert s["offered"] == {"wait": 2}


def test_a_short_retry_after_is_waited_for_and_a_long_one_is_not(env, proxy, tasks_dir, monkeypatch):
    from muse.skills import runner
    sleeps: list[float] = []
    real = runner.run
    monkeypatch.setattr(runner, "run", lambda *a, **k: real(*a, **{**k, "sleep": sleeps.append}))
    route(proxy)
    proxy.script.append(ration_429("defer:pace", 20, "wait:2026-11-14T00:00:00Z"))  # the first call of the first run
    cc.main(["--tasks-dir", str(tasks_dir), "--delay", "0", "--max-wait", "30", "--yes", "--quiet", "--sample", "1", "--seed", "3"], environ=env)
    record = next(iter(report_of(env)["runs"].values()))
    assert sleeps == [20.0] and record["waits"] == 1 and record["deferrals"] == 1 and record["outcome"] != "deferred"


def test_the_pause_between_runs_is_kept(env, proxy, tasks_dir, monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(cc, "_sleep", pauses.append)
    route(proxy)
    cc.main(["--tasks-dir", str(tasks_dir), "--delay", "1.5", "--max-wait", "0", "--yes", "--quiet"], environ=env)
    assert pauses == [1.5] * 4  # between runs, not before the first


def test_router_unreachable_stops_with_a_plain_message(env, tasks_dir, capsys, monkeypatch):
    env = {**env, "MUSE_PROXY_URL": "http://127.0.0.1:9"}
    code = check(env, tasks_dir, "--yes")
    out, err = capsys.readouterr()
    assert code == cc.EXIT_NOT_MET and "The router could not be reached. Start it with `muse up`" in err
    report = report_of(env)
    assert report["stopped"] == "no-router" and len(report["runs"]) == cc.UNREACHABLE_STOP
    s = report["skills"]["files-answer"]["summary"]
    assert s["completed"] == 0 and s["failed"] == 0 and s["no_router"] == 2  # not counted as the skill's failures


def test_runs_repeats_and_only_and_usage_errors(env, proxy, tasks_dir, capsys):
    route(proxy)
    check(env, tasks_dir, "--yes", "--quiet", "--runs", "2", "--sample", "1", "--seed", "1")
    runs = report_of(env)["runs"]
    assert len(runs) == 2 and {k.split("#")[1] for k in runs} == {"1", "2"}
    assert check(env, tasks_dir, "--yes", "--only", "nonexistent") == cc.EXIT_USAGE
    assert "No task file for: nonexistent" in capsys.readouterr().err
    assert check(env, tasks_dir, "--yes", "--only", "ghost-skill") == cc.EXIT_NOT_MET  # nothing could run
    assert "Nothing to run." in capsys.readouterr().out
    assert cc.main(["--runs", "0"], environ=env) == cc.EXIT_USAGE
    (tasks_dir / "files-answer" / "tasks.jsonl").write_text("{broken\n", encoding="utf-8")
    assert check(env, tasks_dir, "--yes") == cc.EXIT_USAGE and "nothing was sent" in capsys.readouterr().err


def test_check_tasks_mode_validates_the_shipped_set_without_a_router(env, capsys):
    assert cc.main(["--check-tasks"], environ={**env, "MUSE_PROXY_URL": "http://127.0.0.1:9"}) == cc.EXIT_OK
    out = capsys.readouterr().out
    assert re.search(r"files-answer: \d+ tasks ok", out) and re.search(r"research-report: \d+ tasks ok", out)


def test_the_shipped_set_runs_end_to_end_for_both_skills_against_the_fake_router(env, proxy, capsys):
    """A sample of the real tasks through both real skills. The fake model finds nothing, so only the flow is proven."""

    def respond(request: dict):
        if request["path"].endswith("/embeddings"):
            return None
        system = request["messages"][0]["content"]
        if system.startswith("You plan"):
            return chat("no plan")
        if system.startswith("You read"):
            return chat(json.dumps({"claims": []}))
        return chat(json.dumps({"answer": "", "claims": [], "not_found": True}))

    proxy.responder = respond
    code = cc.main(["--delay", "0", "--max-wait", "0", "--yes", "--sample", "7", "--seed", "2"], environ=env)
    out = capsys.readouterr().out
    report = report_of(env)
    assert set(report["skills"]) == {"files-answer", "research-report"} and report["skipped"] == [] and code == cc.EXIT_NOT_MET
    assert re.search(r"research-report\s+7 runs \(7 tasks x 1\): at most 280 requests and 1050000 tokens", out)
    for name in ("files-answer", "research-report"):
        s = report["skills"][name]["summary"]
        assert s["completed"] == 7 and s["passed_own"] == 7 and s["cost_usd"] == 0 and s["g1"]["met"] is False
        assert s["passed_independent"] == s["by_kind"]["not-found"]["passed_independent"] == 1  # only the not-in-the-files task is right
        assert s["own_passed_but_wrong"] == 6 and set(s["by_kind"]) == KINDS
    assert out.count("CAVEAT: the skill's own check passed answers that were wrong 6 time(s)") == 2
    assert "Overall G1 line (right by expected facts, every skill that ran): no - not every skill has 50 completed runs yet." in out


def test_record_and_replay_together_is_refused(env, tasks_dir, capsys):
    assert check(env, tasks_dir, "--replay", "a", "--record", "b") == cc.EXIT_USAGE
    assert "not both" in capsys.readouterr().err


def test_replay_of_a_missing_directory_is_refused(env, tasks_dir, capsys, tmp_path):
    missing = tmp_path / "no-such-cassettes"
    assert check(env, tasks_dir, "--yes", "--replay", str(missing)) == cc.EXIT_USAGE
    assert "No cassette directory" in capsys.readouterr().err


def test_record_then_replay_does_not_call_the_router_again(env, proxy, tasks_dir, tmp_path):
    """The gate can be repeated from cassettes. The second run never reaches the router."""
    from muse import cassette

    route(proxy)
    recorded = tmp_path / "tapes"
    report_a = tmp_path / "first.json"
    report_b = tmp_path / "second.json"
    code = check(
        env, tasks_dir, "--yes", "--quiet", "--record", str(recorded),
        "--only", "files-answer", "--sample", "1", "--seed", "0",
        "--report", str(report_a), "--csv", str(tmp_path / "a.csv"),
    )
    files = list(recorded.glob("*.json"))
    assert files, code
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "authorization" not in text.lower() and "Bearer" not in text
    assert cassette.secret_problems(recorded) == []
    sent = len(proxy.requests)
    assert sent >= 1
    first = json.loads(report_a.read_text(encoding="utf-8"))
    assert first["options"]["record"] == str(recorded)
    again = check(
        env, tasks_dir, "--yes", "--quiet", "--replay", str(recorded),
        "--only", "files-answer", "--sample", "1", "--seed", "0",
        "--report", str(report_b), "--csv", str(tmp_path / "b.csv"),
    )
    assert again == code
    assert len(proxy.requests) == sent
    second = json.loads(report_b.read_text(encoding="utf-8"))
    assert second["options"]["replay"] == str(recorded)
    assert {k: v["outcome"] for k, v in first["runs"].items()} == {k: v["outcome"] for k, v in second["runs"].items()}


# ================================================================ typed facts (the first real run's fa-025: a right date, written another way)


@pytest.mark.parametrize("fact, texts, wrong", [
    ({"date": "2031-04-03"}, ["2031-04-03", "3 April 2031", "April 3, 2031", "2031/4/3", "03 Apr 2031", "2031年4月3日",
                              "民國120年4月3日", "due 2031.04.03", "the 3rd of April 2031"],
     ["2031-04-30", "4 April 2031", "April 3, 2032", "4月3日", "2031/3/4", "民國121年4月3日"]),
    ({"date": "2031-03-21", "year_optional": True}, ["21 March", "March 21", "3 月 21 日", "2031-03-21"], ["21 March 2030", "March 22"]),
    ({"time": "08:40"}, ["08:40", "8:40 am", "8:40", "上午8點40分", "8點40分", "8.40am"], ["8:40 pm", "下午8點40分", "08:41", "18.40 dollars"]),
    ({"time": "13:15"}, ["13:15", "1:15 pm", "1:15", "下午1點15分"], ["1:15 am", "13:16", "上午1點15分"]),
    ({"time": "22:30"}, ["10:30 pm", "22:30", "晚上10點半"], ["10:30 am", "22:00"]),
    ({"amount": 1450}, ["1,450", "1450.00", "US$1,450", "NT$1,450", "1,450 元", "$1450"], ["14,500", "145", "1,451"]),
    ({"count": 3, "unit": "year"}, ["3 years", "three years", "a 3-year warranty", "3 年", "三年", "保固三年"], ["4 years", "3 months", "十三年"]),
    ({"count": 2, "unit": "year"}, ["兩年", "2 年", "two years"], ["二十年", "12 years"]),
])
def test_typed_facts_take_every_form_of_the_value_and_no_other_value(fact, texts, wrong):
    for text in texts:
        assert cc.fact_in(fact, text), text
    for text in wrong:
        assert not cc.fact_in(fact, text), text
    assert cc.accepted_forms(fact)


def test_typed_facts_are_validated_and_the_shipped_dates_times_and_amounts_are_typed():
    base = {"id": "x", "skill": "s", "kind": "date", "lang": "en", "folders": [], "inputs": {"question": "q"}}
    for bad in ({"date": "3 April 2031"}, {"time": "8:40"}, {"time": "25:00"}, {"amount": "1450"}, {"count": 3}, {"count": 3, "unit": "fortnight"}):
        assert cc.task_problems({**base, "expect": {"facts": [bad]}}, Path("."), "s"), bad
    found, problems = cc.load_tasks(cc.TASKS_DIR)
    assert problems == []
    facts = [f for tasks in found.values() for t in tasks for f in t["expect"].get("facts", [])]
    assert sum("date" in f for f in facts) >= 15 and sum("time" in f for f in facts) >= 10 and sum("amount" in f for f in facts) >= 50
    fa025 = next(t for t in found["files-answer"] if t["id"] == "fa-025")
    assert fa025["expect"]["facts"] == [{"date": "2031-04-03", "source": "invoices.csv"}]
    # No `any` list is left that is only ways of writing one date.
    for f in facts:
        if "any" in f:
            assert not all(any(y for y, _, _ in cc.dates_in(a)) for a in f["any"]), f


# ================================================================ --keep-failures, the old-router warning, the caps


def test_keep_failures_writes_wrong_and_failed_runs_with_quotes_only_and_mode_0600(env, proxy, tasks_dir):
    route(proxy)
    check(env, tasks_dir, "--yes", "--quiet")
    path = Path(env["MUSE_HOME"]) / cc.FAILURES_FILE
    assert not path.exists()  # nothing is kept by default
    check(env, tasks_dir, "--yes", "--quiet", "--keep-failures")
    assert (path.stat().st_mode & 0o777) == 0o600
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert {r["task"] for r in rows} == {"t-wrong", "t-bad"}
    wrong = next(r for r in rows if r["task"] == "t-wrong")
    assert wrong["own_check"] == "passed" and wrong["right"] is False
    assert wrong["claims"] == [{"id": 1, "says": "The deposit equals two months of rent.", "quote": "The deposit equals two months of rent.",
                                "passage": 1, "source": "lease.md › paragraph 3"}]
    assert wrong["missing_facts"] == [{"fact": 1, "kind": "any", "accepted": ["12 Harbour Street"], "source": ["lease.md"]}]
    text = path.read_text(encoding="utf-8")
    assert CANARY_DOC not in text  # the passage around the quote never leaves
    assert CANARY_REPLY in text  # the failed answer itself is what the person asked to keep


def test_an_old_router_is_named_loudly_and_a_current_one_is_recorded(env, proxy, tasks_dir, capsys):
    route(proxy, {tid: (lambda: chat("")) for tid in ANSWERS})  # empty answers with no x-muse-answer header: an old build
    check(env, tasks_dir, "--yes", "--quiet", "--sample", "2")
    out, err = capsys.readouterr()
    assert "WARNING: your router is older than these skills" in err and "Update it and restart it" in err
    report = report_of(env)
    assert report["router"] == {"build": "old", "warned": True} and "Router build: old" in out
    run = next(iter(report["runs"].values()))
    assert run["empty_answers"] >= 1 and run["router"] == "old" and run["calls"] and all(set(c) == {"provider", "task", "ms", "status", "empty"}
                                                                                           for c in run["calls"])
    current = lambda: chat(claim("The monthly rent is 18,500 dollars.", [1]), **{"x-muse-reasoning": "low"})  # noqa: E731
    route(proxy, {tid: current for tid in ANSWERS})
    check(env, tasks_dir, "--yes", "--quiet", "--sample", "1")
    out, err = capsys.readouterr()
    assert "WARNING" not in err and report_of(env)["router"]["build"] == "current"


def test_only_kinds_and_the_request_cap(env, proxy, tasks_dir, capsys):
    route(proxy)
    assert check(env, tasks_dir, "--yes", "--quiet", "--only-kinds", "lookup") in (cc.EXIT_OK, cc.EXIT_NOT_MET)
    assert {r["kind"] for r in report_of(env)["runs"].values()} == {"lookup"}
    assert check(env, tasks_dir, "--yes", "--only-kinds", "poetry") == cc.EXIT_USAGE
    capsys.readouterr()
    check(env, tasks_dir, "--yes", "--quiet", "--max-requests", "13")  # a run may take 12: after two small runs the third could cross 13
    out = capsys.readouterr().out
    report = report_of(env)
    assert len(report["runs"]) == 2 and report["stopped"] == "request-cap" and report["requests_used"] <= 13
    assert "request cap: 13" in out and f"Requests used: {report['requests_used']} of the cap of 13 (stopped at the cap)" in out
    assert cc.main(["--max-requests", "0"], environ=env) == cc.EXIT_USAGE


def test_the_report_has_status_steps_slowest_calls_and_the_allowance_at_both_ends(env, proxy, tasks_dir, capsys):
    route(proxy)
    check(env, tasks_dir, "--yes", "--quiet")
    out = capsys.readouterr().out
    report = report_of(env)
    s = report["skills"]["files-answer"]["summary"]
    assert s["statuses"] == {"done": 3, "needs_choice": 2} and s["recoveries"] == 0 and s["unanswered"] == 0
    assert set(s["right_by_empty_answers"]) == {"0"} and s["slowest_calls"] and all("ms" in c for c in s["slowest_calls"])
    assert "statuses: done 3, needs_choice 2" in out and "slowest calls:" in out
    assert report["allowance"]["start"] is not None and report["allowance"]["end"] is not None
    assert "Free allowance at the start:" in out and "Free allowance at the end:" in out


def test_router_build_reads_the_call_facts():
    assert cc.router_build([{"reasoning": "low"}]) == "current" and cc.router_build([{"min_output": "raised:300->1024"}]) == "current"
    assert cc.router_build([{"empty": True, "flagged": True}]) == "current"
    assert cc.router_build([{"empty": True, "flagged": False}]) == "old"
    assert cc.router_build([{"deployment": "groq-gpt-oss-20b__fast", "task": "light"}]) == "old"
    assert cc.router_build([{"deployment": "gemini-3.1-flash-lite", "task": "light"}]) == "unknown" and cc.router_build([]) == "unknown"
