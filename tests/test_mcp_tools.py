"""The Muse tools, one by one, without the protocol: muse.mcp.tools.call against the fake home.

The home is the real route table behind a real socket (the fake home's canned
data); the tools reach it only through the home control API client, as in
production. tests/test_mcp_stdio.py drives the same tools over real stdio.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from home_fakes import client_for, running
from muse.home import fake
from muse.home.auth import StaticVerifier
from muse.home.client import HomeClient
from muse.mcp import SCOPE_NOTE, permission, server, skills, tools

HOST, HOST_TOKEN = "host-tools", "sk-muse-dev-CANARYhostTOKEN0123456789abcdefXYZ"
PHONE, PHONE_TOKEN = "my-phone", "phone-device-token-for-tests"
KEY_CANARY = "gsk_CANARYtool0123456789abcdefABCDEF"
PROMPT_CANARY = "CANARY-PROMPT-4be1 summarise my private notes"

EXPECTED_TOOLS = ["allowance_status", "pause_status", "suggest_service", "ask_approval", "check_approval", "check_permission",
                  "record_action", "queue_job", "job_status", "list_skills", "run_skill", "skill_status"]


class Rig:
    def __init__(self, state: str = "normal") -> None:
        self.home = fake.FakeHome(state)
        # As `muse hosts setup` makes it: the tools server's device is a `host`, the person's phone an `owner`.
        self._ctx = running(self.home, StaticVerifier({HOST: HOST_TOKEN, PHONE: PHONE_TOKEN}, roles={HOST: "host"}))
        self.srv = self._ctx.__enter__()
        self.phone = client_for(self.srv, PHONE_TOKEN)
        self.ctx = tools.Context(home=tools.HomeLink(client_for(self.srv, HOST_TOKEN)))
        self.seen: list[str] = []

    def call(self, name: str, args=None, *, error: str | None = None):
        result, is_error = tools.call(self.ctx, name, args)
        self.seen.append(json.dumps(result))
        if error is None:
            assert not is_error, result
        else:
            assert is_error and set(result) == {"error"} and result["error"]["code"] == error, result
            assert "Traceback" not in result["error"]["message"] and "\n" not in result["error"]["message"]
        return result

    def pending(self) -> list[dict]:
        return [c for c in self.home.cards.list("pending") if c["job_ref"] == f"device:{HOST}"]

    def answer_soon(self, decision: str, always: bool = False) -> threading.Thread:
        """Answer the host's next card from the phone, as the person would, while a tool call waits."""
        before = {c["id"] for c in self.pending()}

        def go() -> None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                new = [c for c in self.pending() if c["id"] not in before]
                if new:
                    body = {"decision": decision, **({"always": True} if always else {})}
                    assert self.phone.call("POST", f"/v1/cards/{new[0]['id']}/answer", body)[0] == 200
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self.ctx.stop.set()
        self._ctx.__exit__(None, None, None)
        self.home.close()


@pytest.fixture()
def start():
    made: list[Rig] = []

    def make(state: str = "normal") -> Rig:
        made.append(Rig(state))
        return made[-1]

    yield make
    for r in made:
        r.close()
        assert HOST_TOKEN not in "".join(r.seen) and "CANARY" not in "".join(r.seen)


@pytest.fixture()
def rig(start):
    return start()


ASK = {"kind": "save_file", "title": "Save a file", "thing": "notes.md", "where": "Documents"}


# ---------------------------------------------------------------- the table


def test_tool_names_are_stable_and_the_dangerous_ones_are_absent():
    assert [t.name for t in tools.TOOLS] == EXPECTED_TOOLS
    for name in ("pay", "pause", "resume", "connect", "keys", "apps"):
        assert name not in tools.BY_NAME
    assert tools.NOT_EXPOSED == ("pay", "pause", "resume")


def _strings(schema, path="$"):
    if schema.get("type") == "string":
        yield path, schema
    for name, sub in (schema.get("properties") or {}).items():
        yield from _strings(sub, f"{path}.{name}")
    if "items" in schema:
        yield from _strings(schema["items"], f"{path}[]")


def test_every_tool_is_described_for_a_model_and_every_input_is_bounded():
    for t in tools.TOOLS:
        assert len(t.description) >= 80 and t.title, t.name
        assert t.input_schema["type"] == "object" and t.input_schema["additionalProperties"] is False, t.name
        for path, sub in _strings(t.input_schema):
            assert isinstance(sub.get("maxLength"), int), (t.name, path)
        for name, sub in t.input_schema["properties"].items():
            assert sub.get("description"), (t.name, name)
            if sub["type"] == "integer":
                assert "minimum" in sub and "maximum" in sub, (t.name, name)
            if sub["type"] == "array":
                assert "maxItems" in sub, (t.name, name)
        json.dumps(t.input_schema)
    assert [t.name for t in tools.TOOLS if not t.read_only] == ["ask_approval", "record_action", "queue_job", "run_skill"]


def test_tools_that_touch_approval_or_permission_say_what_muse_does_not_control():
    for name in ("ask_approval", "check_approval", "check_permission", "run_skill"):
        assert SCOPE_NOTE in tools.BY_NAME[name].description
    assert "advisory" in tools.BY_NAME["check_permission"].description.lower()
    assert "not built" not in tools.BY_NAME["record_action"].description.lower()
    assert SCOPE_NOTE in tools.BY_NAME["record_action"].description and "own report" in tools.BY_NAME["record_action"].description
    assert "cannot stop this host's own shell or file tools" in SCOPE_NOTE


# ---------------------------------------------------------------- allowance, pause, suggestion


