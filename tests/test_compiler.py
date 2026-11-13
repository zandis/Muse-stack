"""muse.compiler: plan, apply, status, revoke, sweep; the invariants; the router's gate.

Everything that signs or reads a token needs the optional biscuit-python and skips without it.
The gate (what the proxy runs) and the app registry's new fields need nothing extra and run everywhere.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from muse import apps, auth
from muse import permissions as P
from muse.compiler import gate
from muse.compiler.plan import TARGETS, app_name, plan
from muse.home import folders

needs_library = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")

T0 = 1_794_528_000  # 2026-11-13T00:00:00Z
DAY = 86_400
MID = "m1_" + "ab" * 32


class Clock:
    def __init__(self, now: float = T0 + 100) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def home(_isolated_muse_home) -> dict[str, str]:
    return {"MUSE_HOME": str(_isolated_muse_home)}


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def compiler(home, clock):
    from muse.compiler.core import Compiler

    c = Compiler(home, clock=clock)
    c.ensure_root()
    return c


def draft(compiler, **changes):
    fields = dict(
        issuer=compiler.root_public(), holder="helper:home", actions=("files.read", "llm.call"),
        resources=(P.Resource("provider", "router"),), issued_at=T0, not_before=T0, not_after=T0 + 30 * DAY,
        confirm=P.EACH_WRITE, privacy="private", period_zone="UTC")
    fields.update(changes)
    return P.propose_from_fields(P.Draft(**fields))


def app_draft(compiler, **changes):
    fields = dict(holder="app:reader", actions=("llm.call",), limits=(P.Limit("requests", "", 0, "day", 50),))
    fields.update(changes)
    return draft(compiler, **fields)


def states(targets) -> dict[str, str]:
    return {t: s["state"] for t, s in targets.items()}


# ================================================================ the gate and the app registry (no library needed)


def test_an_app_entry_can_carry_its_permission_and_survives_updates(home):
    app, token = apps.add_app("perm-abc", "normal", environ=home, mandate=MID, valid_from=T0, valid_until=T0 + DAY)
    assert (app.mandate, app.valid_from, app.valid_until) == (MID, T0, T0 + DAY)
    assert apps.load_apps(home)["perm-abc"].mandate == MID and apps.find_by_token(token, home).mandate == MID
    assert apps.update_app("perm-abc", home, daily_requests=5).mandate == MID  # a settings change cannot drop it
    assert apps.rotate_token("perm-abc", home)[0].mandate == MID
    with pytest.raises(apps.AppError):
        apps.update_app("perm-abc", home, mandate=None)  # nor can it be changed by hand through the API's function
    assert app.policy()["muse_mandate"] == MID and "muse_mandate" not in apps.OWNER.policy()
    assert apps.remove_app("perm-abc", home, only_mandate="m1_" + "cd" * 32) is False  # not this permission's: left alone
    assert apps.remove_app("perm-abc", home, only_mandate=MID) is True


def test_an_entry_made_from_a_permission_without_an_end_is_not_loaded(home, caplog):
    path = apps.apps_path(home)
    path.write_text("version: 1\napps:\n- name: perm-x\n  priority: normal\n  token_sha256: " + "0" * 64 + f"\n  mandate: {MID}\n")
    with caplog.at_level(logging.WARNING):
        assert apps.load_apps(home) == {}  # it would never expire: refused rather than trusted


def test_bind_mandate_ties_an_existing_app_and_refuses_one_that_belongs_elsewhere(home):
    _, token = apps.add_app("bot", "interactive", environ=home)
    bound = apps.bind_mandate("bot", MID, T0, T0 + DAY, home, daily_requests=9, allow_paid=False)
    assert bound.mandate == MID and bound.daily_requests == 9 and apps.find_by_token(token, home).mandate == MID
    with pytest.raises(apps.AppError, match="another permission"):
        apps.bind_mandate("bot", "m1_" + "cd" * 32, T0, T0 + DAY, home)


def test_the_gate_refuses_after_the_end_before_the_start_and_once_stopped(home):
    app, _ = apps.add_app("perm-abc", "normal", environ=home, mandate=MID, valid_from=T0, valid_until=T0 + DAY)
    assert gate.refusal(app, home, T0 + 5) is None
    assert gate.refusal(app, home, T0 - 5) == gate.NOT_YET
    assert gate.refusal(app, home, T0 + DAY + 1) == gate.EXPIRED
    gate.mark_stopped([MID], home, T0 + 10)
    assert gate.refusal(app, home, T0 + 11) == gate.STOPPED
    assert stat.S_IMODE(gate.stopped_path(home).stat().st_mode) == 0o600
    gate.mark_stopped(["m1_" + "cd" * 32], home)  # the list only grows
    assert gate.stopped_ids(home) == {MID, "m1_" + "cd" * 32}
    plain, _ = apps.add_app("plain", "normal", environ=home)
    assert gate.refusal(plain, home) is None  # an app with no permission is not the gate's business


def test_a_stopped_list_that_cannot_be_read_refuses_rather_than_un_stops(home):
    app, _ = apps.add_app("perm-abc", "normal", environ=home, mandate=MID, valid_from=T0, valid_until=T0 + DAY)
    gate.stopped_path(home).parent.mkdir(parents=True, exist_ok=True)
    gate.stopped_path(home).write_text("{not json")
    assert gate.stopped_ids(home) is None and gate.refusal(app, home, T0 + 5) == gate.UNREADABLE
    gate.mark_stopped([], home)  # the sweep rewrites it from what it knows
    assert gate.refusal(app, home, T0 + 5) is None


def test_proxy_auth_refuses_an_app_whose_permission_was_stopped_or_ended(home):
    app, token = apps.add_app("perm-abc", "normal", environ=home, mandate=MID, valid_from=T0, valid_until=T0 + DAY,
                              default_privacy="private")
    ok = auth.authenticate(token, None, master_key="sk-master", environ=home, now=T0 + 5)
    assert ok.app.name == "perm-abc" and ok.via == "app"
    with pytest.raises(auth.AuthError) as ended:
        auth.authenticate(token, None, master_key="sk-master", environ=home, now=T0 + DAY + 5)
    assert ended.value.status == 403 and "permission expired" in ended.value.message and "has ended" in ended.value.message
    gate.mark_stopped([MID], home)
    for kwargs in ({"token": token, "header_app": None}, {"token": "sk-master", "header_app": "perm-abc"}):
        with pytest.raises(auth.AuthError) as stopped:  # also when the master key selects that app
            auth.authenticate(kwargs["token"], kwargs["header_app"], master_key="sk-master", environ=home, now=T0 + 5)
        assert stopped.value.status == 403 and "was stopped" in stopped.value.message
        assert token not in stopped.value.message and MID not in stopped.value.message


def test_proxy_auth_holds_a_permission_app_to_its_privacy_class(home):
    _, token = apps.add_app("perm-abc", "normal", environ=home, mandate=MID, valid_from=T0, valid_until=T0 + DAY,
                            default_privacy="private")
    for asked in (None, "private", "local", "nonsense"):  # stricter, the same, or not a class (the hook answers that)
        assert auth.authenticate(token, None, master_key="k", environ=home, now=T0 + 5, privacy=asked).app.name == "perm-abc"
    for asked in ("standard", "public", " Public "):
        with pytest.raises(auth.AuthError) as wider:
            auth.authenticate(token, None, master_key="k", environ=home, now=T0 + 5, privacy=asked)
        assert wider.value.status == 403 and "'private' class at most" in wider.value.message
    _, plain = apps.add_app("plain", "normal", environ=home, default_privacy="private")
    assert auth.authenticate(plain, None, master_key="k", environ=home, privacy="public").app.name == "plain"  # as before


def test_folder_entries_carry_their_permission(home, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    entry = folders.add("perm-ab-0", notes, environ=home, mandate=MID)
    assert entry["mandate"] == MID and folders.load(home)["perm-ab-0"]["mandate"] == MID
    folders.add("mine", tmp_path, environ=home)
    assert "mandate" not in folders.load(home)["mine"]
    assert folders.load(home)["perm-ab-0"]["mandate"] == MID  # kept when the file is rewritten for another entry
    assert folders.remove("mine", home, only_mandate=MID) is False and folders.remove("perm-ab-0", home, only_mandate=MID) is True


# ================================================================ plan (pure)


@needs_library
def test_plan_for_a_helper_names_tokens_folders_and_the_consent_record(compiler, tmp_path):
    m = P.issue(draft(compiler, actions=("files.read", "files.write", "llm.call"),
                      resources=(P.Resource("folder", "/data/notes"), P.Resource("folder", "/data/out"),
                                 P.Resource("home", "skill/files-answer"), P.Resource("provider", "router"))), compiler.keys.load())
    specs = plan(m)
    assert [(s.target, s.kind, s.key) for s in specs] == [
        ("skills", "skill_token", "skill:files-answer"), ("skills", "folder", "folder:/data/notes"),
        ("skills", "folder", "folder:/data/out"), ("records", "consent_record", "consent")]
    assert all(s.mandate_id == m.id for s in specs)
    assert specs[1].params == {"path": "/data/notes", "output": True, "name": f"perm-{m.id[3:11]}-0"}
    assert plan(m) == specs  # the same token gives the same plan


@needs_library
def test_plan_for_an_app_or_a_device_is_a_router_entry_with_the_permissions_caps(compiler):
    root = compiler.keys.load()
    m = P.issue(app_draft(compiler, actions=("llm.call", "llm.paid"), limits=(P.Limit("requests", "", 0, "day", 50),
                                                                              P.Limit("spend", "USD", 2, "day", 200)),
                          purpose_code="router.priority.background", privacy="standard"), root)
    (spec,) = [s for s in plan(m) if s.target == "router"]
    assert spec.kind == "app" and spec.params == {
        "name": app_name(m.id), "priority": "background", "daily_requests": 50, "daily_tokens": None, "allow_paid": True,
        "default_privacy": "standard", "valid_from": T0, "valid_until": T0 + 30 * DAY}
    assert "does not hold it to this permission's own amount" in spec.reason  # said, not hidden
    plain = P.issue(app_draft(compiler, holder="device:bot-tools", limits=()), root)
    (spec,) = [s for s in plan(plain) if s.target == "router"]
    assert (spec.params["priority"], spec.params["daily_requests"], spec.params["allow_paid"]) == ("normal", None, False)
    helper = P.issue(draft(compiler), root)
    assert not [s for s in plan(helper) if s.target == "router"]  # the helper uses the skills apps, not one of its own


@needs_library
def test_targets_that_are_not_built_are_planned_as_not_built_never_left_out(compiler):
    m = P.issue(draft(compiler, holder="helper:home", actions=("pay.spend", "mail.read", "health.read", "files.read"),
                      resources=(P.Resource("folder", "*"), P.Resource("host", "api.example.com")),
                      limits=(P.Limit("spend", "TWD", 2, "day", 2000),)), compiler.keys.load())
    by_target = {s.target: s for s in plan(m) if s.status == "not_built"}
    assert set(by_target) == {"payments", "connectors", "health"} and set(by_target) <= set(TARGETS)
    assert all(s.reason and "not built" in s.reason for s in by_target.values())
    (any_folder,) = [s for s in plan(m) if s.key == "folder:*"]
    assert any_folder.status == "pending" and "name the folders" in any_folder.reason


# ================================================================ apply, status


@needs_library
def test_issue_makes_the_router_app_and_returns_its_key_exactly_once(compiler, home):
    mandate, applied = compiler.issue(app_draft(compiler))
    assert states(applied.targets) == {"router": "active", "records": "active"}
    assert applied.app_name == app_name(mandate.id) and applied.app_token.startswith(apps.TOKEN_PREFIX)
    app = apps.find_by_token(applied.app_token, home)
    assert (app.name, app.mandate, app.daily_requests, app.allow_paid, app.default_privacy, app.valid_until) == (
        applied.app_name, mandate.id, 50, False, "private", T0 + 30 * DAY)
    again = compiler.compile(mandate)  # idempotent: nothing new, and no key the second time
    assert again.app_token is None and states(again.targets) == states(applied.targets)
    assert list(apps.load_apps(home)) == [applied.app_name]
    row = compiler.table.get(mandate.id, "router", "app")
    assert row["artifact_id"] == applied.app_name and row["state"] == "active"
    # The key is nowhere on disk: not in the table, not in the registry, not in the token files.
    for path in Path(home["MUSE_HOME"]).rglob("*"):
        if path.is_file():
            assert applied.app_token.encode() not in path.read_bytes(), path.name
    assert stat.S_IMODE((Path(home["MUSE_HOME"]) / "compiler.sqlite").stat().st_mode) == 0o600


@needs_library
def test_issue_for_the_helper_makes_tokens_folders_and_a_consent_record(compiler, home, tmp_path):
    notes, out = tmp_path / "notes", tmp_path / "out"
    notes.mkdir(), out.mkdir()
    mandate, applied = compiler.issue(draft(
        compiler, actions=("files.read", "files.write", "llm.call"),
        resources=(P.Resource("folder", str(notes)), P.Resource("folder", str(out)), P.Resource("home", "skill/files-answer"),
                   P.Resource("provider", "router"))))
    assert states(applied.targets) == {"skills": "active", "records": "active"} and applied.app_token is None
    listed = folders.load(home)
    assert {e["path"] for e in listed.values()} == {str(notes), str(out)}
    assert all(e["mandate"] == mandate.id and e["output"] is True for e in listed.values())
    name, token = compiler.skill_token(mandate, "files-answer")
    assert token.id == mandate.id and token.jobs() == ("skill.files-answer",) and name in compiler.store.names()
    assert compiler.skill_token(mandate, "files-answer")[0] == name  # made once, then read back
    from muse.records.home import HomeRecords

    records = HomeRecords(home).records()
    assert [(r["action"], r["mandate"], r["actor"], r["egress"]) for r in records] == [("mandate.issue", mandate.id, "person", "none")]
    from muse.records import format as fmt

    assert compiler.table.get(mandate.id, "records", "consent")["artifact_id"] == fmt.record_hash(records[0])
    assert "Gave a permission to the helper home: 3 actions" in records[0]["summary"]


@needs_library
def test_a_folder_that_is_not_there_is_pending_with_a_reason_and_the_sweep_adds_it_later(compiler, home, tmp_path):
    later = tmp_path / "later"
    mandate, applied = compiler.issue(draft(compiler, resources=(P.Resource("folder", str(later)), P.Resource("provider", "router"))))
    assert applied.targets["skills"]["state"] == "pending" and "could not be put on the list" in applied.targets["skills"]["reason"]
    assert folders.load(home) == {}
    later.mkdir()
    result = compiler.sweep()
    assert result["applied"] == [mandate.id] and result["failed"] == 0
    assert compiler.status(mandate.id)["skills"] == {"state": "active", "reason": ""}
    assert [e["mandate"] for e in folders.load(home).values()] == [mandate.id]


@needs_library
def test_not_built_targets_show_as_not_built_with_a_reason(compiler):
    mandate, applied = compiler.issue(draft(compiler, actions=("files.read", "pay.spend"),
                                            resources=(P.Resource("host", "api.example.com"), P.Resource("provider", "router")),
                                            limits=(P.Limit("spend", "TWD", 2, "day", 2000),)))
    assert applied.targets["payments"]["state"] == "not_built" and "not built yet" in applied.targets["payments"]["reason"]
    assert applied.targets["skills"]["state"] == "active" and applied.targets["records"]["state"] == "active"


@needs_library
def test_a_failed_record_is_reported_and_retried_by_the_sweep(home, clock):
    from muse.compiler.core import Compiler

    broken = Compiler(home, clock=clock, record=lambda *a, **k: None)  # as when `cryptography` is missing
    broken.ensure_root()
    mandate, applied = broken.issue(app_draft(broken))
    assert applied.targets["records"]["state"] == "failed" and "consent record could not be written" in applied.targets["records"]["reason"]
    assert applied.targets["router"]["state"] == "active"  # one target failing does not stop the others
    working = Compiler(home, clock=clock)
    assert working.sweep()["applied"] == [mandate.id]
    assert working.status(mandate.id)["records"]["state"] == "active"


@needs_library
def test_an_existing_host_app_is_adopted_not_duplicated(compiler, home):
    _, key = apps.add_app("openbot", "interactive", environ=home, note="host bot")
    from muse.compiler.hosts import default_draft

    mandate, applied = compiler.issue(default_draft(compiler.root_public(), "openbot-tools", now=T0, daily_requests=40),
                                      adopt_app="openbot")
    assert applied.app_token is None and applied.app_name == "openbot" and applied.targets["router"]["state"] == "active"
    app = apps.find_by_token(key, home)  # the key the host already has keeps working, now tied to the permission
    assert (app.name, app.mandate, app.daily_requests, app.allow_paid) == ("openbot", mandate.id, 40, False)
    assert list(apps.load_apps(home)) == ["openbot"]
    compiler.revoke(mandate.id)
    assert apps.find_by_token(key, home) is None  # stopping the permission stops the host's key


# ================================================================ revoke


@needs_library
def test_revoke_removes_everything_and_writes_the_revocation_first(compiler, home, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    helper, _ = compiler.issue(draft(compiler, resources=(P.Resource("folder", str(notes)), P.Resource("provider", "router"))))
    compiler.skill_token(helper, "files-answer")
    app, applied = compiler.issue(app_draft(compiler))
    order: list[str] = []
    compiler.fault = order.append
    for mandate in (helper, app):
        targets = compiler.revoke(mandate.id)
        assert set(states(targets).values()) == {"revoked"}, targets
        assert compiler.table.rows(mandate.id) == []
    assert order[0] == "revocation_recorded" and order[1] == "router_told" and all(s.startswith("remove_") for s in order[2:4])
    assert apps.load_apps(home) == {} and folders.load(home) == {}
    assert apps.find_by_token(applied.app_token, home) is None
    assert sorted(compiler.store.names()) == sorted([helper.id[3:15], app.id[3:15]])  # the permissions stay, as history
    assert gate.stopped_ids(home) == {helper.id, app.id}
    revoked = compiler.revocations()
    assert all(revoked.contains(P.revocation_ids(m)[0]) for m in (helper, app))
    from muse.records.home import HomeRecords

    actions = [(r["action"], r["mandate"]) for r in HomeRecords(home).records()]
    assert actions == [("mandate.issue", helper.id), ("mandate.issue", app.id), ("mandate.revoke", helper.id), ("mandate.revoke", app.id)]
    assert compiler.orphans() == []
    compiler.fault = None
    assert set(states(compiler.revoke(app.id)).values()) == {"revoked"}  # idempotent: no error, no second record
    assert len(HomeRecords(home).records()) == 4 and len(compiler.revocations()) == 2


@needs_library
def test_a_crash_between_revocation_and_removal_is_finished_by_the_sweep(compiler, home):
    mandate, applied = compiler.issue(app_draft(compiler))

    def crash(step: str) -> None:
        if step == "revocation_recorded":
            raise KeyboardInterrupt("the process died here")

    compiler.fault = crash
    with pytest.raises(KeyboardInterrupt):
        compiler.revoke(mandate.id)
    compiler.fault = None
    # The revocation is on the signed list, so every token check already refuses...
    assert compiler.state_of(mandate) == "stopped"
    # ...but the artifact is still there, and the router has not been told.
    assert applied.app_name in apps.load_apps(home) and mandate.id not in gate.stopped_ids(home)
    assert compiler.status(mandate.id)["router"]["state"] == "pending"  # shown as still being removed, never as active
    assert compiler.orphans() != []
    result = compiler.sweep()
    assert result["removed"] == [mandate.id] and result["failed"] == 0
    assert apps.load_apps(home) == {} and mandate.id in gate.stopped_ids(home)
    assert compiler.table.rows(mandate.id) == [] and compiler.orphans() == []
    assert states(compiler.status(mandate.id)) == {"router": "revoked", "records": "revoked"}
    assert compiler.sweep() == {"removed": [], "applied": [], "failed": 0, "skipped": ""}  # nothing left to do


@needs_library
def test_a_removal_that_fails_is_reported_per_target_and_retried(compiler, home, monkeypatch):
    mandate, applied = compiler.issue(app_draft(compiler))
    real = apps.remove_app

    def refuse(*args, **kwargs):
        raise OSError("disk full: /secret/path")

    monkeypatch.setattr(apps, "remove_app", refuse)
    targets = compiler.revoke(mandate.id)
    assert targets["router"] == {"state": "failed", "reason": "This could not be removed yet. The sweep will try again."}
    assert targets["records"]["state"] == "revoked"  # the others went ahead
    row = compiler.table.get(mandate.id, "router", "app")
    assert row["error"] == "OSError" and "secret" not in json.dumps(row)  # the type of the error only
    # Meanwhile the router already refuses the key: the gate was told before anything was removed.
    assert gate.refusal(apps.find_by_token(applied.app_token, home), home, compiler.now()) == gate.STOPPED
    monkeypatch.setattr(apps, "remove_app", real)
    assert compiler.sweep()["failed"] == 0 and apps.load_apps(home) == {}
    assert compiler.status(mandate.id)["router"]["state"] == "revoked"


@needs_library
def test_a_crash_between_noting_an_artifact_and_making_it_leaves_nothing_unknown(compiler, home):
    def crash(step: str) -> None:
        if step == "app_row_written":
            raise KeyboardInterrupt

    compiler.fault = crash
    d = app_draft(compiler)
    with pytest.raises(KeyboardInterrupt):
        compiler.issue(d)
    compiler.fault = None
    mandate = compiler.mandates()[0]  # the token was stored; the row says `pending`; no app exists
    assert apps.load_apps(home) == {} and compiler.table.get(mandate.id, "router", "app")["state"] == "pending"
    assert compiler.status(mandate.id)["router"]["state"] == "pending"
    compiler.sweep()  # the sweep never mints a key nobody would see: it says so instead
    router = compiler.status(mandate.id)["router"]
    assert router["state"] == "failed" and "give it again" in router["reason"]
    assert compiler.compile(mandate).app_token  # an explicit compile by the person does make it


# ================================================================ sweep and the invariants


@needs_library
def test_the_sweep_removes_what_an_expired_permission_left(compiler, home, clock):
    mandate, applied = compiler.issue(app_draft(compiler, not_after=T0 + DAY))
    assert compiler.sweep()["removed"] == []
    clock.now = T0 + DAY + 61
    assert compiler.state_of(mandate) == "expired"
    assert gate.refusal(apps.find_by_token(applied.app_token, home), home, clock.now) == gate.EXPIRED  # refused before any sweep
    result = compiler.sweep()
    assert result["removed"] == [mandate.id] and apps.load_apps(home) == {} and compiler.table.rows() == []
    from muse.records.home import HomeRecords

    last = HomeRecords(home).records()[-1]
    assert (last["action"], last["actor"], last["mandate"]) == ("mandate.revoke", "system", mandate.id) and "time ran out" in last["summary"]


@needs_library
def test_the_sweep_removes_artifacts_whose_permission_is_gone_or_was_never_noted(compiler, home, tmp_path):
    mandate, applied = compiler.issue(app_draft(compiler))
    compiler.store.remove(mandate.id[3:15])  # the token file is deleted by hand
    assert any("unknown permission" in line for line in compiler.orphans())
    # And two artifacts with no row at all, naming a permission that does not exist.
    ghost = "m1_" + "ee" * 32
    apps.add_app("perm-ghost", "normal", environ=home, mandate=ghost, valid_from=T0, valid_until=T0 + 90 * DAY)
    folders.add("perm-ghost-0", tmp_path, environ=home, mandate=ghost)
    apps.add_app("by-hand", "normal", environ=home)
    folders.add("by-hand", tmp_path, environ=home)
    result = compiler.sweep()
    assert set(result["removed"]) == {mandate.id, ghost}
    assert list(apps.load_apps(home)) == ["by-hand"] and list(folders.load(home)) == ["by-hand"]  # the person's own stay
    assert compiler.table.rows() == [] and compiler.orphans() == []


@needs_library
def test_invariants_hold_through_a_life_of_grants_and_stops(compiler, home, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    made = [compiler.issue(app_draft(compiler))[0],
            compiler.issue(draft(compiler, resources=(P.Resource("folder", str(notes)), P.Resource("provider", "router"))))[0],
            compiler.issue(app_draft(compiler, holder="device:bot-tools", limits=()))[0]]
    compiler.skill_token(made[1], "files-answer")

    def check_invariants() -> None:
        known = {m.id for m in compiler.mandates()}
        live = {m.id for m in compiler.mandates() if compiler.state_of(m) == "active"}
        assert all(row["mandate_id"] in known for row in compiler.table.rows())  # no row without a known permission
        assert all(a.mandate in live for a in apps.load_apps(home).values())  # every compiler-made app names a live one
        assert all(e["mandate"] in live for e in folders.load(home).values())
        assert all(t.id in live for t in compiler.tokens().values() if t.levels)
        assert compiler.orphans() == []

    check_invariants()
    assert len(apps.load_apps(home)) == 2 and len(folders.load(home)) == 1
    compiler.revoke(made[0].id)
    check_invariants()
    compiler.revoke(made[1].id)
    check_invariants()
    assert folders.load(home) == {} and len(apps.load_apps(home)) == 1
    assert not [t for t in compiler.tokens().values() if t.levels]  # the narrowed token went with its permission
    compiler.sweep()
    check_invariants()
    compiler.revoke(made[2].id)
    assert apps.load_apps(home) == {} and compiler.table.rows() == []


@needs_library
def test_a_plain_revoke_by_hand_is_brought_in_line_by_the_sweep(compiler, home):
    """`muse permissions revoke` only signs the stopped list. The sweep removes what the permission had made."""
    mandate, applied = compiler.issue(app_draft(compiler))
    revoked = compiler.revocations()
    revoked.add(P.revocation_ids(mandate)[0], compiler.now(), compiler.keys.load())
    compiler.store.save_revocations(revoked)
    assert compiler.sweep()["removed"] == [mandate.id]
    assert apps.load_apps(home) == {} and mandate.id in gate.stopped_ids(home)


@needs_library
def test_usage_is_counted_per_period_in_the_permissions_zone(compiler, clock):
    mandate, _ = compiler.issue(app_draft(compiler, limits=(P.Limit("requests", "", 0, "day", 50), P.Limit("spend", "USD", 2, "month", 500)),
                                          actions=("llm.call", "llm.paid"), period_zone="Asia/Taipei"))
    compiler.add_usage(mandate, "requests", "", 3)
    compiler.add_usage(mandate, "spend", "USD", 120)
    assert compiler.usage(mandate) == {("requests", "", "day"): 3, ("spend", "USD", "month"): 120}
    clock.now += DAY  # a new day in Taipei: the day's count starts again, the month's does not
    assert compiler.usage(mandate) == {("requests", "", "day"): 0, ("spend", "USD", "month"): 120}


@needs_library
def test_an_approval_token_is_single_use_and_short_lived(compiler, clock):
    mandate, _ = compiler.issue(draft(compiler, actions=("files.write",), resources=(P.Resource("folder", "/data/out"),)))
    _, token = compiler.skill_token(mandate, "writer")
    request = P.Request(holder="helper:home", action="files.write", resource=P.Resource("folder", "/data/out/a.md"),
                        privacy="private", job="skill.writer")
    assert P.check(token, request, {}, compiler.revocations(), compiler.store.spent(), compiler.now()).kind == "needs_confirmation"
    assert compiler.use_approval(token, request, "card0001").allowed
    again = compiler.use_approval(token, request, "card0001")
    assert (again.kind, again.detail) == ("deny", "already_used")  # the same approval, replayed
    assert compiler.use_approval(token, request, "card0002").allowed  # another card is another approval
    elsewhere = P.Request(holder="helper:home", action="files.write", resource=P.Resource("folder", "/etc/passwd"),
                          privacy="private", job="skill.writer")
    assert compiler.use_approval(token, elsewhere, "card0003").detail == "resource_not_allowed"  # an approval widens nothing
    compiler.revoke(mandate.id)
    assert compiler.use_approval(token, request, "card0004").detail == "revoked"
    spent = json.loads((compiler.store.dir / "spent.json").read_text())
    assert len(spent["entries"]) == 2 and all(until <= compiler.now() + 300 + 60 for _, until in spent["entries"])


# ================================================================ nothing secret in the table, the logs or the status


@needs_library
def test_no_token_or_key_reaches_the_table_the_status_or_the_log(compiler, home, caplog):
    with caplog.at_level(logging.DEBUG):
        mandate, applied = compiler.issue(app_draft(compiler))
        compiler.skill_token(mandate, "x")
        compiler.status(mandate.id)
        compiler.revoke(mandate.id)
        compiler.sweep()
    root_key = (Path(home["MUSE_HOME"]) / "root.key").read_text().strip()
    token_text = mandate.to_base64()
    table = (Path(home["MUSE_HOME"]) / "compiler.sqlite").read_bytes()
    records = (Path(home["MUSE_HOME"]) / "records.sqlite").read_bytes()
    for secret in (root_key, root_key.split("/", 1)[1], token_text, applied.app_token):
        assert secret.encode() not in table and secret.encode() not in records
        assert secret not in caplog.text
    assert os.listdir(compiler.store.dir)  # and what is on disk there is private
    for path in compiler.store.dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path.name
