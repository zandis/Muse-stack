"""Skills through the home API: listing, starting a run, and the first card -> action path (a proposed file is
written only after its card is approved).

No network and no real provider: the scripted router of the skills tests (queue_fakes.FakeProxy)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from home_fakes import assert_error
from muse import cli
from muse.home import api, auth, fake, folders, skills as home_skills, state
from muse.skills import loader
from test_skills_support import ECHO, chat, env, proxy, write_skill  # noqa: F401  (fixtures)

OWNER = auth.Device("mei-phone", role="owner")
HOST = auth.Device("bot-tools", role="host")
OTHER_HOST = auth.Device("other-tools", role="host")
VIEWER = auth.Device("wall-screen", role="viewer")
CANARY = "CANARY-FILE-CONTENT-41d0"

WRITER = f"""
def run(ctx):
    name = ctx.inputs.get("name", "report.md")
    for i in range(int(ctx.inputs.get("count", 1))):
        ctx.workspace.propose_write(name if i == 0 else f"{{i}}-" + name, "{CANARY} " + str(ctx.inputs.get("body", "")), "the weekly report")
    return {{"text": "Proposed a report."}}


def check(output, ctx):
    return []
"""

READER = """
def run(ctx):
    return {"text": " ".join(sorted(info.path.rsplit("/", 1)[1] for info in ctx.workspace.list())), "roots": len(ctx.workspace.roots())}


def check(output, ctx):
    return []