def test_allowance_status_is_the_snapshot_reduced_to_what_a_bot_needs(rig):
    got = rig.call("allowance_status")
    assert got["paused"] is False and got["services_connected"] == len(got["services"]) > 0
    gemini = next(s for s in got["services"] if s["id"] == "gemini")
    assert gemini["state"] == "ok" and gemini["local"] is False
    day = next(lim for lim in gemini["limits"] if lim["window"] == "day" and lim["counts"] == "requests")
    snap = rig.phone.call("GET", "/v1/status")[1]  # the fake home moves its canned times to "now"
    source = next(lim for p in snap["providers"] if p["id"] == "gemini" for lim in p["limits"]
                  if lim["window"] == "day" and lim["metric"] == "requests")
    assert day == {"window": "day", "counts": "requests", "left": 1388, "of": 1500, "refills_at": source["reset_at"], "estimate": True}
    assert got["paid_today"] == {"spent_usd": 0.03, "cap_usd": 1.0, "left_usd": 0.97, "resets_at": snap["paid"]["resets_at"],
                                 "calls_with_unknown_cost": 0, "estimate": True}
    assert got["as_of"] == snap["generated_at"]
    assert got["queue_waiting"] == 3 and "estimate" in got["about_estimates"]
    # Nothing a bot has no use for: no app names, no recent calls, no key names, no deployments.
    text = json.dumps(got)
    for absent in ("key_env", "GEMINI_API_KEY", "open-webui", "recent", "deployments", "latency_ms", "/home/you"):
        assert absent not in text
    assert len(text) < tools.MAX_RESULT_CHARS


def test_next_refill_is_the_soonest_future_daily_or_monthly_reset(rig):
    rig.ctx.clock = lambda: 0.0  # every canned reset is still ahead
    got = rig.call("allowance_status")
    resets = sorted(lim["refills_at"] for s in got["services"] for lim in s["limits"] if lim["window"] != "minute" and lim["refills_at"])
    assert got["next_refill_at"] == resets[0]
    rig.ctx.clock = lambda: 4_000_000_000.0  # long after: nothing ahead
    assert rig.call("allowance_status")["next_refill_at"] is None


def test_allowance_status_when_used_up_paused_or_empty(start):
    used = start("used").call("allowance_status")
    assert not any(s["state"] == "ok" for s in used["services"] if not s["local"])
    assert any(s["state"] == "exhausted" and s["blocked_until"] for s in used["services"])
    assert all(lim["left"] == 0 for s in used["services"] if not s["local"] for lim in s["limits"] if lim["left"] is not None)
    paused = start("paused")
    assert paused.call("allowance_status")["paused"] is True
    status = paused.call("pause_status")
    assert status["paused"] is True and "paused" in status["meaning"] and status["scope_note"] == SCOPE_NOTE
    empty = start("empty").call("allowance_status")
    assert empty["services"] == [] and empty["services_connected"] == 0 and empty["next_refill_at"] is None


def test_pause_status_only_reads_and_says_who_may_change_it(rig):
    got = rig.call("pause_status")
    assert got["paused"] is False and "host bot cannot" in got["who_can_change_it"]
    assert rig.home.is_paused() is False
    rig.home.set_paused(True)
    assert rig.call("pause_status")["paused"] is True


def test_suggest_service_names_the_next_one_and_why(rig):
    got = rig.call("suggest_service")
    best = got["suggestion"]
    cat = rig.phone.call("GET", "/v1/catalogue")[1]
    assert best["id"] == cat["suggestions"][0]["id"] and best["why"] and best["effort"]
    assert f"muse connect {best['id']}" in got["how"] and "never ask for or handle a key" in got["how"]
    assert got["also_possible"] == [s["id"] for s in cat["suggestions"][1:4]]


def test_suggest_service_with_nothing_left_to_suggest(rig, monkeypatch):
    monkeypatch.setattr(rig.ctx.home.client, "call", lambda *a: (200, {"connected": ["a"], "suggestions": []}))
    got = rig.call("suggest_service")
    assert got["suggestion"] is None and got["connected"] == 1 and got["reason"]


# ---------------------------------------------------------------- approvals


def test_ask_approval_approved(rig):
    answering = rig.answer_soon("approve")
    got = rig.call("ask_approval", {**ASK, "timeout_s": 10})
    answering.join(5)
    assert got["decision"] == "approved" and got["always"] is False and got["scope_note"] == SCOPE_NOTE
    card = rig.home.cards.get(got["card_id"])
    assert (card["kind"], card["title"], card["thing"], card["where"]) == ("save_file", "Save a file", "notes.md", "Documents")
    assert card["under"]["text"] == "Asked by a host bot through Muse's tools" and card["answered_by"] == PHONE
    assert card["job_ref"] == f"device:{HOST}"


def test_ask_approval_declined_stops_the_action(rig):
    answering = rig.answer_soon("decline")
    got = rig.call("ask_approval", {**ASK, "timeout_s": 10})
    answering.join(5)
    assert got["decision"] == "declined" and "Do not do this action" in got["what_to_do"] and "always" not in got


def test_ask_approval_always_is_reported_but_not_acted_on(rig):
    answering = rig.answer_soon("approve", always=True)
    got = rig.call("ask_approval", {**ASK, "timeout_s": 10})
    answering.join(5)
    assert got["always"] is True and "ask again next time" in got["about_always"]


def test_ask_approval_expired(rig):
    started = time.monotonic()
    got = rig.call("ask_approval", {**ASK, "timeout_s": 10, "valid_for_s": 1})
    assert got["decision"] == "expired" and "Treat that as no" in got["what_to_do"]
    assert time.monotonic() - started < 5


def test_ask_approval_times_out_as_pending_and_check_approval_polls(rig):
    started = time.monotonic()
    got = rig.call("ask_approval", {**ASK, "timeout_s": 1})
    waited = time.monotonic() - started
    assert got["decision"] == "pending" and 0.9 <= waited < 3 and got["expires_at"]
    assert "check_approval" in got["what_to_do"] and "Do not do the action yet" in got["what_to_do"]
    card_id = got["card_id"]
    assert rig.call("check_approval", {"card_id": card_id})["decision"] == "pending"
    assert rig.phone.call("POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"})[0] == 200
    after = rig.call("check_approval", {"card_id": card_id})
    assert after["decision"] == "approved" and after["card_id"] == card_id and after["scope_note"] == SCOPE_NOTE


