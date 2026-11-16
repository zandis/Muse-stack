"""Skills checked against permissions (muse/compiler/guard.py): MUSE_PERMISSIONS=off|advise|enforce.

No network and no real provider: the scripted router of the skills tests. Everything that signs or reads a token
needs biscuit-python and skips without it; what must hold WITHOUT the library (advise changes nothing) runs everywhere.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import time
from pathlib import Path

import pytest

from wardmuse import permissions as P
from wardmuse.compiler import guard as guard_mod
from wardmuse.home import api, auth, folders
from wardmuse.skills import runner
from queue_fakes import FakeProxy  # noqa: F401
from test_skills_support import chat, env, proxy, write_skill  # noqa: F401  (fixtures)

needs_library = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")

READER = """
def run(ctx):
    target = ctx.inputs.get("path")
    text = ctx.workspace.read_text(target) if target else ""
    reply = ctx.llm.ask([{"role": "user", "content": "hi"}])
    if ctx.inputs.get("write"):
        ctx.workspace.propose_write(ctx.inputs["write"], "GOOD " + text, "a copy")
    return "GOOD " + str(len(text)) + " " + reply.text


def check(output, ctx):
    return [] if output.startswith("GOOD") else ["no"]
"""
READS = {"muse_needs": {"files.read": ["chosen"], "files.write": ["output"], "net": []}, "muse_writes": True}
CANARY = "CANARY-GUARD-91c2"


def grant(env, *, actions, resources, limits=(), privacy="private", confirm=None, holder="helper:home", routine=(), days=1):
    from wardmuse.compiler.core import Compiler

    c = Compiler(env)
    now = int(time.time())
    d = P.propose_from_fields(P.Draft(
        issuer=c.ensure_root(), holder=holder, actions=tuple(actions), routine=tuple(routine),
        resources=tuple(P.Resource(k, v) for k, v in resources), limits=tuple(limits), issued_at=now, not_before=now - 60,
        not_after=now + days * 86400, confirm=confirm or P.EACH_WRITE, privacy=privacy, period_zone="UTC"))
    mandate, _ = c.issue(d)
    return c, mandate


def folders_for(tmp_path):
    docs, other, out = tmp_path / "docs", tmp_path / "other", tmp_path / "out"
    docs.mkdir(), other.mkdir(), out.mkdir()
    (docs / "a.txt").write_text("abc")
    (other / "b.txt").write_text(CANARY)
    return Path(os.path.realpath(docs)), Path(os.path.realpath(other)), Path(os.path.realpath(out))


class Records:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict]] = []

    def __call__(self, action, summary, **fields):
        self.rows.append((action, summary, fields))
        return {"action": action}


# ================================================================ modes (no library needed)


def test_the_setting_has_three_values_and_defaults_to_advise():
    assert guard_mod.normalise_mode(None) == "advise" and guard_mod.normalise_mode(" ENFORCE ") == "enforce"
    with pytest.raises(ValueError, match="MUSE_PERMISSIONS must be one of off, advise, enforce"):
        guard_mod.normalise_mode("strict")


def test_a_skill_that_does_not_ship_with_muse_is_always_enforced(monkeypatch):
    monkeypatch.delenv("MUSE_SANDBOX_UNSAFE_HOME_SKILLS", raising=False)
    for setting in ("off", "advise", "enforce", None):
        env = {} if setting is None else {"MUSE_PERMISSIONS": setting}
        assert guard_mod.mode_for("home", env) == "enforce"
        assert guard_mod.mode_for("shipped", env) == (setting or "advise")


def test_advise_without_permissions_set_up_changes_nothing(env, proxy, tmp_path):
    docs, _, out = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    result = runner.run("echo", {"path": str(docs / "a.txt"), "write": "c.txt"}, folders=[docs], out_dir=out, environ=env)
    assert result.status == "done" and len(result.writes) == 1 and result.permissions is None
    assert "permissions" not in result.public()


def test_an_unknown_setting_fails_the_run_with_a_plain_reason(env, proxy):
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    result = runner.run("echo", {}, environ={**env, "MUSE_PERMISSIONS": "loose"})
    assert result.status == "failed" and "MUSE_PERMISSIONS must be one of" in result.notes[0] and proxy.requests == []


# ================================================================ enforce


@needs_library
def test_enforce_without_any_permission_refuses_before_anything_is_read_or_sent(env, proxy, tmp_path):
    docs, _, _ = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    result = runner.run("echo", {"path": str(docs / "a.txt")}, folders=[docs], environ={**env, "MUSE_PERMISSIONS": "enforce"})
    assert result.status == "failed" and proxy.requests == []
    assert "Stopped before it started" in result.notes[0] and "no permission in force covers this skill" in result.notes[0]


@needs_library
def test_enforce_allows_what_the_permission_names_and_refuses_an_ungranted_folder(env, proxy, tmp_path):
    docs, other, _ = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    grant(env, actions=("files.read", "llm.call"), resources=(("folder", str(docs)), ("provider", "router"), ("home", "skill/echo")), privacy="standard")
    run_env = {**env, "MUSE_PERMISSIONS": "enforce"}
    seen = Records()
    good = runner.run("echo", {"path": str(docs / "a.txt")}, folders=[docs], environ=run_env, permission_record=seen)
    assert good.status == "done", good.notes
    assert good.output.startswith("GOOD 3") and len(proxy.requests) == 1
    assert good.permissions["mode"] == "enforce" and not good.permissions["blocked"]
    bad = runner.run("echo", {"path": str(other / "b.txt")}, folders=[other], environ=run_env, permission_record=seen)
    assert bad.status == "failed" and len(proxy.requests) == 1  # nothing more was sent
    assert "not allowed to read a folder: the permission does not cover this folder or service" in bad.notes[0]
    assert CANARY not in json.dumps(bad.public(), default=str)
    assert [(a, f["outcome"]) for a, _, f in seen.rows] == [("mandate.check", "declined")]
    assert seen.rows[0][1].startswith("Refused the skill echo when it went to read a folder")
    # A permission that names another skill does not cover this one.
    write_skill(envget(env, "MUSE_HOME"), name="other", code=READER, **READS)
    assert runner.run("other", {"path": str(docs / "a.txt")}, folders=[docs], environ=run_env).status == "failed"


@needs_library
def test_a_model_call_above_the_permissions_privacy_class_is_refused_before_it_is_sent(env, proxy):
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)  # the skill's default class is `standard`
    grant(env, actions=("llm.call",), resources=(("provider", "router"),), privacy="private")
    run_env = {**env, "MUSE_PERMISSIONS": "enforce"}
    result = runner.run("echo", {}, environ=run_env)
    assert result.status == "failed" and proxy.requests == []
    assert any("does not allow data to be sent under this privacy class" in n for n in result.notes)
    assert runner.run("echo", {}, privacy="private", environ=run_env).status == "done" and len(proxy.requests) == 1


@needs_library
def test_a_paid_call_over_the_limit_is_refused_before_it_is_sent_and_usage_is_counted(env, proxy):
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    _, m = grant(env, actions=("llm.call", "llm.paid"), resources=(("provider", "router"),), privacy="standard",
                 limits=(P.Limit("spend", "USD", 2, "day", 10),), confirm=P.Confirm("over_amount", "USD", 2, 100))
    run_env = {**env, "MUSE_PERMISSIONS": "enforce"}
    over = runner.run("echo", {}, budget={"paid_usd": 0.5}, environ=run_env)  # could cost 50 cents; 10 a day allowed
    assert over.status == "failed" and proxy.requests == []
    assert any("would go over the permission's limit" in n for n in over.notes)
    proxy.push(*chat("GOOD", provider="paidp", cost="0.040000"))
    ok = runner.run("echo", {}, budget={"paid_usd": 0.05}, environ=run_env)
    assert ok.status == "done" and proxy.requests[0]["headers"]["x-muse-allow-paid"] == "true"
    from wardmuse.compiler.core import Compiler

    assert Compiler(env).usage(m)[("spend", "USD", "day")] == 4  # four cents, from the router's cost header
    again = runner.run("echo", {}, budget={"paid_usd": 0.07}, environ=run_env)  # 4 used + up to 7 > 10
    assert again.status == "failed" and len(proxy.requests) == 1


@needs_library
def test_a_paid_call_that_needs_approval_asks_and_spends_the_approval_once(env, proxy):
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    grant(env, actions=("llm.call", "llm.paid"), resources=(("provider", "router"),), privacy="standard",
          limits=(P.Limit("spend", "USD", 2, "day", 100),))  # each_write: every payment is asked about
    run_env = {**env, "MUSE_PERMISSIONS": "enforce"}
    assert runner.run("echo", {}, budget={"paid_usd": 0.05}, environ=run_env).status == "failed"  # nobody to ask
    assert proxy.requests == []
    asked = []
    result = runner.run("echo", {}, budget={"paid_usd": 0.05}, environ=run_env,
                        permission_confirm=lambda action, facts: asked.append((action, facts["paid_usd"])) or "card-pay-1")
    assert result.status == "done" and asked == [("llm.paid", 0.05)] and len(proxy.requests) == 1
    # The same card id again: the single-use token it became is spent.
    replay = runner.run("echo", {}, budget={"paid_usd": 0.05}, environ=run_env, permission_confirm=lambda a, f: "card-pay-1")
    assert replay.status == "failed" and len(proxy.requests) == 1


# ================================================================ advise computes the same, blocks nothing


@needs_library
def test_advise_computes_the_same_decisions_records_them_and_blocks_nothing(env, proxy, tmp_path):
    docs, other, _ = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    grant(env, actions=("files.read", "llm.call"), resources=(("folder", str(docs)), ("provider", "router")))
    seen = Records()
    result = runner.run("echo", {"path": str(other / "b.txt")}, folders=[other], environ={**env, "MUSE_PERMISSIONS": "advise"},
                        permission_record=seen)
    assert result.status == "done"  # not blocked
    decisions = {(d["action"], d["decision"], d["reason"], d["blocked"]) for d in result.permissions["decisions"]}
    assert ("files.read", "deny", "resource_not_allowed", False) in decisions
    assert ("llm.call", "deny", "privacy_not_allowed", False) in decisions  # standard > private: the same as enforce says
    assert all(f["outcome"] == "ok" for _, _, f in seen.rows) and seen.rows
    assert seen.rows[0][1].startswith("Advice only, not blocked")
    events = [e for e in result.events if e["kind"] == "permission"]
    assert events and all(e["mode"] == "advise" for e in events)


# ================================================================ the sandbox's authorize hook


@needs_library
def test_the_sandbox_asks_the_same_check_before_it_does_what_a_skill_asked(env, proxy, monkeypatch):
    from wardmuse import sandbox

    if not sandbox.probe().can_require:
        pytest.skip("this machine cannot isolate a skill")
    write_skill(envget(env, "MUSE_HOME"), code=READER, **READS)
    grant(env, actions=("llm.call",), resources=(("provider", "router"),), privacy="private")
    calls = []
    real = guard_mod.Guard.sandbox_authorize

    def spy(self, default_privacy):
        inner = real(self, default_privacy)

        def authorize(action, facts, token):
            calls.append(action)
            return inner(action, facts, token)

        return authorize

    monkeypatch.setattr(guard_mod.Guard, "sandbox_authorize", spy)
    run_env = {**env, "MUSE_PERMISSIONS": "enforce"}
    refused = runner.run("echo", {}, environ=run_env, sandbox="require")
    assert refused.sandbox["isolated"] is True and calls == ["llm.ask"] and proxy.requests == []
    assert refused.status in ("failed", "partial")
    calls.clear()
    allowed = runner.run("echo", {}, privacy="private", environ=run_env, sandbox="require")
    assert allowed.status == "done" and calls == ["llm.ask"] and len(proxy.requests) == 1


@needs_library
def test_the_authorize_hook_refuses_with_a_permission_error_under_enforce(env):
    grant(env, actions=("llm.call",), resources=(("provider", "router"),), privacy="private")
    g = guard_mod.Guard("echo", "shipped", {**env, "MUSE_PERMISSIONS": "enforce"})
    authorize = g.sandbox_authorize("standard")  # the run's class; a step may only make it stricter
    authorize("llm.ask", {"privacy": "private"}, None)
    with pytest.raises(PermissionError, match="privacy class"):
        authorize("llm.ask", {"privacy": None}, None)
    advise = guard_mod.Guard("echo", "shipped", {**env, "MUSE_PERMISSIONS": "advise"}).sandbox_authorize("standard")
    advise("llm.ask", {"privacy": None}, None)  # computed, not blocked


# ================================================================ write -> card -> one write, through the home


OWNER = auth.Device("mei-phone", role="owner")
HOST = auth.Device("bot-tools", role="host")


def _call(home, who, method, path, body=None):
    resp = api.dispatch(home, who, method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


@needs_library
def test_a_write_under_enforce_becomes_a_card_and_is_applied_once_after_approval(env, proxy, tmp_path, monkeypatch):
    docs, _, out = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), name="writer", code=READER, **READS)
    folders.add("notes", docs)
    folders.add("reports", out, output=True)
    monkeypatch.setenv("MUSE_PERMISSIONS", "enforce")
    c, m = grant(env, actions=("files.read", "files.write", "llm.call"), privacy="standard",
                 resources=(("folder", str(docs)), ("folder", str(out)), ("provider", "router")))
    home = api.Home(proxy_running=lambda: True)
    status, started = _call(home, HOST, "POST", "/v1/skills/writer/run",
                            {"inputs": {"path": str(docs / "a.txt"), "write": "copy.txt"}, "folders": ["notes"], "out": "reports"})
    assert status == 202, started
    home.skills.wait(started["run"]["id"])
    run = _call(home, HOST, "GET", f"/v1/skills/runs/{started['run']['id']}")[1]["run"]
    assert run["status"] == "done" and len(run["writes"]) == 1, run
    card_id = run["writes"][0]["card_id"]
    approval = c.table.approval(card_id)
    assert approval["mandate_id"] == m.id and approval["mode"] == "enforce" and approval["state"] == "pending"
    status, reply = _call(home, OWNER, "POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"})
    assert status == 200 and reply["write"]["state"] == "written" and (out / "copy.txt").read_text() == "GOOD abc"
    assert c.table.approval(card_id)["state"] == "used"
    last = home.records().records()[-1]
    assert last["action"] == "files.write" and last["mandate"] == m.id
    # Replay: the approval was spent, at the table and in the spent set.
    from wardmuse.compiler.guard import settle_write

    assert settle_write(env, card_id)["reason"] == "already_used"
    parent = c.store.load(approval["token_name"], c.keys.public())
    request = P.Request(holder="helper:home", action="files.write", resource=P.Resource("folder", str(out / "copy.txt")),
                        privacy="standard", job=approval["job"])
    assert c.use_approval(parent, request, card_id).detail == "already_used"


@needs_library
def test_an_approved_write_is_refused_once_the_permission_was_stopped(env, proxy, tmp_path, monkeypatch):
    docs, _, out = folders_for(tmp_path)
    write_skill(envget(env, "MUSE_HOME"), name="writer", code=READER, **READS)
    folders.add("notes", docs)
    folders.add("reports", out, output=True)
    monkeypatch.setenv("MUSE_PERMISSIONS", "enforce")
    c, m = grant(env, actions=("files.read", "files.write", "llm.call"), privacy="standard",
                 resources=(("folder", str(docs)), ("folder", str(out)), ("provider", "router")))
    home = api.Home(proxy_running=lambda: True)
    started = _call(home, HOST, "POST", "/v1/skills/writer/run",
                    {"inputs": {"path": str(docs / "a.txt"), "write": "copy.txt"}, "folders": ["notes"], "out": "reports"})[1]
    home.skills.wait(started["run"]["id"])
    card_id = _call(home, HOST, "GET", f"/v1/skills/runs/{started['run']['id']}")[1]["run"]["writes"][0]["card_id"]
    c.revoke(m.id)
    status, reply = _call(home, OWNER, "POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"})
    assert status == 200 and reply["write"]["state"] == "refused" and "no longer allow it" in reply["write"]["reason"]
    assert not (out / "copy.txt").exists()
    assert c.table.approval(card_id)["state"] == "revoked"