"""

WRITES = {"muse_needs": {"files.read": [], "files.write": ["output"], "net": []}, "muse_writes": True}
READS = {"muse_needs": {"files.read": ["chosen"], "files.write": [], "net": []}}


class Clock:
    def __init__(self) -> None:
        self.now = 1_794_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def home(env, _isolated_muse_home):  # noqa: F811
    write_skill(_isolated_muse_home, "echo")
    write_skill(_isolated_muse_home, "writer", WRITER, **WRITES)
    write_skill(_isolated_muse_home, "reader", READER, **READS)
    write_skill(_isolated_muse_home, "unapproved", approve=False)
    return api.Home(proxy_running=lambda: True, clock=Clock())


@pytest.fixture()
def reports(tmp_path):
    folder = tmp_path / "Reports"
    folder.mkdir()
    folders.add("reports", folder, output=True)
    return folder


@pytest.fixture()
def notes(tmp_path):
    folder = tmp_path / "Notes"
    folder.mkdir()
    (folder / "a.md").write_text("alpha")
    (folder / "b.md").write_text("beta")
    folders.add("notes", folder)
    return folder


def call(home, who, method, path, body=None):
    resp = api.dispatch(home, who, method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


def ok(home, who, method, path, body=None, status=200):
    got, payload = call(home, who, method, path, body)
    assert got == status, (got, payload)
    return payload


def run(home, who, name, body=None) -> dict:
    """Start a run, wait for it, and return the finished run as the starter sees it."""
    started = ok(home, who, "POST", f"/v1/skills/{name}/run", body or {}, status=202)["run"]
    assert started["status"] == "running" and started["result"] is None and started["started_by"] == who.name
    home.skills.wait(started["id"])
    return ok(home, who, "GET", f"/v1/skills/runs/{started['id']}")["run"]


def records(home) -> list[tuple]:
    return [(r["action"], r["actor"], r["mandate"], r["outcome"], r.get("egress"), r["summary"]) for r in home.records().records()]


def answer(home, who, card_id, decision="approve"):
    return call(home, who, "POST", f"/v1/cards/{card_id}/answer", {"decision": decision})


# ---------------------------------------------------------------- listing


def test_the_list_says_what_each_skill_asks_for_and_whether_it_may_run(home, notes, reports):
    got = ok(home, HOST, "GET", "/v1/skills")
    by_name = {s["name"]: s for s in got["skills"]}
    assert {"echo", "writer", "reader", "unapproved", "files-answer"} <= set(by_name)
    assert by_name["files-answer"]["source"] == "shipped" and by_name["files-answer"]["approved"] is True
    assert by_name["echo"]["approved"] is True and by_name["echo"]["source"] == "home" and by_name["echo"]["state"] == "ready"
    assert by_name["unapproved"]["approved"] is False and by_name["unapproved"]["state"] == "not-approved"
    assert by_name["writer"]["writes_files"] is True and by_name["reader"]["reads_folders"] is True
    assert any("Nothing is written until you agree" in line for line in by_name["writer"]["needs"])
    assert by_name["echo"]["budget"] == {"requests": 5, "tokens": 20000, "seconds": 60.0, "paid_usd": 0.0}
    assert "path" not in by_name["echo"] and "hash" not in by_name["echo"] and "entry" not in by_name["echo"]
    # A host bot learns the folder names; only the person's own device sees where they are.
    assert got["folders"] == [{"name": "notes", "output": False}, {"name": "reports", "output": True}]
    assert str(notes) not in json.dumps(got)
    mine = ok(home, OWNER, "GET", "/v1/skills")["folders"]
    assert mine == [{"name": "notes", "output": False, "path": str(notes)}, {"name": "reports", "output": True, "path": str(reports)}]
    assert_error(*call(home, VIEWER, "GET", "/v1/skills"), 403, "forbidden")


# ---------------------------------------------------------------- running


def test_a_run_goes_through_the_runner_and_its_answer_comes_back(home, proxy):  # noqa: F811
    got = run(home, HOST, "echo", {"inputs": {"q": "say something"}})
    assert got["status"] == "done" and got["skill"] == "echo" and got["finished_at"] and got["writes"] == []
    result = got["result"]
    assert result["output"] == {"text": "GOOD", "task": "light"} and result["check"] == {"status": "passed", "reasons": []}
    assert result["used"]["requests"] == 1 and result["used"]["providers"]["freea"]["requests"] == 1
    assert "events" not in result  # progress events stay on the home
    assert [r["prompt"] for r in proxy.requests] == ["say something"]
    assert proxy.requests[0]["auth"].startswith("Bearer sk-muse-app-")  # the runner's own app token: never a provider key
    assert records(home) == [
        ("skill.run", "helper", "device:bot-tools", "ok", "host:bot-tools", "Ran the skill echo for a host bot"),
        ("llm.call", "helper", "device:bot-tools", "ok", "service:freea", "The skill echo used the service freea: 1 request")]
    mine = run(home, OWNER, "echo")
    assert mine["status"] == "done"
    assert records(home)[2:] == [("skill.run", "person", None, "ok", "none", "Ran the skill echo"),
                                 ("llm.call", "person", None, "ok", "service:freea", "The skill echo used the service freea: 1 request")]


def test_a_local_model_or_the_cache_is_not_recorded_as_having_left(home, proxy):  # noqa: F811
    proxy.script.append(chat("GOOD", provider="ollama"))
    run(home, OWNER, "echo")
    proxy.script.append(chat("GOOD", provider="cache"))
    run(home, OWNER, "echo")
    assert [r for r in records(home) if r[0] == "llm.call"] == []
    assert [r[4] for r in records(home)] == ["none", "none"]


def test_a_host_sees_only_its_own_runs(home):
    mine = run(home, HOST, "echo")
    theirs = run(home, OTHER_HOST, "echo")
    persons = run(home, OWNER, "echo")
    for run_id in (theirs["id"], persons["id"], "0123456789abcdef"):
        assert_error(*call(home, HOST, "GET", f"/v1/skills/runs/{run_id}"), 404, "not_found")
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{mine['id']}")["run"]["id"] == mine["id"]
    for run_id in (mine["id"], theirs["id"], persons["id"]):
        assert ok(home, OWNER, "GET", f"/v1/skills/runs/{run_id}")["run"]["status"] == "done"
    assert_error(*call(home, VIEWER, "GET", f"/v1/skills/runs/{mine['id']}"), 403, "forbidden")
    assert_error(*call(home, HOST, "GET", "/v1/skills/runs/not-an-id"), 404, "not_found")


def test_a_skill_that_is_missing_unapproved_or_changed_does_not_run(home, _isolated_muse_home):
    assert_error(*call(home, HOST, "POST", "/v1/skills/nosuch/run", {}), 404, "not_found")
    assert_error(*call(home, HOST, "POST", "/v1/skills/unapproved/run", {}), 409, "conflict")
    (_isolated_muse_home / "skills" / "echo" / "skill.py").write_text("def run(ctx):\n    return {'text': 'changed'}\n\ndef check(o, c):\n    return []\n")
    err = assert_error(*call(home, HOST, "POST", "/v1/skills/echo/run", {}), 409, "conflict")
    assert "muse skills approve" in err["message"]
    assert home.book.running() == [] and records(home) == []


@pytest.mark.parametrize("body", [
    {"inputs": "text"}, {"inputs": {"a b": "x"}}, {"inputs": {"1a": "x"}}, {"inputs": {"q": ["list"]}}, {"inputs": {"q": {"a": 1}}},
    {"inputs": {"q": "x" * 20_001}}, {"inputs": {f"k{i}": "v" for i in range(33)}}, {"unknown": 1}, {"folders": "notes"},
    {"folders": [1]}, {"budget": {"dollars": 1}}, {"budget": {"requests": -1}}, {"budget": {"requests": True}},
    {"budget": {"requests": 6}}, {"budget": {"tokens": 20001}}, {"budget": {"seconds": 61}}, {"budget": {"paid_usd": 0.01}},
    {"privacy": "secret"}, {"privacy": "public"}, {"task": "huge"}, {"task": "everyday"}, {"task": "frontier"}, {"out": "reports"},
])
def test_what_a_caller_asks_for_is_checked_and_never_exceeds_the_skills_own(home, reports, body):
    err = assert_error(*call(home, OWNER, "POST", "/v1/skills/echo/run", body), 400, "invalid_request")
    assert "\n" not in err["message"]
    assert home.book.running() == []


def test_a_run_may_be_given_less_than_the_skill_allows_and_a_stricter_privacy_class(home):
    seen = {}

    def recording(skill, inputs, **kwargs):
        seen.update(kwargs, inputs=inputs)
        return fake._fake_skill_run(skill, inputs)

    home.skills.runner = recording
    got = run(home, HOST, "echo", {"inputs": {"q": "x", "n": 3, "flag": True}, "budget": {"requests": 2, "tokens": 500, "seconds": 5},
                                    "privacy": "local", "task": "light"})
    assert got["status"] == "done"
    assert seen["budget"] == {"paid_usd": 0, "requests": 2, "tokens": 500, "seconds": 5} and seen["privacy"] == "local"
    assert seen["task"] == "light" and seen["inputs"] == {"q": "x", "n": 3, "flag": True} and seen["apply_writes"] is False
    assert seen["folders"] == [] and seen["out_dir"] is None
    env_given = seen["environ"]
    assert "MUSE_PROXY_URL" in env_given and not any(name.endswith("_API_KEY") for name in env_given)


def test_money_is_zero_unless_the_persons_own_device_gives_some(home, _isolated_muse_home):
    write_skill(_isolated_muse_home, "paid", muse_budget={"requests": 5, "tokens": 20000, "seconds": 60, "paid_usd": 0.5})
    seen = []
    home.skills.runner = lambda skill, inputs, **kw: seen.append(kw["budget"]) or fake._fake_skill_run(skill, inputs)
    assert run(home, HOST, "paid")["status"] == "done" and seen[-1] == {"paid_usd": 0}  # the skill's own 0.5 is not inherited
    err = assert_error(*call(home, HOST, "POST", "/v1/skills/paid/run", {"budget": {"paid_usd": 0.1}}), 403, "forbidden")
    assert "person's own device" in err["message"]
    assert run(home, OWNER, "paid", {"budget": {"paid_usd": 0.1}})["status"] == "done" and seen[-1] == {"paid_usd": 0.1}
    assert_error(*call(home, OWNER, "POST", "/v1/skills/paid/run", {"budget": {"paid_usd": 0.51}}), 400, "invalid_request")


def test_one_run_at_a_time_for_a_host_and_two_for_the_home(home):
    release = threading.Event()
    home.skills.runner = lambda skill, inputs, **kw: release.wait(10) and fake._fake_skill_run(skill, inputs)
    first = ok(home, HOST, "POST", "/v1/skills/echo/run", {}, status=202)["run"]
    assert_error(*call(home, HOST, "POST", "/v1/skills/echo/run", {}), 409, "conflict")
    second = ok(home, OTHER_HOST, "POST", "/v1/skills/echo/run", {}, status=202)["run"]
    assert_error(*call(home, OWNER, "POST", "/v1/skills/echo/run", {}), 409, "conflict")  # two at once for the whole home
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{first['id']}")["run"]["status"] == "running"
    release.set()
    for started in (first, second):
        home.skills.wait(started["id"])
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{first['id']}")["run"]["status"] == "done"
    assert run(home, HOST, "echo")["status"] == "done"
    assert (home_skills.MAX_RUNS, home_skills.MAX_RUNS_PER_HELPER) == (2, 1)


def test_a_runner_that_breaks_is_a_failed_run_with_no_detail(home, caplog):
    def broken(skill, inputs, **kw):
        raise RuntimeError("CANARY-ERRTEXT /secret/path")

    home.skills.runner = broken
    got = run(home, HOST, "echo")
    assert got["status"] == "failed" and got["result"]["notes"] == ["The home hit a problem running this skill (RuntimeError). Nothing was written."]
    assert "CANARY" not in json.dumps(got) and "CANARY" not in caplog.text
    assert records(home)[-1][:5] == ("skill.run", "helper", "device:bot-tools", "failed", "host:bot-tools")
    assert run(home, HOST, "echo")["status"] == "failed"  # the slot was given back


def test_a_run_cut_off_by_the_home_stopping_is_reported_as_failed(home):
    orphan = home.book.new_run("echo", HOST.name, HOST.role)  # as a home that then stopped would leave it
    got = ok(home, HOST, "GET", f"/v1/skills/runs/{orphan}")["run"]
    assert got["status"] == "failed" and got["result"]["notes"] == [home_skills.INTERRUPTED]


# ---------------------------------------------------------------- folders: names, never paths


def test_a_skill_reads_only_folders_the_person_put_on_the_list(home, notes, tmp_path):
    got = run(home, HOST, "reader", {"folders": ["notes"]})
    assert got["status"] == "done" and got["result"]["output"] == {"text": "a.md b.md", "roots": 1}
    other = tmp_path / "Private"
    other.mkdir()
    for bad in (["private"], [str(other)], ["/etc"], ["../Private"], ["notes", "nope"], ["notes", "notes"], ["Notes"], [""]):
        err = assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"folders": bad}), 400, "invalid_request")
        assert str(other) not in err["message"]
    assert_error(*call(home, HOST, "POST", "/v1/skills/echo/run", {"folders": ["notes"]}), 400, "invalid_request")  # it reads no folders
    # Taking a folder off the list takes effect for the next run.
    assert folders.remove("notes") is True
    assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"folders": ["notes"]}), 400, "invalid_request")


def test_a_listed_folder_that_was_replaced_by_a_link_is_refused(home, notes, tmp_path):
    secret = tmp_path / "Secret"
    secret.mkdir()
    (secret / "diary.md").write_text("private")
    moved = tmp_path / "Notes-moved"
    notes.rename(moved)
    notes.symlink_to(secret, target_is_directory=True)  # the name on the list now leads somewhere else
    assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"folders": ["notes"]}), 400, "invalid_request")
    notes.unlink()
    assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"folders": ["notes"]}), 400, "invalid_request")  # gone


def test_what_may_be_put_on_the_list(tmp_path, _isolated_muse_home):
    good = tmp_path / "Docs"
    good.mkdir()
    entry = folders.add("docs", good, now=1_800_000_000)
    assert entry == {"name": "docs", "path": str(good.resolve()), "output": False, "added_at": "2027-01-15T08:00:00Z"}
    path = folders.folders_path()
    assert path == _isolated_muse_home / "skill-folders.json" and oct(path.stat().st_mode & 0o777) == "0o600"
    (_isolated_muse_home / "skills").mkdir(exist_ok=True)
    for name, target, words in (("docs", good, "already on the list"), ("Bad Name", good, "a folder name is"), ("x", tmp_path / "nope", "not a folder"),
                                ("x", "/", "whole disk"), ("x", _isolated_muse_home, "Muse's own folder"),
                                ("x", _isolated_muse_home / "skills", "Muse's own folder"), ("x", _isolated_muse_home.parent, "Muse's own folder"),
                                ("x", good / "file.txt", "not a folder"), ("x", "", "not a usable")):
        with pytest.raises(folders.FolderError, match=words):
            folders.add(name, target)
    link = tmp_path / "link-to-home"
    link.symlink_to(_isolated_muse_home, target_is_directory=True)
    with pytest.raises(folders.FolderError, match="Muse's own folder"):
        folders.add("sneaky", link)  # a link is followed before it is judged
    assert list(folders.load()) == ["docs"]
    with pytest.raises(folders.FolderError, match="reading only"):
        folders.resolve("docs", for_output=True)
    assert folders.resolve("docs") == good.resolve() and folders.remove("docs") and not folders.remove("docs")


def test_a_list_edited_by_hand_cannot_point_at_muses_own_folder_or_be_overwritten_when_damaged(home, _isolated_muse_home):
    path = folders.folders_path()
    path.write_text(json.dumps({"folders": [{"name": "keys", "path": str(_isolated_muse_home), "output": True},
                                            {"name": "rel", "path": "relative/path"}, {"name": "Bad", "path": "/tmp"}, "junk"]}))
    assert list(folders.load()) == ["keys"]  # the malformed entries are not there at all
    assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"folders": ["keys"]}), 400, "invalid_request")
    assert_error(*call(home, HOST, "POST", "/v1/skills/writer/run", {"out": "keys"}), 400, "invalid_request")
    path.write_text("{not json")
    assert folders.load() == {}
    with pytest.raises(folders.FolderError, match="not in the form Muse writes"):
        folders.add("docs", _isolated_muse_home.parent / "x")  # whatever is being added: the damaged list comes first
    assert path.read_text() == "{not json"


def test_muse_skills_folders_add_list_rm(capsys, tmp_path, env):  # noqa: F811
    docs, out = tmp_path / "Docs", tmp_path / "Out"
    docs.mkdir()
    out.mkdir()

    def go(*argv):
        capsys.readouterr()
        code = cli.main(["skills", "folders", *argv])
        got = capsys.readouterr()
        return code, got.out, got.err

    assert go("list")[1].startswith("No folders.")
    code, text, _ = go("add", "docs", str(docs))
    assert code == 0 and "may read" in text and str(docs) in text and "cannot choose any other path" in text
    code, text, _ = go("add", "out", str(out), "--output")
    assert code == 0 and "propose new files" in text
    code, text, _ = go("list")
    assert code == 0 and [line.split()[0] for line in text.splitlines()] == ["docs", "out"] and "read + new files" in text
    assert [(e["name"], e["output"]) for e in json.loads(go("list", "--json")[1])] == [("docs", False), ("out", True)]
    code, _, err = go("add", "docs", str(out))
    assert code == 1 and "already on the list" in err
    code, _, err = go("add", "home", os.environ["MUSE_HOME"])
    assert code == 1 and "Muse's own folder" in err
    assert go("rm", "docs")[0] == 0 and go("rm", "docs")[0] == 1
    assert list(folders.load()) == ["out"]


# ---------------------------------------------------------------- card -> write


def proposed(home, who=HOST, body=None) -> tuple[dict, dict]:
    """A finished `writer` run with one proposed file, and that file's entry."""
    got = run(home, who, "writer", {"out": "reports", **(body or {})})
    assert got["status"] == "done", got
    assert len(got["writes"]) == 1 and got["writes"][0]["state"] == "pending"
    return got, got["writes"][0]