def test_ask_approval_with_no_wait_returns_at_once(rig):
    started = time.monotonic()
    assert rig.call("ask_approval", {**ASK, "timeout_s": 0})["decision"] == "pending"
    assert time.monotonic() - started < 1


def test_the_wait_is_bounded_whatever_is_asked(rig):
    rig.call("ask_approval", {**ASK, "timeout_s": tools.MAX_TIMEOUT_S + 1}, error="invalid_input")
    rig.call("ask_approval", {**ASK, "timeout_s": -1}, error="invalid_input")
    assert tools.DEFAULT_TIMEOUT_S == 120 and tools.MAX_TIMEOUT_S == 600
    assert rig.pending() == []


def test_a_waiting_tool_lets_go_when_the_server_stops(rig):
    threading.Timer(0.3, rig.ctx.stop.set).start()
    started = time.monotonic()
    got = rig.call("ask_approval", {**ASK, "timeout_s": 60})
    assert got["decision"] == "pending" and time.monotonic() - started < 3


def test_card_lifetimes(rig):
    rig.call("ask_approval", {**ASK, "timeout_s": 0})
    spend = {"kind": "spend", "title": "Pay", "thing": "Top-up", "cost": {"amount": "30", "currency": "TWD"},
             "extra": {"payee": "ACME", "limit_left": {"amount": "70", "currency": "TWD"}}, "timeout_s": 0}
    rig.call("ask_approval", spend)
    from datetime import datetime

    lives = {}
    for card in rig.pending():
        expires = datetime.fromisoformat(card["expires_at"].replace("Z", "+00:00")).timestamp()
        lives[card["kind"]] = expires - time.time()
    assert abs(lives["save_file"] - tools.DEFAULT_VALID_FOR_S) < 10 and abs(lives["spend"] - 300) < 10


def test_each_kind_a_host_may_ask_about(rig):
    cases = [
        {"kind": "message", "title": "Send an email", "thing": "Re: invoice", "where": "to a@example.com",
         "extra": {"preview": "Hello, the invoice is attached.", "to": "a@example.com"}},
        {"kind": "send_private", "title": "Send private notes", "thing": "health-notes.md",
         "extra": {"badge": "private", "services": ["Gemini"]}},
        {"kind": "save_file", "title": "Save", "thing": "a.txt", "extra": {"size_bytes": 10}},
    ]
    for case in cases:
        assert rig.call("ask_approval", {**case, "timeout_s": 0})["decision"] == "pending"
    assert sorted(c["kind"] for c in rig.pending()) == ["message", "save_file", "send_private"]


@pytest.mark.parametrize("args", [
    {"kind": "grant_permission", "title": "t", "thing": "x"},  # a host may not ask for a standing permission
    {"kind": "install_skill", "title": "t", "thing": "x"},
    {"kind": "spend", "title": "Pay", "thing": "x", "extra": {"payee": "A", "limit_left": {"amount": "1", "currency": "USD"}}},
    {"kind": "message", "title": "Send", "thing": "x"},  # no preview
    {"kind": "save_file", "title": "t", "thing": "x", "extra": {"surprise": 1}},
    {"kind": "save_file", "title": "t" * 81, "thing": "x"},
    {"kind": "save_file", "title": "t", "thing": "x", "cost": {"amount": "1"}},
    {"kind": "save_file", "title": "t", "thing": "x", "cost": {"amount": "one", "currency": "USD"}},
    {"kind": "save_file", "title": "t", "thing": "x", "extra": "big"},
    {"kind": "save_file", "title": "t", "thing": "x", "extra": {"a": "y" * 5000}},
    {"kind": "save_file", "title": "t", "thing": "x", "under": "from the person"},  # not a field a host may set
    {"title": "t", "thing": "x"},
])
def test_a_bad_question_is_refused_and_no_card_is_made(rig, args):
    rig.call("ask_approval", {**args, "timeout_s": 0}, error="invalid_input")
    assert rig.pending() == []


def test_check_approval_failures(rig):
    rig.call("check_approval", {"card_id": "0" * 16}, error="not_found")
    for bad in ({}, {"card_id": "nope"}, {"card_id": "0" * 15}, {"card_id": 5}, {"card_id": "0" * 16, "x": 1}):
        rig.call("check_approval", bad, error="invalid_input")


def test_too_many_waiting_questions_is_said_plainly(rig):
    from muse.home import api

    for _ in range(api.MAX_PENDING_PER_DEVICE):
        rig.call("ask_approval", {**ASK, "timeout_s": 0})
    got = rig.call("ask_approval", {**ASK, "timeout_s": 0}, error="conflict")
    assert "too many questions" in got["error"]["message"]


# ---------------------------------------------------------------- permission (the interim rule)


def test_check_permission_asks_for_everything_that_writes_sends_or_spends(rig):
    rig.ctx.allowed_read = ("/home/me/notes",)
    for action in permission.OTHER_ACTIONS:
        got = rig.call("check_permission", {"action": action, "resource": "/home/me/notes/a.md"})
        assert got["decision"] == "ask" and got["reason"] == "permissions are not enforced yet; ask for approval"
        assert got["enforced"] is False and got["scope_note"] == SCOPE_NOTE and "ask_approval" in got["what_to_do"]
        assert "advice only" in got["advisory"]
    got = rig.call("check_permission", {"action": "spend", "amount": {"amount": "5", "currency": "USD"}})
    assert got["decision"] == "ask"


