"""muse.skills: the SKILL.md format, the loader and approvals, the router client, the runner, the CLI.

No network and no real provider: a scripted stand-in for the router (queue_fakes.FakeProxy).
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
from pathlib import Path

import pytest

from wardmuse import apps, cli
from wardmuse.skills import format as fmt
from wardmuse.skills import llm, loader, runner
from wardmuse.skills.workspace import NeedsViolation, Workspace
from queue_fakes import Clock, FakeProxy, completion, error, privacy_403, ration_429
from test_skills_support import ECHO, chat, env, proxy, skill_md, write_skill  # noqa: F401  (fixtures)

CANARY = "CANARY-SKILL-7f3a91"


# ================================================================ format


def test_a_good_skill_md_parses():
    skill = fmt.parse_skill_md(skill_md(body="# Echo\n\nFor people."), folder_name="echo")
    assert (skill.name, skill.version, skill.entry, skill.tier_default, skill.privacy_default) == (
        "echo", "0.1.0", "skill.py:run", "light", "standard")
    assert skill.budget.as_dict() == {"requests": 5, "tokens": 20000, "seconds": 60.0, "paid_usd": 0.0}  # paid_usd defaults to 0
    assert skill.needs.as_dict() == {"files.read": [], "files.write": [], "net": []}
    assert skill.writes is False and skill.check == "check" and skill.body == "# Echo\n\nFor people."


def test_the_open_format_keys_are_kept_and_the_author_is_read():
    skill = fmt.parse_skill_md(skill_md(license="MIT", compatibility="any", metadata={"author": "Ann"}))
    assert skill.author == "Ann" and skill.license == "MIT"


@pytest.mark.parametrize("changes, expected", [
    ({"name": None}, "name is required"),
    ({"name": "Bad Name"}, "name is required"),
    ({"name": "other"}, "must equal the folder name"),
    ({"description": None}, "description is required"),
    ({"description": "x" * 1025}, "longer than 1024"),
    ({"surprise": 1}, "surprise: unknown key"),
    ({"muse": None}, "muse: is required"),
    ({"muse_colour": "red"}, "muse.colour: unknown key"),
    ({"muse_version": None}, "muse.version is required"),
    ({"muse_version": "one"}, "muse.version must look like"),
    ({"muse_entry": "../evil.py:run"}, "muse.entry must be"),
    ({"muse_entry": "/abs/skill.py:run"}, "muse.entry must be"),
    ({"muse_entry": "skill.py"}, "muse.entry must be"),
    ({"muse_tier_default": "embed"}, "muse.tier_default must be one of"),
    ({"muse_privacy_default": "public"}, "muse.privacy_default must be one of"),
    ({"muse_budget": {"requests": 0, "tokens": 10, "seconds": 5}}, "muse.budget.requests"),
    ({"muse_budget": {"requests": 1, "tokens": 10}}, "muse.budget.seconds"),
    ({"muse_budget": {"requests": 1, "tokens": 10, "seconds": 5, "paid_usd": -1}}, "muse.budget.paid_usd"),
    ({"muse_budget": {"requests": 1, "tokens": 10, "seconds": 5, "dollars": 1}}, "muse.budget.dollars: unknown key"),
    ({"muse_budget": {"requests": True, "tokens": 10, "seconds": 5}}, "muse.budget.requests"),
    ({"muse_needs": {"files.read": [], "files.write": []}}, "muse.needs.net is required"),
    ({"muse_needs": {"files.read": ["/etc"], "files.write": [], "net": []}}, "is not one of chosen, data"),
    ({"muse_needs": {"files.read": [], "files.write": [], "net": [], "camera": []}}, "muse.needs.camera: unknown key"),
    ({"muse_needs": {"files.read": [], "files.write": [], "net": ["http://x.example/a"]}}, "plain lower-case host name"),
    ({"muse_needs": {"files.read": "chosen", "files.write": [], "net": []}}, "must be a list"),
    ({"muse_writes": "no"}, "muse.writes must be true or false"),
    ({"muse_writes": True}, "true exactly when"),
    ({"muse_needs": {"files.read": [], "files.write": ["output"], "net": []}}, "true exactly when"),
    ({"muse_check": "not a name"}, "muse.check must be"),
    ({"muse_check": None}, "muse.check is required"),
])
def test_each_bad_skill_md_is_refused_with_a_plain_reason(changes, expected):
    with pytest.raises(fmt.FormatError) as caught:
        fmt.parse_skill_md(skill_md(**changes), folder_name="echo")
    assert any(expected in problem for problem in caught.value.problems), caught.value.problems


def test_frontmatter_shape_errors():
    for text, expected in (("no frontmatter", "must start with"), ("---\nname: x\n", "not closed"),
                           ("---\n: : :\n  - [\n---\n", "not valid YAML"), ("---\n- a\n---\n", "must be a YAML mapping")):
        with pytest.raises(fmt.FormatError) as caught:
            fmt.parse_skill_md(text)
        assert expected in str(caught.value)


def test_every_problem_is_listed_at_once():
    with pytest.raises(fmt.FormatError) as caught:
        fmt.parse_skill_md(skill_md(muse_tier_default="x", muse_check="1bad", muse_colour=1))
    assert len(caught.value.problems) == 3


def test_needs_in_words_says_what_the_skill_touches():
    skill = fmt.parse_skill_md(skill_md(
        muse_needs={"files.read": ["chosen"], "files.write": ["output", "data"], "net": []}, muse_writes=True))
    words = " ".join(fmt.needs_in_words(skill))
    assert "folders you name" in words and "Nothing is written until you agree" in words
    assert "No internet access" in words and "never pays" in words and "at most 5 requests" in words
    assert "No access to your files." in fmt.needs_in_words(fmt.parse_skill_md(skill_md()))


# ================================================================ loader, hash, approval


def test_the_shipped_skill_is_found_and_ready(_isolated_muse_home):
    skills, broken = loader.discover({"MUSE_HOME": str(_isolated_muse_home)})
    assert broken == []
    assert skills["files-answer"].source == "shipped" and skills["files-answer"].state == "ready"


def test_a_home_skill_needs_approval_and_a_change_revokes_it(_isolated_muse_home):
    home = {"MUSE_HOME": str(_isolated_muse_home)}
    folder = write_skill(_isolated_muse_home, approve=False)
    skill = loader.find("echo", home)
    assert skill.source == "home" and skill.state == "not-approved" and skill.hash.startswith("sha256:")
    with pytest.raises(loader.LoaderError, match="not approved yet"):
        loader.require_ready(skill)
    assert loader.approve("echo", home).state == "ready"
    assert loader.find("echo", home).state == "ready"
    recorded = json.loads((_isolated_muse_home / "skills-approved.json").read_text())
    assert recorded["skills"]["echo"]["hash"] == skill.hash

    (folder / "skill.py").write_text(ECHO + "\n# one more byte\n")
    changed = loader.find("echo", home)
    assert changed.state == "changed" and changed.hash != skill.hash
    with pytest.raises(loader.LoaderError, match="changed since you approved it"):
        loader.require_ready(changed)
    assert runner.run("echo", {}, environ=home).status == "failed"  # and the runner refuses it

    (folder / "extra.txt").write_text("a new file also changes the hash")
    (folder / "skill.py").write_text(ECHO)
    assert loader.find("echo", home).state == "changed"
    (folder / "extra.txt").unlink()
    assert loader.find("echo", home).state == "ready"  # byte-for-byte back to what was approved


def test_the_hash_ignores_bytecode_caches_and_covers_names_and_bytes(_isolated_muse_home):
    folder = write_skill(_isolated_muse_home, approve=False)
    before = loader.content_hash(loader.read_folder(folder))
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "skill.cpython-313.pyc").write_bytes(b"junk")
    assert loader.content_hash(loader.read_folder(folder)) == before
    assert loader.content_hash({"a": b"bc"}) != loader.content_hash({"ab": b"c"})  # path and bytes cannot run together
    (folder / "skill.py").rename(folder / "skill2.py")
    assert loader.content_hash(loader.read_folder(folder)) != before


def test_approval_refuses_a_folder_that_changed_while_it_was_being_read(_isolated_muse_home):
    home = {"MUSE_HOME": str(_isolated_muse_home)}
    folder = write_skill(_isolated_muse_home, approve=False)
    shown = loader.find("echo", home).hash
    (folder / "skill.py").write_text(ECHO + "\nimport os\n")
    with pytest.raises(loader.LoaderError, match="changed while"):
        loader.approve("echo", home, expect_hash=shown)
    assert loader.find("echo", home).state == "not-approved"


def test_what_runs_is_what_was_hashed_not_a_planted_bytecode_file(_isolated_muse_home):
    home = {"MUSE_HOME": str(_isolated_muse_home)}
    folder = write_skill(_isolated_muse_home, code="VALUE = 'approved'\n\ndef run(ctx):\n    return VALUE\n\ndef check(o, c):\n    return []\n")
    skill = loader.find("echo", home)
    (folder / "skill.py").write_text("VALUE = 'swapped after the hash was taken'\n")  # edited after the snapshot
    assert loader.import_skill(skill).VALUE == "approved"
    assert not (folder / "__pycache__").exists()  # and no bytecode cache was written


def test_broken_folders_are_listed_and_never_runnable(_isolated_muse_home, tmp_path):
    home = {"MUSE_HOME": str(_isolated_muse_home)}
    base = _isolated_muse_home / "skills"
    (base / "nomd").mkdir(parents=True)
    write_skill(_isolated_muse_home, name="badkey", approve=False, muse_colour="red")
    write_skill(_isolated_muse_home, name="noentry", approve=False, muse_entry="missing.py:run")
    linked = write_skill(_isolated_muse_home, name="linked", approve=False)
    (linked / "secret").symlink_to(tmp_path)
    write_skill(_isolated_muse_home, name="files-answer", approve=False)  # may not shadow a shipped skill
    skills, broken = loader.discover(home)
    reasons = {b.name: " ".join(b.problems) for b in broken}
    assert "no SKILL.md" in reasons["nomd"] and "muse.colour: unknown key" in reasons["badkey"]
    assert "missing.py, which is not in the folder" in reasons["noentry"] and "symbolic link" in reasons["linked"]
    assert "ships with Muse" in reasons["files-answer"] and skills["files-answer"].source == "shipped"
    with pytest.raises(loader.LoaderError, match="cannot be loaded"):
        loader.find("badkey", home)
    with pytest.raises(loader.LoaderError, match="no skill named"):
        loader.find("nope", home)


# ================================================================ the router client


def client(proxy: FakeProxy, budget: llm.Budget | None = None, **kw):
    events: list[dict] = []
    meter = llm.Meter(budget or llm.Budget(5, 20000, 60.0))
    c = llm.Client(proxy.url, "sk-muse-app-test", meter, on_event=lambda kind, **facts: events.append({"kind": kind, **facts}), **kw)
    return c, meter, events


def test_ask_sends_the_token_task_and_privacy_and_returns_the_route_facts(proxy):
    proxy.push(*chat("the answer", task="everyday", provider="groq", cost="0.000000", **{
        "x-muse-cache": "miss", "x-muse-min-output": "raised:300->1024", "x-muse-handoff": "suggest"}))
    c, meter, events = client(proxy, default_task="everyday", privacy="private")
    answer = c.ask([{"role": "user", "content": CANARY}], max_tokens=50)
    request = proxy.requests[0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer sk-muse-app-test"
    assert request["task"] == "everyday" and request["privacy"] == "private" and request["model"] == "auto"
    assert request["max_tokens"] == 50 and "x-muse-allow-paid" not in request["headers"]
    assert (answer.text, answer.provider, answer.cost_usd, answer.cache, answer.min_output, answer.answer) == (
        "the answer", "groq", 0.0, "miss", "raised:300->1024", "")
    assert answer.task == "everyday" and answer.handoff is True and (answer.tokens_in, answer.tokens_out) == (11, 7)
    assert meter.used()["requests"] == 1 and meter.used()["tokens"] == 18 and meter.used()["providers"]["groq"]["requests"] == 1
    assert CANARY not in json.dumps(events) and "the answer" not in json.dumps(events)


def test_empty_reasoning_is_reported_not_hidden(proxy):
    proxy.push(200, {"x-muse-provider": "freea", "x-muse-answer": "empty-reasoning"}, completion(""))
    answer = client(proxy)[0].ask([{"role": "user", "content": "hi"}])
    assert answer.text == "" and answer.answer == "empty-reasoning" and answer.empty


def test_a_json_schema_becomes_a_response_format(proxy):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    client(proxy)[0].ask([{"role": "user", "content": "hi"}], json_schema=schema)
    assert proxy.requests[0]["body"]["response_format"]["json_schema"]["schema"] == schema


def test_a_step_may_tighten_privacy_but_never_loosen_it(proxy):
    c, _, _ = client(proxy, privacy="private")
    c.ask([{"role": "user", "content": "hi"}], privacy="standard")
    c.ask([{"role": "user", "content": "hi"}], privacy="local")
    assert [r["privacy"] for r in proxy.requests] == ["private", "local"]


def test_the_budget_is_checked_before_anything_is_sent(proxy):
    c, meter, _ = client(proxy, llm.Budget(requests=1, tokens=20000, seconds=60.0))
    c.ask([{"role": "user", "content": "one"}])
    with pytest.raises(llm.BudgetExceeded) as caught:
        c.ask([{"role": "user", "content": "two"}])
    assert caught.value.which == "requests" and len(proxy.requests) == 1  # the second call never left

    c, meter, _ = client(proxy, llm.Budget(requests=5, tokens=100, seconds=60.0))
    with pytest.raises(llm.BudgetExceeded) as caught:
        c.ask([{"role": "user", "content": "x" * 30}], max_tokens=200)  # 200 output tokens alone are over 100
    assert caught.value.which == "tokens" and len(proxy.requests) == 1 and meter.requests == 0

    clock = Clock()
    meter = llm.Meter(llm.Budget(5, 20000, 10.0), clock=clock.now)
    c = llm.Client(proxy.url, "t", meter)
    clock.t += 11
    with pytest.raises(llm.BudgetExceeded) as caught:
        c.ask([{"role": "user", "content": "late"}])
    assert caught.value.which == "seconds" and len(proxy.requests) == 1

    with pytest.raises(llm.BudgetExceeded):
        client(proxy, llm.Budget(1, 3, 60.0))[0].embed(["a long enough text to cost more than three tokens"])
    assert len(proxy.requests) == 1


def test_tokens_already_used_count_against_the_next_call(proxy):
    proxy.push(*chat("a", tin=900, tout=90))
    c, meter, _ = client(proxy, llm.Budget(requests=5, tokens=1200, seconds=60.0))
    c.ask([{"role": "user", "content": "hi"}], max_tokens=100)
    assert meter.tokens == 990
    with pytest.raises(llm.BudgetExceeded):
        c.ask([{"role": "user", "content": "hi"}], max_tokens=300)
    assert len(proxy.requests) == 1


def test_a_429_becomes_a_typed_deferral_with_exactly_the_routers_options(proxy):
    proxy.push(*ration_429("defer:reserve", 600, "wait:2026-11-14T00:00:00Z,handoff,pay:0.041000"))
    c, meter, events = client(proxy)
    with pytest.raises(llm.Deferred) as caught:
        c.ask([{"role": "user", "content": "hi"}])
    d = caught.value
    assert d.retry_after == 600 and d.reason == "defer:reserve" and d.sent_upstream is False
    assert d.options == llm.Options(wait_at="2026-11-14T00:00:00Z", handoff=True, pay_usd=0.041, offered=True)
    assert d.options.as_list() == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}, {"choice": "handoff"},
                                   {"choice": "pay", "about_usd": 0.041}]
    assert len(proxy.requests) == 1  # it did not sleep and try again by itself
    assert meter.tokens == 0 and meter.reserved == 0 and meter.deferred == 1
    assert [e["kind"] for e in events] == ["deferred"]


def test_deferral_variants(proxy):
    proxy.push(*ration_429("refuse:oneoff-held", None, "none"))
    proxy.push(*ration_429("", 57, None, wait="provider-limit"))
    proxy.push(429, {}, error("Rate limit reached", 429))
    c, _, _ = client(proxy)
    held, limited, bare = [pytest.raises(llm.Deferred) for _ in range(3)]
    with held as a:
        c.ask([{"role": "user", "content": "hi"}])
    with limited as b:
        c.ask([{"role": "user", "content": "hi"}])
    with bare as d:
        c.ask([{"role": "user", "content": "hi"}])
    assert a.value.retry_after is None and a.value.options.as_list() == [] and a.value.options.offered is True
    assert b.value.reason == "provider-limit" and b.value.retry_after == 57 and b.value.sent_upstream is True
    assert b.value.options.offered is False
    assert d.value.retry_after is None and d.value.reason == "" and d.value.options == llm.Options()


def test_a_403_privacy_refusal_is_final_and_other_errors_are_typed(proxy):
    proxy.push(*privacy_403("local"))
    proxy.push(403, {}, error("[muse] this key cannot switch app", 403))
    proxy.push(400, {}, error(f"[muse] invalid x-muse-task: unknown task 'poetry'. | {CANARY}", 400))
    proxy.push(500, {}, error(f"provider said: {CANARY}", 500))
    proxy.push(401, {}, error("[muse] unknown API key", 401))
    c, _, events = client(proxy, privacy="local")
    with pytest.raises(llm.PrivacyRefused) as refused:
        c.ask([{"role": "user", "content": "hi"}])
    assert refused.value.privacy == "local" and len(proxy.requests) == 1
    statuses = []
    for _ in range(4):
        with pytest.raises(llm.RouterError) as caught:
            c.ask([{"role": "user", "content": "hi"}])
        statuses.append(caught.value.status)
        assert CANARY not in str(caught.value)
    assert statuses == [403, 400, 500, 401]
    assert CANARY not in json.dumps(events, default=str)
    dead = llm.Client("http://127.0.0.1:9", "t", llm.Meter(llm.Budget(5, 20000, 60.0)))
    with pytest.raises(llm.RouterError) as caught:
        dead.ask([{"role": "user", "content": "hi"}])
    assert caught.value.status == 0


def test_embed_uses_the_embed_alias_and_counts_against_the_budget(proxy):
    from queue_fakes import embeddings
    proxy.push(200, {"x-muse-provider": "cohere", "x-muse-deployment": "embed-v4"}, embeddings([[0.0, 1.0], [1.0, 0.0]], tokens=9))
    c, meter, events = client(proxy, privacy="private")
    assert c.embed([CANARY, "second"]) == [[0.0, 1.0], [1.0, 0.0]]
    request = proxy.requests[0]
    assert request["path"] == "/v1/embeddings" and request["model"] == "embed" and request["task"] == "embed"
    assert request["privacy"] == "private" and request["input"] == [CANARY, "second"]
    assert meter.requests == 1 and meter.tokens == 9 and CANARY not in json.dumps(events)
    proxy.push(*ration_429("defer:pace", 30, "wait:2026-11-14T00:00:00Z"))
    with pytest.raises(llm.Deferred):
        c.embed(["again"])
    proxy.push(200, {}, completion("not embeddings"))
    with pytest.raises(llm.RouterError):
        c.embed(["again"])


def test_raising_a_tier_moves_one_step_and_stops_at_heavy():
    assert [llm.raised(t, 1) for t in ("light", "everyday", "heavy", "frontier", "embed")] == [
        "everyday", "heavy", "heavy", "frontier", "embed"]
    assert llm.raised("light", 0) == "light"


# ================================================================ the workspace: needs, path escapes, writes


def test_reads_stay_inside_the_named_folders(tmp_path):
    inside, outside = tmp_path / "docs", tmp_path / "private"
    inside.mkdir(), outside.mkdir()
    (inside / "a.txt").write_text("fine")
    (inside / "sub").mkdir()
    (inside / "sub" / "b.txt").write_text("also fine")
    (outside / "secret.txt").write_text(CANARY)
    (inside / "link.txt").symlink_to(outside / "secret.txt")
    (inside / "linkdir").symlink_to(outside)
    ws = Workspace([inside])
    assert ws.read_text("a.txt") == "fine" and ws.read_text(inside / "sub" / "b.txt") == "also fine"
    for bad in ("../private/secret.txt", inside / ".." / "private" / "secret.txt", outside / "secret.txt", "/etc/hostname",
                inside / "link.txt", inside / "linkdir" / "secret.txt", "sub/../../private/secret.txt"):
        with pytest.raises(NeedsViolation):
            ws.read_bytes(bad)
    listed = [Path(info.path).name for info in ws.list()]
    assert listed == ["a.txt", "b.txt"] and ws.skipped_links == 2  # the links are skipped, and counted
    with pytest.raises(NeedsViolation):
        list(ws.list(outside))
    assert Workspace([]).roots() == []
    with pytest.raises(NeedsViolation):
        Workspace([]).read_text(inside / "a.txt")


def test_a_link_inside_the_folder_to_a_file_inside_the_folder_is_fine(tmp_path):
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "real.txt")
    ws = Workspace([tmp_path])
    assert ws.read_text("alias.txt") == "x" and len(list(ws.list())) == 1  # listed once, by its real path


def test_proposed_writes_are_checked_and_touch_nothing(tmp_path):
    out, elsewhere = tmp_path / "out", tmp_path / "elsewhere"
    out.mkdir(), elsewhere.mkdir()
    (out / "escape").symlink_to(elsewhere)
    ws = Workspace([], out, may_write=True)
    write = ws.propose_write("report.md", "hello", "the weekly report")
    assert write.path == str(out / "report.md") and write.bytes == b"hello" and not write.applied
    assert list(out.iterdir()) == [out / "escape"]  # nothing written
    for bad in ("../x.md", elsewhere / "x.md", "escape/x.md", "/etc/x.md", "a/../../x.md"):
        with pytest.raises(NeedsViolation):
            ws.propose_write(bad, "x")
    with pytest.raises(NeedsViolation):  # a skill that did not declare writes may not propose any
        Workspace([], out, may_write=False).propose_write("a.md", "x")
    assert ws.apply(write) is True and (out / "report.md").read_text() == "hello"
    again = ws.propose_write("report.md", "second")
    assert ws.apply(again) is False and "does not overwrite" in again.refused and (out / "report.md").read_text() == "hello"
    nested = ws.propose_write("sub/deep.md", "x")
    assert ws.apply(nested) is True and (out / "sub" / "deep.md").read_text() == "x"


# ================================================================ the runner


def test_a_run_that_passes_its_check_is_done(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("GOOD answer", task="light", provider="groq"))
    seen: list[dict] = []
    result = runner.run("echo", {"q": CANARY}, environ=env, on_event=seen.append)
    assert result.status == "done" and result.exit_code == 0 and result.output["text"] == "GOOD answer"
    assert result.check == {"status": "passed", "reasons": []} and result.notes == []
    assert result.used["requests"] == 1 and result.used["cost_usd"] == 0 and list(result.used["providers"]) == ["groq"]
    request = proxy.requests[0]
    assert request["task"] == "light" and request["privacy"] == "standard"
    assert [e["kind"] for e in result.events] == ["start", "call", "check", "end"] and seen == result.events
    # The runner's own app: created on first use, never paid, token kept out of the result.
    app = apps.load_apps(env)["skills"]
    assert app.priority == "normal" and app.allow_paid is False
    assert apps.hash_token(request["auth"].removeprefix("Bearer ")) == app.token_sha256
    assert request["auth"].removeprefix("Bearer ") not in json.dumps(result.public(), default=str)
    assert CANARY not in json.dumps(result.events)  # the prompt never reaches an event


def test_a_failed_check_retries_once_on_the_next_tier_up(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("weak answer", task="light"))
    proxy.push(*chat("GOOD answer", task="everyday", provider="gemini"))
    result = runner.run("echo", {}, environ=env)
    assert [r["task"] for r in proxy.requests] == ["light", "everyday"]
    assert result.status == "done" and result.output["text"] == "GOOD answer" and result.check["status"] == "passed"
    assert any("stronger model" in note for note in result.notes)
    assert [e["kind"] for e in result.events] == ["start", "call", "check", "retry", "call", "check", "end"]
    assert result.used["requests"] == 2 and set(result.used["providers"]) == {"freea", "gemini"}


def test_the_retry_happens_once_only_and_then_a_choice_is_needed(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("weak", task="light"))
    proxy.push(*chat("still weak", task="everyday"))
    proxy.push(*chat("GOOD but never asked for", task="heavy"))
    result = runner.run("echo", {}, environ=env)
    assert [r["task"] for r in proxy.requests] == ["light", "everyday"]  # exactly one retry
    assert result.status == "needs_choice" and result.exit_code == 4 and result.options == []  # the router offered nothing: no guess
    assert result.check["status"] == "failed" and result.output["text"] == "still weak"
    assert any("did not pass its check" in n for n in result.notes) and any("no other choice" in n for n in result.notes)


def test_after_two_failed_checks_the_handoff_is_offered_only_if_the_router_suggested_it(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("weak", task="everyday"))
    proxy.push(*chat("still weak", task="heavy", **{"x-muse-handoff": "suggest"}))
    result = runner.run("echo", {}, task="everyday", environ=env)
    assert [r["task"] for r in proxy.requests] == ["everyday", "heavy"]
    assert result.status == "needs_choice" and result.options == [{"choice": "handoff"}]


def test_a_skill_already_on_heavy_is_not_retried(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("weak", task="heavy"))
    result = runner.run("echo", {"step": "heavy"}, environ=env)
    assert len(proxy.requests) == 1 and result.status == "needs_choice" and result.check["status"] == "failed"


def test_a_deferral_gives_needs_choice_with_exactly_what_the_router_offered(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*ration_429("defer:reserve", 3600, "wait:2026-11-14T00:00:00Z,pay:0.041000"))
    result = runner.run("echo", {}, environ=env)
    assert result.status == "needs_choice" and result.output is None and result.check["status"] == "not-run"
    assert result.options == [{"choice": "wait", "until": "2026-11-14T00:00:00Z"}, {"choice": "pay", "about_usd": 0.041}]
    assert len(proxy.requests) == 1 and result.used["deferred"] == 1 and result.used["tokens"] == 0
    assert any("not free right now" in n for n in result.notes)

    proxy.push(*ration_429("refuse:oneoff-held", None, "none"))
    result = runner.run("echo", {}, environ=env)
    assert result.status == "needs_choice" and result.options == []
    assert any("no other choice" in n for n in result.notes)


def test_when_the_stronger_tier_is_not_free_the_first_answer_is_kept_and_the_choices_shown(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("weak", task="light"))
    proxy.push(*ration_429("defer:reserve", 900, "wait:2026-11-13T18:00:00Z,handoff"))
    result = runner.run("echo", {}, environ=env)
    assert [r["task"] for r in proxy.requests] == ["light", "everyday"]
    assert result.status == "needs_choice" and result.output["text"] == "weak" and result.check["status"] == "failed"
    assert result.options == [{"choice": "wait", "until": "2026-11-13T18:00:00Z"}, {"choice": "handoff"}]


def test_the_runner_may_sit_out_a_short_deferral_when_told_to(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*ration_429("defer:yield", 3, "wait:2026-11-13T18:00:00Z"))
    proxy.push(*chat("GOOD", task="light"))
    clock = Clock()
    result = runner.run("echo", {}, environ=env, wait_up_to=5, clock=clock.now, sleep=clock.sleep)
    assert result.status == "done" and clock.sleeps == [3] and len(proxy.requests) == 2
    proxy.push(*ration_429("defer:pace", 600, "wait:2026-11-13T18:00:00Z"))
    clock = Clock()
    assert runner.run("echo", {}, environ=env, wait_up_to=5, clock=clock.now, sleep=clock.sleep).status == "needs_choice"
    assert clock.sleeps == []  # a long wait is the person's choice, not the runner's


def test_a_privacy_refusal_is_final_and_never_downgraded(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*privacy_403("local"))
    proxy.push(*chat("GOOD from a cloud provider that must never be asked"))
    result = runner.run("echo", {}, privacy="local", environ=env)
    assert result.status == "failed" and result.refusal == {"kind": "privacy", "privacy": "local"}
    assert len(proxy.requests) == 1 and proxy.requests[0]["privacy"] == "local"  # no second try under a lower class
    assert any("did not lower the privacy class" in n for n in result.notes)


def test_budget_overrides_and_running_out(env, proxy):
    code = """
    def run(ctx):
        for i in range(3):
            ctx.llm.ask([{"role": "user", "content": "step"}])
        return "GOOD"

    def check(output, ctx):
        return []
    """
    write_skill(envget(env, "MUSE_HOME"), code=code)
    result = runner.run("echo", {}, budget={"requests": 2}, environ=env)
    assert result.status == "failed" and len(proxy.requests) == 2  # the third call was never sent
    assert any("budget of 2 requests" in n for n in result.notes) and result.used["requests"] == 2
    assert runner.run("echo", {}, environ=env).status == "done"
    assert runner.run("echo", {}, budget={"dollars": 1}, environ=env).status == "failed"


def test_a_money_budget_uses_the_paid_app_and_says_so_to_the_router(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("GOOD", provider="paidp", cost="0.004100"))
    result = runner.run("echo", {}, budget={"paid_usd": 0.05}, environ=env)
    request = proxy.requests[0]
    assert request["headers"]["x-muse-allow-paid"] == "true"
    registry = apps.load_apps(env)
    assert registry["skills-paid"].allow_paid is True
    assert apps.hash_token(request["auth"].removeprefix("Bearer ")) == registry["skills-paid"].token_sha256
    assert result.used["cost_usd"] == 0.0041 and result.status == "done"


def test_the_token_is_repaired_once_after_a_rotation(env, proxy):
    write_skill(envget(env, "MUSE_HOME"))
    assert runner.run("echo", {}, environ=env).status in ("done", "needs_choice")
    first = proxy.requests[0]["auth"]
    apps.rotate_token("skills", env)
    proxy.requests.clear()
    proxy.script.clear()
    proxy.push(*chat("GOOD"))
    assert runner.run("echo", {}, environ=env).status == "done"
    assert proxy.requests[0]["auth"] != first


def test_notes_partial_and_stop(env, proxy):
    code = """
    def run(ctx):
        if ctx.inputs.get("stop"):
            ctx.stop("Give a question.")
        ctx.note("Two files could not be read.", incomplete=True)
        ctx.note("Two files could not be read.", incomplete=True)
        ctx.event("progress", files=2, name="x" * 500)
        return "GOOD"

    def check(output, ctx):
        return None
    """
    write_skill(envget(env, "MUSE_HOME"), code=code)
    result = runner.run("echo", {}, environ=env)
    assert result.status == "partial" and result.exit_code == 3 and result.notes == ["Two files could not be read."]
    progress = next(e for e in result.events if e["kind"] == "skill.progress")
    assert progress["files"] == 2 and progress["name"] == "(long text removed)"  # long text never rides in an event
    stopped = runner.run("echo", {"stop": "1"}, environ=env)
    assert stopped.status == "failed" and stopped.notes == ["Give a question."] and proxy.requests == []


def test_a_crashing_skill_fails_without_leaking_its_message(env, proxy):
    code = f"""
    def run(ctx):
        raise RuntimeError("{CANARY} from a file")

    def check(output, ctx):
        return []
    """
    write_skill(envget(env, "MUSE_HOME"), code=code)
    result = runner.run("echo", {}, environ=env)
    assert result.status == "failed" and result.exit_code == 1
    assert "RuntimeError at skill.py:3" in result.notes[0] and CANARY not in json.dumps(result.public(), default=str)


def test_folders_are_given_only_to_skills_that_asked(env, proxy, tmp_path):
    write_skill(envget(env, "MUSE_HOME"))
    result = runner.run("echo", {}, folders=[tmp_path], environ=env)
    assert result.status == "failed" and "did not ask to read folders" in result.notes[0] and proxy.requests == []
    assert runner.run("echo", {}, out_dir=tmp_path, environ=env).status == "failed"
    write_skill(envget(env, "MUSE_HOME"), name="netty", muse_needs={"files.read": [], "files.write": [], "net": ["api.example.com"]})
    result = runner.run("netty", {}, environ=env)
    assert result.status == "failed" and "needs the sandbox" in result.notes[0]


READER = """
def run(ctx):
    target = ctx.inputs.get("path")
    text = ctx.workspace.read_text(target) if target else ""
    if ctx.inputs.get("write"):
        ctx.workspace.propose_write(ctx.inputs["write"], "GOOD " + text, "a copy")
    return "GOOD " + str(len(text))