def test_a_proposed_file_is_written_only_when_its_card_is_approved(home, reports):
    got, write = proposed(home, body={"inputs": {"body": "week 46"}})
    target = reports / "report.md"
    assert not target.exists() and list(reports.iterdir()) == []  # the run wrote nothing
    assert "1 file was proposed. Nothing is written until the person approves each one in Muse." in got["result"]["notes"]
    card = home.cards.get(write["card_id"])
    assert (card["kind"], card["title"], card["thing"], card["where"], card["status"]) == ("save_file", "Save a file", "report.md", str(reports), "pending")
    assert card["under"]["text"] == "The skill writer proposed this file" and card["job_ref"] == f"skill-run:{got['id']}"
    assert card["extra"] == {"size_bytes": write["size"]} and CANARY not in json.dumps(card)  # the card names the file, not its contents
    reply = ok(home, OWNER, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "approve"})
    assert reply["write"] == {"state": "written", "file": "report.md", "reason": None} and reply["card"]["status"] == "approved"
    assert target.read_text() == f"{CANARY} week 46" and oct(target.stat().st_mode & 0o777) == "0o600"
    after = ok(home, HOST, "GET", f"/v1/skills/runs/{got['id']}")["run"]["writes"]
    assert after == [{"card_id": card["id"], "file": "report.md", "size": write["size"], "state": "written", "reason": None}]
    last = home.records().records()[-1]
    assert (last["action"], last["actor"], last["mandate"], last["outcome"], last["egress"]) == ("files.write", "helper", "device:bot-tools", "ok", "none")
    assert last["links"] == {"card": card["id"]} and len(last["resource"]) == 64 and last["summary"] == "Wrote a file a skill proposed, after it was approved"
    # The bytes are gone from the home's own store once the file is written.
    assert CANARY.encode() not in state.state_path().read_bytes()
    assert [r[0] for r in records(home)] == ["skill.run", "card.create", "card.answer", "files.write"]