def test_check_permission_allows_reading_only_what_the_person_listed(rig):
    rig.ctx.allowed_read = ("/home/me/notes", "calendar")
    allow = [("read", "/home/me/notes/a.md"), ("list", "/home/me/notes"), ("search", "/home/me/notes/sub/"), ("read", "calendar")]
    ask = [("read", "/home/me/notes-private/a.md"), ("read", "/home/me"), ("read", "/home/me/notes/../.ssh/id"), ("read", "/etc/passwd"),
           ("read", "calendar2"), ("read", ""), ("list", "/home/me/notes\x00/x")]
    for action, resource in allow:
        got = rig.call("check_permission", {"action": action, "resource": resource})
        assert got["decision"] == "allow" and got["enforced"] is False and got["scope_note"] == SCOPE_NOTE, resource
    for action, resource in ask:
        result, is_error = tools.call(rig.ctx, "check_permission", {"action": action, "resource": resource})
        assert is_error or result["decision"] == "ask", resource
    assert rig.call("check_permission", {"action": "read"})["decision"] == "ask"  # nothing named
    assert rig.call("check_permission", {"action": "read", "resource": "/home/me/notes/a", "amount": {"amount": "1", "currency": "USD"}})[
        "decision"] == "ask"
    rig.ctx.allowed_read = ()
    assert rig.call("check_permission", {"action": "read", "resource": "/home/me/notes/a.md"})["decision"] == "ask"


def test_the_interim_rule_is_one_pure_function_that_never_denies():
    assert permission.decide("read", "/a/b", allowed_read=("/a",)) == {
        "decision": "allow", "reason": "a read-only action on something the person listed for this host", "enforced": False}
    for action in permission.ACTIONS + ("anything",):
        for resource in (None, "/x", 5, ["/a"]):
            verdict = permission.decide(action, resource, allowed_read=())
            assert verdict["decision"] == "ask" and verdict["enforced"] is False
    assert not permission.listed("../a", ("/",)) and permission.listed("C:\\notes\\a.md", ("C:/notes",))


def test_check_permission_bad_input(rig):
    for bad in ({}, {"action": "fly"}, {"action": "read", "resource": "x" * 401}, {"action": "read", "amount": "5"}):
        rig.call("check_permission", bad, error="invalid_input")


# ---------------------------------------------------------------- records


def test_record_action_writes_a_signed_record_on_the_home_and_returns_its_number_and_hash(rig):
    pytest.importorskip("cryptography")
    from muse.records import format as fmt
    from muse.records.home import HomeRecords

    before = len(HomeRecords(rig.home.environ).records())
    got = rig.call("record_action", {"action": "saved a file", "summary": "Wrote the weekly digest.", "outcome": "done",
                                     "cost": {"amount": "30", "currency": "TWD"}, "sent_to": "none"})
    record = HomeRecords(rig.home.environ).records()[-1]
    assert got == {"recorded": True, "seq": before, "hash": fmt.record_hash(record), "action": "host.saved_a_file",
                   "recorded_as": "helper", "about": tools.RECORD_NOTE, "scope_note": SCOPE_NOTE}
    assert (record["action"], record["actor"], record["mandate"], record["outcome"]) == ("host.saved_a_file", "helper", f"device:{HOST}", "ok")
    assert record["summary"] == "Wrote the weekly digest." and record["cost"] == {"currency": "TWD", "minor": 3000} and record["egress"] == "none"
    assert fmt.verify_chain(HomeRecords(rig.home.environ).records()) == before + 1
    assert not list(Path(os.environ["MUSE_HOME"]).iterdir())  # written by the home, in the home's folder: no copy here
    sent = rig.call("record_action", {"action": "message.send", "summary": "Sent a message", "outcome": "skipped", "sent_to": "service:gmail"})
    last = HomeRecords(rig.home.environ).records()[-1]
    assert sent["seq"] == before + 1 and (last["action"], last["outcome"], last["egress"]) == ("host.message.send", "declined", "service:gmail")
    for bad in ({}, {"action": "a", "summary": "s", "outcome": "great"}, {"action": "a", "summary": "s" * 201, "outcome": "done"},
                {"action": "a", "summary": "two\nlines", "outcome": "done"}, {"action": "a", "summary": "s", "outcome": "done", "sent_to": "host:me"},
                {"action": "a", "summary": "s", "outcome": "done", "sent_to": "home"}, {"action": "a", "summary": "s", "outcome": "done", "actor": "person"},
                {"action": "a", "summary": "s", "outcome": "done", "cost": {"amount": "1", "currency": "XTS"}},
                {"action": "a", "summary": "s", "outcome": "done", "cost": {"amount": "0.001", "currency": "USD"}}):
        rig.call("record_action", bad, error="invalid_input")
    assert len(HomeRecords(rig.home.environ).records()) == before + 2


@pytest.mark.parametrize("given,stored", [
    ("files.write", "host.files.write"), ("Message.Send", "host.message.send"), ("saved a file", "host.saved_a_file"),
    ("web search!", "host.web_search"), ("home.pause", "host.home.pause"), ("card.answer", "host.card.answer"), ("key.store", "host.key.store"),
    ("skill.run", "host.skill.run"), ("llm.call", "host.llm.call"), ("host.files.write", "host.files.write"), ("a.b.c", "host.a.b.c"),
    ("a.b.c.d", "host.a_b_c_d"), ("3d print", "host.x_3d_print"), ("\u5beb\u5165\u6a94\u6848", "host.x"), ("a" * 80, "host." + "a" * 32),
    ("a.b.c.d.e", "host.a_b_c_d_e"), ("host", "host.host"), ("host.", "host.x"),
])
def test_a_hosts_action_name_is_always_filed_under_host(given, stored):
    from muse.records import format as fmt

    assert tools.record_action_name(given) == stored
    assert fmt._ACTION.match(stored) and stored.split(".")[0] == tools.HOST_FAMILY == "host"