def check(output, ctx):
    return [] if output.startswith("GOOD") and not ctx.inputs.get("fail") else ["told to fail"]
"""
READS = {"muse_needs": {"files.read": ["chosen"], "files.write": ["output", "data"], "net": []}, "muse_writes": True}


def test_a_skill_cannot_read_outside_the_folders_named_for_the_run(env, proxy, tmp_path):
    docs, other = tmp_path / "docs", tmp_path / "other"
    docs.mkdir(), other.mkdir()
    (docs / "a.txt").write_text("12345")
    (other / "secret.txt").write_text(CANARY)
    (docs / "link.txt").symlink_to(other / "secret.txt")
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    assert runner.run("echo", {"path": str(docs / "a.txt")}, folders=[docs], environ=env).output == "GOOD 5"
    for escape in (str(other / "secret.txt"), "../other/secret.txt", str(docs / "link.txt"), "/etc/passwd"):
        result = runner.run("echo", {"path": escape}, folders=[docs], environ=env)
        assert result.status == "failed" and result.output is None, escape
        assert "did not declare" in result.notes[0] and CANARY not in json.dumps(result.public(), default=str)
    assert runner.run("echo", {}, folders=[tmp_path / "missing"], environ=env).status == "failed"


def test_writes_are_proposals_until_the_caller_agrees(env, proxy, tmp_path):
    docs, out = tmp_path / "docs", tmp_path / "out"
    docs.mkdir()
    (docs / "a.txt").write_text("abc")
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    inputs = {"path": str(docs / "a.txt"), "write": "copy.txt"}
    result = runner.run("echo", inputs, folders=[docs], out_dir=out, environ=env)
    assert result.status == "done" and len(result.writes) == 1 and result.writes[0].path == str(out.resolve() / "copy.txt")
    assert result.writes[0].bytes == b"GOOD abc" and result.writes[0].reason == "a copy" and not result.writes[0].applied
    assert not out.exists()  # nothing written without consent
    runner.apply_writes(result)
    assert (out / "copy.txt").read_text() == "GOOD abc" and result.writes[0].applied

    result = runner.run("echo", {**inputs, "write": "second.txt"}, folders=[docs], out_dir=out, environ=env, apply_writes=True)
    assert (out / "second.txt").exists() and result.writes[0].applied
    result = runner.run("echo", inputs, folders=[docs], out_dir=out, environ=env, apply_writes=True)
    assert result.writes[0].applied is False and any("does not overwrite" in n for n in result.notes)
    # A result that failed its check proposes nothing, even with consent given up front.
    result = runner.run("echo", {**inputs, "write": "third.txt", "fail": "1"}, folders=[docs], out_dir=out, environ=env, apply_writes=True)
    assert result.writes == [] and not (out / "third.txt").exists()
    result = runner.run("echo", {**inputs, "write": "../outside.txt"}, folders=[docs], out_dir=out, environ=env, apply_writes=True)
    assert result.status == "failed" and not (tmp_path / "outside.txt").exists()
    assert (Path(envget(env, "MUSE_HOME")) / "skills-data").exists() is False  # the data folder is made only when a skill uses it


# ================================================================ the command line


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_list_show_and_approve(env, proxy, capsys):
    write_skill(envget(env, "MUSE_HOME"), approve=False, metadata={"author": "Ann Example"})
    code, out, _ = run_cli(capsys, "skills", "list")
    assert code == 0 and "files-answer" in out and "ready" in out and "echo" in out and "not approved" in out
    code, out, _ = run_cli(capsys, "skills", "show", "files-answer")
    assert code == 0 and "What it asks for:" in out and "folders you name" in out and "Answer over my files" in out
    code, _, err = run_cli(capsys, "skills", "run", "echo")
    assert code == 1 and "not approved yet" in err and proxy.requests == []
    code, out, err = run_cli(capsys, "skills", "approve", "echo")  # not a terminal and no --yes: nothing is recorded
    assert code == 1 and "What it asks for:" in out and "Written by: Ann Example" in out and "approve only code you trust" in out
    assert "No access to your files." in out and loader.find("echo", env).state == "not-approved"
    code, out, _ = run_cli(capsys, "skills", "approve", "echo", "--yes")
    assert code == 0 and "Approved echo" in out and loader.find("echo", env).state == "ready"
    code, out, _ = run_cli(capsys, "skills", "approve", "files-answer")
    assert code == 0 and "needs no approval" in out
    code, _, err = run_cli(capsys, "skills", "show", "nope")
    assert code == 1 and "no skill named" in err
    assert run_cli(capsys, "skills")[0] == 2
    write_skill(envget(env, "MUSE_HOME"), name="broken", approve=False, muse_colour=1)
    code, out, _ = run_cli(capsys, "skills", "list")
    assert code == 1 and "cannot be loaded" in out and "muse.colour: unknown key" in out


def test_cli_run_prints_the_answer_the_grey_line_and_the_exit_codes(env, proxy, capsys):
    write_skill(envget(env, "MUSE_HOME"))
    proxy.push(*chat("GOOD answer", provider="groq"))
    code, out, err = run_cli(capsys, "skills", "run", "echo", "--input", "q=hello", "--privacy", "private", "--task", "everyday")
    assert code == 0 and out.strip() == "GOOD answer"
    assert "Answered by groq. Nothing spent." in err
    assert "What it asks for:" in err  # shown once, before first use
    assert proxy.requests[0]["task"] == "everyday" and proxy.requests[0]["privacy"] == "private"
    assert proxy.requests[0]["prompt"] == "hello"

    proxy.push(*chat("GOOD", provider="paidp", cost="0.004100"))
    code, out, err = run_cli(capsys, "skills", "run", "echo", "--pay", "0.05")
    assert code == 0 and "Answered by paidp. Cost about US$0.0041." in err and "What it asks for:" not in err

    proxy.push(*ration_429("defer:reserve", 3600, "wait:2026-11-14T00:00:00Z,handoff,pay:0.041000"))
    code, out, err = run_cli(capsys, "skills", "run", "echo")
    assert code == 4 and out == ""
    assert "wait until 2026-11-14T00:00:00Z" in err and "hand off: open your own ChatGPT" in err and "pay about US$0.0410" in err
    assert "No model was used. Nothing spent." not in err and "Nothing spent." in err

    proxy.push(*chat("weak"))
    proxy.push(*chat("weak again"))
    code, out, err = run_cli(capsys, "skills", "run", "echo")
    assert code == 4 and "NOT CHECKED" in err and "nothing else is on offer" in err and out.strip() == "weak again"

    proxy.push(*privacy_403("local"))
    code, out, err = run_cli(capsys, "skills", "run", "echo", "--privacy", "local")
    assert code == 1 and "Refused for privacy" in err

    proxy.push(*chat("GOOD"))
    code, out, _ = run_cli(capsys, "skills", "run", "echo", "--json")
    payload = json.loads(out)
    assert code == 0 and payload["status"] == "done" and payload["used"]["requests"] == 1 and payload["check"]["status"] == "passed"
    assert run_cli(capsys, "skills", "run", "echo", "--input", "novalue")[0] == 2


def test_cli_partial_exit_code_and_writes_need_yes(env, proxy, capsys, tmp_path):
    docs, out_dir = tmp_path / "docs", tmp_path / "out"
    docs.mkdir()
    (docs / "a.txt").write_text("abc")
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    args = ("skills", "run", "echo", "--folder", str(docs), "--out", str(out_dir), "--input", f"path={docs / 'a.txt'}",
            "--input", "write=copy.txt")
    code, out, err = run_cli(capsys, *args)  # not a terminal, no --yes: proposed, not written
    assert code == 0 and not out_dir.exists() and "proposed and not written" in err and "No model was used. Nothing spent." in err
    code, out, err = run_cli(capsys, *args, "--yes")
    assert code == 0 and (out_dir / "copy.txt").read_text() == "GOOD abc" and f"Wrote {out_dir.resolve() / 'copy.txt'}" in err

    write_skill(envget(env, "MUSE_HOME"), name="half", code="""
    def run(ctx):
        ctx.note("One source could not be reached.", incomplete=True)
        return "GOOD"

    def check(output, ctx):
        return []
    """)
    code, out, err = run_cli(capsys, "skills", "run", "half")
    assert code == 3 and "Note: One source could not be reached." in err


def test_the_grey_line_wording():
    from wardmuse.skills.cli import grey_line
    assert grey_line({"requests": 0}) == "No model was used. Nothing spent."
    assert grey_line({"requests": 1, "providers": {"groq": {}}, "cost_usd": 0}) == "Answered by groq. Nothing spent."
    assert grey_line({"requests": 2, "providers": {"cache": {}}, "cost_usd": 0}) == "Answered from Muse's own cache. Nothing spent."
    assert grey_line({"requests": 3, "providers": {"a": {}, "b": {}, "c": {}}, "cost_usd": 0.0123}) == "Answered by a, b and c. Cost about US$0.0123."