def test_declining_throws_the_file_away(home, reports):
    got, write = proposed(home)
    assert CANARY.encode() in state.state_path().read_bytes()  # kept until the card is answered
    reply = ok(home, OWNER, "POST", f"/v1/cards/{write['card_id']}/answer", {"decision": "decline"})
    assert reply["write"] == {"state": "discarded", "file": "report.md", "reason": None}
    assert list(reports.iterdir()) == [] and CANARY.encode() not in state.state_path().read_bytes()
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{got['id']}")["run"]["writes"][0]["state"] == "discarded"
    assert [r[0] for r in records(home)] == ["skill.run", "card.create", "card.answer"]  # no files.write: nothing was written


def test_a_card_nobody_answers_expires_and_the_file_is_thrown_away(home, reports):
    got, write = proposed(home)
    home.clock.now += 24 * 3600 + 1
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{got['id']}")["run"]["writes"][0]["state"] == "discarded"
    assert_error(*answer(home, OWNER, write["card_id"]), 410, "card_expired")  # too late to approve
    assert list(reports.iterdir()) == [] and CANARY.encode() not in state.state_path().read_bytes()
    # The same when the first to notice is the answer itself, or the card list.
    got2, write2 = proposed(home, body={"inputs": {"name": "second.md"}})
    home.clock.now += 24 * 3600 + 1
    assert_error(*answer(home, OWNER, write2["card_id"]), 410, "card_expired")
    assert home.book.write_for(write2["card_id"])["state"] == "discarded"
    got3, write3 = proposed(home, body={"inputs": {"name": "third.md"}})
    home.clock.now += 24 * 3600 + 1
    ok(home, OWNER, "GET", "/v1/cards")
    assert home.book.write_for(write3["card_id"])["state"] == "discarded" and list(reports.iterdir()) == []