def test_record_action_says_plainly_when_nothing_was_recorded(rig, monkeypatch):
    for reply in ((503, {"error": {"code": "unavailable", "message": "x"}}), (501, {"error": {"code": "not_built", "message": "x"}}),
                  (201, {"record": "odd"}), (201, {})):
        monkeypatch.setattr(rig.ctx.home.client, "call", lambda m, p, b=None, reply=reply: reply)
        got = rig.call("record_action", {"action": "a", "summary": "s", "outcome": "done"})
        assert got["recorded"] is False and "do not tell the person" in got["what_to_do"] and got["scope_note"] == SCOPE_NOTE
    sent = []
    monkeypatch.setattr(rig.ctx.home.client, "call", lambda m, p, b=None: (sent.append((m, p, b)) or (201, {"record": {"seq": 4, "actor": "helper"}, "hash": "ab" * 32})))
    got = rig.call("record_action", {"action": "a", "summary": "s", "outcome": "failed"})
    assert got["recorded"] is True and got["seq"] == 4 and got["hash"] == "ab" * 32
    assert sent == [("POST", "/v1/receipts", {"action": "host.a", "summary": "s", "outcome": "failed"})]  # never an actor, a device or a time


# ---------------------------------------------------------------- the queue


def test_queue_job_and_job_status_never_return_the_prompt(rig):
    got = rig.call("queue_job", {"prompt": PROMPT_CANARY, "task": "light", "by": "2099-01-01T08:00:00Z"})
    job = got["job"]
    assert job["status"] == "queued" and job["kind"] == "prompt" and job["task"] == "light" and job["allow_paid"] is False
    assert job["by"] == "2099-01-01T08:00:00Z" and "not returned through this tool" in got["what_happens"]
    stored = rig.home.queue().get(job["id"])
    assert stored.payload == PROMPT_CANARY and stored.allow_paid is False
    status = rig.call("job_status", {"id": job["id"]})["job"]
    assert status["id"] == job["id"] and status["status"] == "queued" and status["result_file"] is None
    assert "CANARY-PROMPT" not in "".join(rig.seen)


def test_a_batch_job(rig):
    got = rig.call("queue_job", {"prompts": ["tag this", "tag that"]})
    assert got["job"]["kind"] == "batch" and json.loads(rig.home.queue().get(got["job"]["id"]).payload) == ["tag this", "tag that"]


def test_job_status_of_a_finished_job_gives_the_file_not_the_answer(rig):
    done = next(j for j in rig.home.queue().list() if j.status == "done")
    rig.call("job_status", {"id": done.id}, error="not_found")  # somebody else's job: a host sees only its own
    rig.home.book.set_job_owner(done.id, HOST)
    job = rig.call("job_status", {"id": done.id})["job"]
    assert job["status"] == "done" and job["service"] == "gemini" and job["cost_usd"] == 0.0
    assert set(job) == {"id", "status", "kind", "task", "by", "allow_paid", "attempts", "times_held_back", "next_try_at",
                        "why_waiting", "finished_at", "service", "cost_usd", "result_file"}


@pytest.mark.parametrize("args", [
    {},
    {"prompt": "a", "prompts": ["b"]},
    {"prompt": ""},
    {"prompt": "   "},
    {"prompt": "x" * (tools.MAX_PROMPT_CHARS + 1)},
    {"prompts": []},
    {"prompts": ["a"] * 21},
    {"prompts": ["a", 5]},
    {"prompts": ["x" * 20_000] * 3},
    {"prompt": "a", "task": "Heavy!"},
    {"prompt": "a", "by": "tomorrow"},
    {"prompt": "a", "by": "2026-11-14T08:30:00"},  # no zone
    {"prompt": "a", "by": "2001-01-01T00:00:00Z"},  # in the past
    {"prompt": "a", "allow_paid": "yes"},
    {"prompt": "a", "alias": "smart"},
    {"prompt": "a", "privacy": "public"},
])
def test_a_bad_job_is_refused_and_nothing_is_queued(rig, args):
    before = len(rig.home.queue().list())
    rig.call("queue_job", args, error="invalid_input")
    assert len(rig.home.queue().list()) == before


def test_an_unknown_task_is_refused_by_the_home_in_plain_words(rig):
    result, is_error = tools.call(rig.ctx, "queue_job", {"prompt": "a", "task": "no-such-task-name"})
    assert not is_error or result["error"]["code"] == "invalid_input"


def test_a_paid_job_needs_an_approved_spend_card(rig):
    before = len(rig.home.queue().list())
    assert "ask_approval" in rig.call("queue_job", {"prompt": "a", "allow_paid": True}, error="approval_needed")["error"]["message"]
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": "0" * 16}, error="approval_needed")
    spend = {"kind": "spend", "title": "Pay for a job", "thing": "up to US$0.05", "cost": {"amount": "0.05", "currency": "USD"},
             "extra": {"payee": "paid model", "limit_left": {"amount": "0.97", "currency": "USD"}}}
    # Still waiting, declined, or approved but for something that is not spending: all refused.
    waiting = rig.call("ask_approval", {**spend, "timeout_s": 0})["card_id"]
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": waiting}, error="approval_needed")
    rig.phone.call("POST", f"/v1/cards/{waiting}/answer", {"decision": "decline"})
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": waiting}, error="approval_needed")
    other = rig.call("ask_approval", {**ASK, "timeout_s": 0})["card_id"]
    rig.phone.call("POST", f"/v1/cards/{other}/answer", {"decision": "approve"})
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": other}, error="approval_needed")
    # A spend card the home made for something else (not asked through the tools) does not count either.
    own = rig.home.cards.create(kind="spend", title="Pay", thing="x", cost={"amount": "1", "currency": "USD"},
                                extra={"payee": "A", "limit_left": {"amount": "1", "currency": "USD"}})
    rig.phone.call("POST", f"/v1/cards/{own}/answer", {"decision": "approve"})
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": own}, error="approval_needed")
    assert len(rig.home.queue().list()) == before
    # Approved: the job is queued and may pay. An hour later the same approval no longer works.
    card = rig.call("ask_approval", {**spend, "timeout_s": 0})["card_id"]
    rig.phone.call("POST", f"/v1/cards/{card}/answer", {"decision": "approve"})
    got = rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": card})
    assert got["job"]["allow_paid"] is True and rig.home.queue().get(got["job"]["id"]).allow_paid is True
    rig.ctx.clock = lambda: time.time() + tools.PAID_APPROVAL_MAX_AGE_S + 60
    rig.call("queue_job", {"prompt": "a", "allow_paid": True, "approval_card_id": card}, error="approval_needed")


