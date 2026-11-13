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
    ("t-wrong", "lookup", "What is the deposit and where is the flat?", {"facts": [{"any": ["12 Harbour Street"], "source": "lease.md"}]}),
    ("t-none", "not-found", "What is the door code?", {"not_found": True}),
    ("t-held", "numeric", "When is the rent due each month?", {"facts": [{"any": ["5th"], "source": "lease.md"}]}),
    ("t-bad", "date", f"Where is the flat? {CANARY_QUESTION}", {"facts": [{"any": ["12 Harbour Street"], "source": "lease.md"}]}),
]


def claim(text: str, cites: list[int], not_found: bool = False) -> str:
    return json.dumps({"answer": text + "".join(f" [{n}]" for n in cites), "claims": [{"text": text, "cites": cites}] if cites else [],
                       "not_found": not_found})


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
    assert bad["status"] == "needs_choice" and bad["failure"] == "check-failed" and bad["check_reasons"] == {"weak-overlap": 1}
    assert bad["requests"] == 2  # the runner's one stronger retry
    assert all(r["cost_usd"] == 0 and r["providers"] in ({"freea": r["requests"]}, {}) for r in runs.values())

    s = report["skills"]["files-answer"]["summary"]
    assert (s["planned"], s["runs"], s["completed"], s["deferred"], s["not_run"]) == (5, 5, 4, 1, 0)
    assert (s["passed_own"], s["passed_independent"], s["passed_both"], s["own_passed_but_wrong"], s["failed"]) == (3, 2, 2, 1, 1)
    assert s["rate_own"] == 0.75 and s["interval_own"] == [round(v, 4) for v in cc.wilson(3, 4)]
    assert s["rate_independent"] == 0.5 and s["failures"] == {"check-failed": 1} and s["check_reasons"] == {"weak-overlap": 1}
    assert s["cost_usd"] == 0 and s["providers"] == {"freea": 4} and s["offered"] == {"wait+handoff": 1}
    assert s["by_kind"]["lookup"] == {"completed": 2, "passed_own": 2, "passed_independent": 1}
    assert s["g1"] == {"met": False, "completed": 4, "enough_runs": False, "nothing_spent": True}
    assert report["verdict"]["met"] is False and report["verdict"]["cost_usd"] == 0

    # The readable summary.
    assert "files-answer 0.1.0: 5 planned, 4 completed, 1 deferred, 0 not run" in out
    assert re.search(r"passed own check\s+3 of 4\s+75\.0%\s+\(95% interval 30\.1% to 95\.4%\)", out)
    assert "G1: at least 80% of completed runs passed their own check with nothing spent: no (3 of 4)" in out
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
    route(proxy, {"t-held": lambda: chat(claim("The rent is due on the 5th of each month.", [1]))})
    code = check(env, tasks_dir, "--yes", "--resume")
    out = capsys.readouterr().out
    assert len(proxy.requests) - before == 1  # only the run that was deferred
    assert re.search(r"files-answer\s+1 runs \(5 tasks x 1\), 4 already done: at most 12 requests", out)
    report = report_of(env)
    assert report["runs"]["files-answer/t-held#1"]["outcome"] == "passed" and len(report["runs"]) == 5
    s = report["skills"]["files-answer"]["summary"]
    assert (s["completed"], s["deferred"], s["passed_own"]) == (5, 0, 4) and s["g1"]["met"] is True and s["rate_own"] == 0.8
    assert code == cc.EXIT_OK and "nothing spent: yes (4 of 5)" in out
    # Everything done: a further resume sends nothing.
    before = len(proxy.requests)
    assert check(env, tasks_dir, "--yes", "--resume") == cc.EXIT_OK and len(proxy.requests) == before
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
    assert set(report["skills"]) == {"files-answer", "research-report"} and report["skipped"] == [] and code == cc.EXIT_OK
    assert re.search(r"research-report\s+7 runs \(7 tasks x 1\): at most 280 requests and 1050000 tokens", out)
    for name in ("files-answer", "research-report"):
        s = report["skills"][name]["summary"]
        assert s["completed"] == 7 and s["passed_own"] == 7 and s["cost_usd"] == 0 and s["g1"]["met"] is True
        assert s["passed_independent"] == s["by_kind"]["not-found"]["passed_independent"] == 1  # only the not-in-the-files task is right
        assert s["own_passed_but_wrong"] == 6 and set(s["by_kind"]) == KINDS
    assert out.count("CAVEAT: the skill's own check passed answers that were wrong 6 time(s)") == 2
    assert "Overall G1 line (every skill that ran): yes - not every skill has 50 completed runs yet. Spent: US$0.000000." in out


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