def test_answering_twice_writes_once(home, reports):
    got, write = proposed(home)
    assert answer(home, OWNER, write["card_id"])[0] == 200
    target = reports / "report.md"
    target.write_text("the person edited it afterwards")
    for decision in ("approve", "decline"):
        assert_error(*answer(home, OWNER, write["card_id"], decision), 409, "card_already_answered")
    assert target.read_text() == "the person edited it afterwards"  # not written again, not removed
    assert home.skills.settle_card(home.cards.get(write["card_id"])) == {"state": "written", "file": "report.md", "reason": None}
    assert home.skills.sweep() == 0 and target.read_text() == "the person edited it afterwards"
    assert [r[0] for r in records(home)].count("files.write") == 1


def test_two_answers_at_the_same_moment_write_once(home, reports):
    got, write = proposed(home)
    other_owner = auth.Device("desk", role="owner")
    results = []
    barrier = threading.Barrier(2)

    def go(who, decision):
        barrier.wait()
        results.append(answer(home, who, write["card_id"], decision)[0])

    threads = [threading.Thread(target=go, args=(OWNER, "approve")), threading.Thread(target=go, args=(other_owner, "approve"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == [200, 409]
    assert (reports / "report.md").read_text().startswith(CANARY) and [r[0] for r in records(home)].count("files.write") == 1


def test_an_existing_file_is_never_overwritten(home, reports):
    got, write = proposed(home)
    (reports / "report.md").write_text("already here")
    reply = ok(home, OWNER, "POST", f"/v1/cards/{write['card_id']}/answer", {"decision": "approve"})
    assert reply["write"]["state"] == "refused" and "already exists" in reply["write"]["reason"]
    assert (reports / "report.md").read_text() == "already here"
    last = records(home)[-1]
    assert last[:5] == ("files.write", "helper", "device:bot-tools", "failed", "none")
    assert CANARY.encode() not in state.state_path().read_bytes()


def test_a_folder_taken_off_the_list_before_the_approval_is_not_written_to(home, reports):
    got, write = proposed(home)
    folders.remove("reports")
    reply = ok(home, OWNER, "POST", f"/v1/cards/{write['card_id']}/answer", {"decision": "approve"})
    assert reply["write"] == {"state": "refused", "file": "report.md", "reason": "the output folder is no longer on the person's list"}
    assert list(reports.iterdir()) == []


def test_a_file_cannot_be_written_through_a_link_planted_after_the_run(home, reports, tmp_path):
    got, write = proposed(home, body={"inputs": {"name": "sub/report.md"}})
    outside = tmp_path / "Outside"
    outside.mkdir()
    (reports / "sub").symlink_to(outside, target_is_directory=True)  # the folder the file was to go in now leads elsewhere
    reply = ok(home, OWNER, "POST", f"/v1/cards/{write['card_id']}/answer", {"decision": "approve"})
    assert reply["write"]["state"] == "refused" and "outside the output folder" in reply["write"]["reason"]
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape.md", "/etc/muse-test-escape.md", "sub/../../escape.md"])
def test_a_skill_cannot_propose_a_file_outside_the_output_folder(home, reports, name):
    got = run(home, HOST, "writer", {"out": "reports", "inputs": {"name": name}})
    assert got["status"] == "failed" and got["writes"] == [] and home.cards.counts()["pending"] == 0
    assert not (reports.parent / "escape.md").exists() and not Path("/etc/muse-test-escape.md").exists()


def test_without_an_output_folder_files_go_to_muses_own_folder_for_that_skill(home, _isolated_muse_home):
    got = run(home, OWNER, "writer")
    write = got["writes"][0]
    target = _isolated_muse_home / "skills-out" / "writer" / "report.md"
    assert not target.exists()
    assert ok(home, OWNER, "POST", f"/v1/cards/{write['card_id']}/answer", {"decision": "approve"})["write"]["state"] == "written"
    assert target.read_text().startswith(CANARY)
    last = home.records().records()[-1]
    assert (last["action"], last["actor"], last["mandate"]) == ("files.write", "helper", None)  # the person started this run


def test_an_out_folder_must_be_one_the_person_allowed_for_new_files(home, notes, reports):
    assert_error(*call(home, HOST, "POST", "/v1/skills/writer/run", {"out": "notes"}), 400, "invalid_request")  # on the list, read only
    assert_error(*call(home, HOST, "POST", "/v1/skills/writer/run", {"out": str(reports)}), 400, "invalid_request")  # a path, not a name
    assert_error(*call(home, HOST, "POST", "/v1/skills/reader/run", {"out": "reports"}), 400, "invalid_request")  # it proposes no files


def test_a_host_cannot_approve_its_own_file_but_can_see_what_became_of_it(home, reports):
    got, write = proposed(home)
    assert_error(*answer(home, HOST, write["card_id"]), 403, "forbidden")
    assert_error(*answer(home, OTHER_HOST, write["card_id"]), 403, "forbidden")
    assert list(reports.iterdir()) == []
    assert ok(home, HOST, "GET", f"/v1/cards/{write['card_id']}")["card"]["status"] == "pending"  # its own run's card
    assert_error(*call(home, OTHER_HOST, "GET", f"/v1/cards/{write['card_id']}"), 404, "not_found")
    assert [c["id"] for c in ok(home, HOST, "GET", "/v1/cards")["cards"]] == [write["card_id"]]
    assert ok(home, OTHER_HOST, "GET", "/v1/cards")["cards"] == []
    assert answer(home, OWNER, write["card_id"])[0] == 200
    assert ok(home, HOST, "GET", f"/v1/cards/{write['card_id']}")["card"]["status"] == "approved"


def test_a_card_answered_while_nothing_was_looking_is_settled_at_the_next_look(home, reports):
    got, write = proposed(home)
    home.cards.answer(write["card_id"], "approve", "desktop")  # e.g. straight in the card store, not through this API
    assert not (reports / "report.md").exists()
    assert ok(home, HOST, "GET", f"/v1/skills/runs/{got['id']}")["run"]["writes"][0]["state"] == "written"
    assert (reports / "report.md").read_text().startswith(CANARY)


def test_many_proposed_files_become_at_most_twenty_cards(home, reports):
    got = run(home, HOST, "writer", {"out": "reports", "inputs": {"count": 23}})
    assert got["status"] == "done" and len(got["writes"]) == home_skills.MAX_WRITES_PER_RUN == 20
    assert any("3 more proposed files were left out" in note for note in got["result"]["notes"])
    assert home.cards.counts()["pending"] == 20 and list(reports.iterdir()) == []


def test_a_card_that_is_not_about_a_file_is_only_answered(home):
    card = home.cards.create(kind="spend", title="Pay", thing="NT$30", cost={"amount": "30", "currency": "TWD"},
                             extra={"payee": "ACME", "limit_left": {"amount": "70", "currency": "TWD"}})
    reply = ok(home, OWNER, "POST", f"/v1/cards/{card}/answer", {"decision": "approve"})
    assert "write" not in reply and set(reply) == {"card", "recorded"}
    assert home.skills.settle_card(home.cards.get(card)) is None


# ---------------------------------------------------------------- the store's bounds


def test_old_runs_are_pruned_but_never_one_with_a_file_still_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "MAX_RUNS_KEPT", 3)
    book = state.HomeState(path=tmp_path / "s.sqlite")
    waiting = book.new_run("writer", "bot", "host")
    book.finish_run(waiting, "done", {"output": "x"})
    book.add_write(card_id="c" * 16, run_id=waiting, skill="writer", root="/r", path="/r/a.md", data=b"bytes")
    made = []
    for i in range(6):
        made.append(book.new_run("echo", "bot", "host"))
        book.finish_run(made[-1], "done", {"output": i})
    assert book.get_run(waiting) is not None and book.write_for("c" * 16)["state"] == "pending"
    kept = [r for r in made if book.get_run(r) is not None]
    assert len(kept) <= 4 and made[-1] in kept and made[0] not in kept


def test_a_result_too_large_to_keep_is_replaced_by_a_note(tmp_path):
    book = state.HomeState(path=tmp_path / "s.sqlite")
    run_id = book.new_run("echo", "bot", "host")
    book.finish_run(run_id, "done", {"output": "x" * (state.MAX_RESULT_BYTES + 1), "notes": [], "check": {"status": "passed"}})
    kept = book.get_run(run_id)["result"]
    assert kept["output"] is None and "too large" in kept["notes"][0] and kept["check"] == {"status": "passed"}


def test_a_claimed_write_is_claimed_once(tmp_path):
    book = state.HomeState(path=tmp_path / "s.sqlite")
    book.add_write(card_id="a" * 16, run_id="r1", skill="writer", root="/r", path="/r/a.md", data=b"bytes")
    claims = []
    threads = [threading.Thread(target=lambda: claims.append(book.claim_write("a" * 16))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sum(1 for c in claims if c is not None) == 1 and [c for c in claims if c][0]["data"] == b"bytes"
    book.settle_write("a" * 16, "written")
    assert book.write_for("a" * 16)["state"] == "written" and book.claim_write("a" * 16) is None


# ---------------------------------------------------------------- the fake home


def test_the_fake_home_lists_skills_and_finishes_a_run_with_a_canned_answer():
    home = fake.FakeHome("normal")
    try:
        dev = auth.Device(fake.FAKE_DEVICE)
        listed = ok(home, dev, "GET", "/v1/skills")
        assert any(s["name"] == "files-answer" for s in listed["skills"]) and [f["name"] for f in listed["folders"]] == ["documents", "feeds"]  # the fake home's demo folders
        got = run(home, dev, "files-answer", {"inputs": {"question": "x"}})
        assert got["status"] == "done" and "fake home" in got["result"]["output"]["text"] and got["writes"] == []
    finally:
        home.close()