def test_job_status_failures(rig):
    rig.call("job_status", {"id": "0" * 12}, error="not_found")
    for bad in ({}, {"id": "nope"}, {"id": "0" * 16}):
        rig.call("job_status", bad, error="invalid_input")


# ---------------------------------------------------------------- skills


def test_skills_answer_not_built_until_someone_connects_them(rig):
    assert skills.backend() is None
    listed = rig.call("list_skills")
    assert listed["built"] is False and listed["skills"] == [] and listed["reason"] in (
        "skills are not connected to host bots yet", "skills are not built yet")
    ran = rig.call("run_skill", {"name": "weekly-digest", "inputs": {"week": "46"}})
    assert ran["built"] is False and ran["ran"] is False
    for bad in ({}, {"name": "Bad Name"}, {"name": "ok", "inputs": "x"}, {"name": "ok", "inputs": {"a": "y" * 5000}}):
        rig.call("run_skill", bad, error="invalid_input")


@pytest.fixture()
def connected(rig):
    """The tools server as `build_context` leaves it: skills reached through the home."""
    skills.connect(skills.HomeSkills(rig.ctx.home))
    yield rig
    skills.connect(None)


def test_skills_are_listed_and_run_through_the_home(connected):
    rig = connected
    listed = rig.call("list_skills")
    assert listed["built"] is True and [f["name"] for f in listed["folders"]] == ["documents", "feeds"] and "cannot add one" in listed["about_folders"]
    by_name = {s["name"]: s for s in listed["skills"]}
    assert by_name["files-answer"]["can_run"] is True and by_name["files-answer"]["reads_folders"] is True
    assert set(by_name["files-answer"]) == {"name", "summary", "can_run", "reads_folders", "proposes_files"}
    assert len(by_name["files-answer"]["summary"]) <= 160
    ran = rig.call("run_skill", {"name": "files-answer", "inputs": {"question": "what changed?"}})
    assert ran["built"] is True and ran["ran"] is True and ran["status"] == "done" and ran["checked"] is True
    assert "fake home" in ran["answer"] and ran["answer_cut_short"] is False and ran["scope_note"] == SCOPE_NOTE
    assert re.fullmatch(tools.RUN_ID.strip("^$"), ran["run_id"]) and "files" not in ran
    again = rig.call("skill_status", {"run_id": ran["run_id"]})
    assert again == ran
    # The run was started by this host's own device: the person's phone sees it, another device's id does not exist for the host.
    assert rig.phone.call("GET", f"/v1/skills/runs/{ran['run_id']}")[1]["run"]["started_by"] == HOST
    rig.call("skill_status", {"run_id": "0" * 16}, error="not_found")


def test_run_skill_refusals_say_what_to_do(connected):
    rig = connected
    assert "list_skills" in rig.call("run_skill", {"name": "no-such-skill"}, error="not_found")["error"]["message"]
    err = rig.call("run_skill", {"name": "files-answer", "folders": ["not-on-the-list"]}, error="invalid_input")
    assert "folder names that list_skills shows" in err["error"]["message"]
    for bad in ({}, {"name": "Bad Name"}, {"name": "ok", "inputs": "x"}, {"name": "ok", "inputs": {"a": "y" * 5000}},
                {"name": "ok", "folders": ["/etc"]}, {"name": "ok", "folders": ["../x"]}, {"name": "ok", "out": "/tmp"},
                {"name": "ok", "folders": ["a"] * 9}, {"name": "ok", "budget": {"paid_usd": 1}}, {"name": "ok", "timeout_s": 601}):
        rig.call("run_skill", bad, error="invalid_input")  # a path is never a folder name, and a host cannot set a budget
    rig.call("skill_status", {"run_id": "short"}, error="invalid_input")


def test_a_run_that_outlasts_the_wait_is_picked_up_with_skill_status(connected):
    rig = connected
    release = threading.Event()
    canned = rig.home.skills.runner
    rig.home.skills.runner = lambda skill, inputs, **kw: release.wait(10) and canned(skill, inputs)
    started = rig.call("run_skill", {"name": "files-answer", "timeout_s": 0})
    assert started["status"] == "running" and started["ran"] is False and "skill_status" in started["what_to_do"] and "answer" not in started
    assert "another skill is still running" in rig.call("run_skill", {"name": "files-answer"}, error="conflict")["error"]["message"]
    release.set()
    rig.home.skills.wait(started["run_id"])
    assert rig.call("skill_status", {"run_id": started["run_id"]})["status"] == "done"


def test_a_long_answer_is_cut_to_fit_and_says_so_and_proposed_files_are_listed_with_their_cards(connected):
    rig = connected
    run = {"id": "ab" * 8, "skill": "writer", "status": "partial", "result": {
        "output": {"text": "line one\n" + "\"quoted\" " * 2000}, "notes": ["n" * 500] * 9, "check": {"status": "failed", "reasons": ["x"]}},
        "writes": [{"card_id": f"{i:016x}", "file": "f" * 300, "size": 3, "state": state, "reason": None}
                   for i, state in enumerate(["pending", "written", "refused", "discarded", "applying", "odd"] * 2)]}

    class Backend:
        def status(self, run_id):
            return run

    skills.connect(Backend())
    got = rig.call("skill_status", {"run_id": "ab" * 8})
    assert len(json.dumps(got, ensure_ascii=False)) <= tools.MAX_RESULT_CHARS
    assert got["answer"].startswith("line one\n") and got["answer_cut_short"] is True and 500 < len(got["answer"]) <= tools.MAX_ANSWER_CHARS
    assert got["checked"] is False and len(got["notes"]) == 5 and all(len(n) <= tools.MAX_TEXT for n in got["notes"])
    assert len(got["files"]) == 10 and got["more_files"] == 2 and len(got["files"][0]["file"]) == 80
    assert [f["state"].split(":")[0].split(";")[0] for f in got["files"][:6]] == [
        "waiting for the person to approve it in Muse", "written", "approved, but could not be written", "not written", "being written", "unknown"]
    assert "Do not write the file yourself" in got["about_files"]
    # Replies of an unexpected shape never crash the tool or leak.
    for odd in (None, [], "text", {"status": "exploded"}, {"status": "done", "result": "CANARY-ERRTEXT"}):
        run = odd
        got = rig.call("skill_status", {"run_id": "ab" * 8})
        assert "CANARY" not in json.dumps(got) and got["status"] in ("failed", "done")


def test_the_skills_bridge_imports_nothing_from_the_skills_package():
    source = Path(skills.__file__).read_text(encoding="utf-8")
    assert "import muse.skills" not in source and "from muse.skills" not in source and "from muse import skills" not in source


# ---------------------------------------------------------------- input safety


def test_anything_key_shaped_is_refused_with_one_generic_sentence(rig):
    cases = [
        ("ask_approval", {**ASK, "thing": f"use {KEY_CANARY}"}),
        ("ask_approval", {**ASK, "extra": {"size_bytes": 1, "note": KEY_CANARY}}),
        ("queue_job", {"prompt": f"my key is {KEY_CANARY}"}),
        ("queue_job", {"prompts": ["fine", f"Authorization: Bearer {KEY_CANARY}"]}),
        ("record_action", {"action": "a", "summary": KEY_CANARY, "outcome": "done"}),
        ("check_permission", {"action": "read", "resource": KEY_CANARY}),
        ("run_skill", {"name": "x", "inputs": {"token": KEY_CANARY}}),
        ("allowance_status", {KEY_CANARY: 1}),
    ]
    jobs, cards_before = len(rig.home.queue().list()), len(rig.home.cards.list())
    for name, args in cases:
        got = rig.call(name, args, error="refused")
        assert got["error"]["message"] == tools.SECRET_REFUSED
    assert len(rig.home.queue().list()) == jobs and len(rig.home.cards.list()) == cards_before


@pytest.mark.parametrize("name,args", [
    ("no_such_tool", {}),
    ("pause", {}),
    ("resume", {}),
    ("pay", {"amount": "1"}),
    (None, {}),
    ("", {}),
])
def test_an_unknown_tool_is_refused(rig, name, args):
    rig.call(name, args, error="unknown_tool")
    assert rig.home.is_paused() is False


@pytest.mark.parametrize("args", ["text", 5, ["a"], {"x": object()}])
def test_arguments_that_are_not_a_plain_object_are_refused(rig, args):
    rig.call("allowance_status", args, error="invalid_input")


def test_tools_that_take_nothing_refuse_extra_fields_and_oversized_input(rig):
    for name in ("allowance_status", "pause_status", "suggest_service", "list_skills"):
        rig.call(name, {"extra_field": 1}, error="invalid_input")
        assert tools.call(rig.ctx, name, None)[1] is False and tools.call(rig.ctx, name, {})[1] is False
    rig.call("queue_job", {"prompt": "a", "junk": "z" * (tools.MAX_ARGS_CHARS + 1)}, error="invalid_input")


# ---------------------------------------------------------------- when the home is not there, and output safety


def test_an_unreachable_home_is_a_clear_tool_error_not_a_crash(tmp_path):
    ctx = tools.Context(home=tools.HomeLink(HomeClient(HOST_TOKEN, socket_path=tmp_path / "no-home.sock", timeout=2)))
    for name, args in (("allowance_status", {}), ("pause_status", {}), ("suggest_service", {}), ("ask_approval", ASK),
                       ("check_approval", {"card_id": "0" * 16}), ("record_action", {"action": "a", "summary": "s", "outcome": "done"}),
                       ("queue_job", {"prompt": "a"}), ("job_status", {"id": "0" * 12})):
        result, is_error = tools.call(ctx, name, args)
        assert is_error and result["error"]["code"] == "home_unreachable", name
        assert "muse home serve" in result["error"]["message"] and "sock" not in result["error"]["message"]
    # Tools that do not need the home keep working.
    assert tools.call(ctx, "check_permission", {"action": "write"})[0]["decision"] == "ask"
    assert tools.call(ctx, "list_skills", {})[1] is False
    skills.connect(skills.HomeSkills(ctx.home))
    try:
        for name, args in (("list_skills", {}), ("run_skill", {"name": "files-answer"}), ("skill_status", {"run_id": "0" * 16})):
            result, is_error = tools.call(ctx, name, args)
            assert is_error and result["error"]["code"] == "home_unreachable", name
    finally:
        skills.connect(None)


def test_a_wrong_token_and_an_unavailable_home(start):
    rig = start()
    rig.ctx.home = tools.HomeLink(client_for(rig.srv, "not-the-token"))
    got = rig.call("allowance_status", error="unauthorized")
    assert "muse home token add" in got["error"]["message"]
    down = start("error")
    got = down.call("allowance_status", error="home_unavailable")
    assert got["error"]["retry_after"] == 5
    down.call("ask_approval", ASK, error="home_unavailable")


class _Reply:
    def __init__(self, status, payload):
        self.reply = (status, payload)

    def call(self, method, path, body=None):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_a_result_is_bounded_and_scrubbed_whatever_the_home_sends():
    long_name = "n" * 5000
    snap = {"paused": False, "generated_at": "2026-11-12T14:00:00Z", "paid": {"cap_usd": 1.0, "spent_usd": 0.1, "resets_at": KEY_CANARY},
            "providers": [{"id": "p1", "name": long_name, "configured": True, "state": KEY_CANARY, "kind": "cloud",
                           "limits": [{"window": "day", "metric": "requests", "remaining": 1, "limit": 2, "reset_at": "x", "source": "header"}],
                           "token": HOST_TOKEN}]}
    result, is_error = tools.call(tools.Context(home=tools.HomeLink(_Reply(200, snap))), "allowance_status", {})
    assert not is_error
    text = json.dumps(result)
    assert "CANARY" not in text and "[removed]" in text and long_name not in text
    assert len(result["services"][0]["name"]) == tools.MAX_TEXT and result["services"][0]["limits"][0]["estimate"] is False
    many = {"paused": False, "providers": [{"id": f"p{i}", "name": "n" * 300, "configured": True, "limits": [
        {"window": "w" * 300, "metric": "m" * 300, "reset_at": "r" * 300}] * 6} for i in range(200)]}
    result, is_error = tools.call(tools.Context(home=tools.HomeLink(_Reply(200, many))), "allowance_status", {})
    assert is_error and result["error"]["code"] == "too_large"


@pytest.mark.parametrize("reply", [
    (200, None), (200, []), (200, "text"), (500, {"error": {"code": "internal", "message": "CANARY-ERRTEXT"}}),
    (500, None), (418, {"error": "CANARY-ERRTEXT"}), (200, {"card": "CANARY-ERRTEXT"}), (200, {"providers": "CANARY", "paid": 5}),
    RuntimeError("CANARY-ERRTEXT in an exception"), KeyError("CANARY-ERRTEXT"),
])
def test_an_unexpected_reply_or_failure_never_leaks_its_text(reply):
    client = _Reply(*reply) if isinstance(reply, tuple) else _Reply(0, None)
    if isinstance(reply, Exception):
        client.reply = reply
    ctx = tools.Context(home=tools.HomeLink(client))
    for name, args in (("allowance_status", {}), ("suggest_service", {}), ("ask_approval", {**ASK, "timeout_s": 0}),
                       ("check_approval", {"card_id": "0" * 16}), ("queue_job", {"prompt": "a"}), ("job_status", {"id": "0" * 12}),
                       ("record_action", {"action": "a", "summary": "s", "outcome": "done"}), ("pause_status", {})):
        result, is_error = tools.call(ctx, name, args)
        assert "CANARY" not in json.dumps(result), name
        assert isinstance(result, dict)
        if is_error:
            assert set(result) == {"error"} and set(result["error"]) <= {"code", "message", "retry_after"}


def test_a_bug_in_a_tool_is_a_plain_internal_error(rig, monkeypatch):
    def boom(ctx, args):
        raise RuntimeError("CANARY-ERRTEXT /home/me/secret/path")

    monkeypatch.setitem(tools.BY_NAME, "pause_status", tools.Tool("pause_status", "t", "d", tools._obj(), boom, True))
    got = rig.call("pause_status", error="internal")
    assert "CANARY" not in json.dumps(got)


# ---------------------------------------------------------------- the device token


def _token_file(tmp_path: Path, text: str = HOST_TOKEN + "\n", mode: int = 0o600) -> Path:
    path = tmp_path / "host.token"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_the_token_is_read_from_a_private_file(tmp_path):
    assert server.read_token_file(_token_file(tmp_path)) == HOST_TOKEN
    assert server.load_token(str(_token_file(tmp_path)), {"MUSE_HOME_TOKEN": "ignored-when-a-file-is-given"}) == HOST_TOKEN
    assert server.load_token(None, {"MUSE_HOME_TOKEN": f" {HOST_TOKEN}\n"}) == HOST_TOKEN


@pytest.mark.skipif(os.name != "posix", reason="file modes")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o777])
def test_a_token_file_others_can_read_stops_the_server(tmp_path, mode):
    with pytest.raises(server.StartupError) as err:
        server.read_token_file(_token_file(tmp_path, mode=mode))
    assert "chmod 600" in str(err.value) and "CANARY" not in str(err.value)


def test_other_token_problems_never_quote_the_token(tmp_path):
    link = tmp_path / "link.token"
    link.symlink_to(_token_file(tmp_path))
    cases = [link, tmp_path / "missing.token", tmp_path]
    for text in ("", "\n\n", f"{HOST_TOKEN}\n{HOST_TOKEN}\n", f"{HOST_TOKEN} trailing words", "x" * 5000):
        folder = tmp_path / f"case{len(cases)}"
        folder.mkdir()
        cases.append(_token_file(folder, text))
    for path in cases:
        with pytest.raises(server.StartupError) as err:
            server.read_token_file(path)
        assert "CANARY" not in str(err.value) and HOST_TOKEN not in str(err.value)
    for environ in ({}, {"MUSE_HOME_TOKEN": "  "}, {"MUSE_HOME_TOKEN": "two words"}):
        with pytest.raises(server.StartupError) as err:
            server.load_token(None, environ)
        assert "two words" not in str(err.value)


def test_the_token_is_never_taken_from_the_command_line():
    options = {a.dest for a in server.parser()._actions}
    assert options == {"help", "token_file", "socket", "tcp", "allow_read"}
