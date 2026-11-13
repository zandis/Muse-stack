"""The app's routes (muse/home/app_api.py): money, Ask, chores, services, the one-tap flow, settings.

No network and no model: the router is a function that returns canned replies, the OAuth
service is a tiny server on 127.0.0.1, and the key test is stubbed.
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal

import pytest

from home_fakes import assert_error
from muse import keys
from muse.home import api, app_api, app_ask, app_connect, app_money, auth, fake

OWNER = auth.Device("my-laptop", role="owner")
HOST = auth.Device("a-bot", role="host")
VIEWER = auth.Device("a-viewer", role="viewer")
CANARY = "what is the airspeed of an unladen swallow"


def demo_snapshot() -> dict:
    from muse.dashboard.server import demo_snapshot as demo

    return demo()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "MUSE_APPS_FILE", "MUSE_CURRENCY", "MUSE_USD_RATE",
                                         "MUSE_DAILY_PAID_CAP_USD", "MUSE_LANG", "MUSE_USD_RATE_DATE"]:
        monkeypatch.delenv(name, raising=False)
    return api.Home(snapshot_fn=demo_snapshot, proxy_running=lambda: True)


def call(home, method: str, path: str, body=None, who: auth.Device = OWNER) -> tuple[int, dict]:
    resp = api.dispatch(home, who, method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


def ok(home, method: str, path: str, body=None, status: int = 200) -> dict:
    got, payload = call(home, method, path, body)
    assert got == status, (got, payload)
    return payload


def reply(status: int, headers: dict | None = None, text: str | None = None, error: str | None = None) -> app_ask.RouterReply:
    if text is not None:
        body = json.dumps({"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 12, "completion_tokens": 34}})
    else:
        body = json.dumps({"error": {"message": error or ""}})
    return app_ask.RouterReply(status, {k.lower(): v for k, v in (headers or {}).items()}, body.encode())


def script(home, *replies: app_ask.RouterReply) -> list[dict]:
    """Make the router answer with these replies in turn; returns the list of what was sent."""
    sent: list[dict] = []
    queue = list(replies)

    def send(messages, *, privacy, task, allow_paid):
        sent.append({"messages": messages, "privacy": privacy, "task": task, "allow_paid": allow_paid})
        return queue.pop(0)

    app_api.backend(home).router_send = send
    return sent


# ---------------------------------------------------------------- money


def test_a_new_home_shows_nt30_and_says_the_router_still_has_its_own_default(home):
    money = ok(home, "GET", "/v1/status")["money"]
    assert money["currency"] == "TWD" and money["rate"] == "32" and money["rate_source"] == "fixed setting"
    assert money["rate_date"] == app_money.DEFAULT_RATE_DATE
    assert money["spent_today"] == {"usd": "0.03", "local": "1", "approx": True}  # NT$0.96, shown as about NT$1
    assert money["daily_limit"] == {"usd": "0.9375", "local": "30", "approx": False, "source": "default", "router_usd": "1.00",
                                    "in_step": False}


def test_the_router_default_is_unchanged_and_nothing_is_written_by_reading(home, tmp_path):
    from muse.ration import parse_paid_cap

    assert parse_paid_cap(None) == Decimal("1.00") and parse_paid_cap("") == Decimal("1.00")
    ok(home, "GET", "/v1/status")
    ok(home, "GET", "/v1/settings")
    assert not (tmp_path / "settings.env").exists()  # a GET never writes a setting


def test_storing_the_first_key_writes_the_default_limit_for_the_router(home, tmp_path):
    got = ok(home, "POST", "/v1/keys/groq", {"key": "gsk_" + "a1B2" * 8})
    assert got["limit_written"] is True and got["restart"]["needed"] is True
    text = (tmp_path / "settings.env").read_text()
    assert "MUSE_DAILY_PAID_CAP_USD=0.9375" in text
    limit = ok(home, "GET", "/v1/status")["money"]["daily_limit"]
    assert limit["local"] == "30" and limit["source"] == "setting" and limit["in_step"] is True and limit["router_usd"] == "0.9375"
    assert ok(home, "POST", "/v1/keys/groq", {"key": "gsk_" + "c3D4" * 8})["limit_written"] is False  # only once


def test_a_limit_someone_set_is_never_overwritten(home, monkeypatch):
    monkeypatch.setenv("MUSE_DAILY_PAID_CAP_USD", "5")
    assert ok(home, "POST", "/v1/keys/groq", {"key": "gsk_" + "a1B2" * 8})["limit_written"] is False
    limit = ok(home, "GET", "/v1/status")["money"]["daily_limit"]
    assert limit["usd"] == "5.00" and limit["local"] == "160" and limit["approx"] is False and limit["in_step"] is True
    monkeypatch.setenv("MUSE_DAILY_PAID_CAP_USD", "0")
    assert ok(home, "GET", "/v1/status")["money"]["daily_limit"]["local"] == "0"  # zero means never spend, and says so


@pytest.mark.parametrize("usd, local, approx", [("0", "0", False), ("0.04", "1", True), ("1", "32", False), ("0.0125", "0", True),
                                               ("1.2", "38", True)])
def test_amounts_are_decimal_strings_and_say_when_they_are_rounded(usd, local, approx):
    got = app_money.amount(usd, {})
    assert got["local"] == local and got["approx"] is approx and Decimal(got["usd"]) == Decimal(usd)
    assert "e" not in got["usd"].lower() and len(got["usd"].split(".")[1]) >= 2


def test_other_currencies_and_a_bad_rate(home, monkeypatch):
    assert app_money.amount("1.5", {"MUSE_CURRENCY": "USD"}) == {"usd": "1.50", "local": "1.50", "approx": False}
    assert app_money.daily_limit({"MUSE_CURRENCY": "USD"})["usd"] == "1.00"  # no NT$30 rule outside TWD: the router's own default
    assert app_money.rate({"MUSE_USD_RATE": "abc"})[0] == Decimal("32") and app_money.rate({"MUSE_USD_RATE": "-3"})[0] == Decimal("32")
    assert app_money.amount("1", {"MUSE_USD_RATE": "31.5"})["local"] == "32" and app_money.amount("1", {"MUSE_USD_RATE": "31.5"})["approx"]


# ---------------------------------------------------------------- ask


def test_an_answer_carries_the_grey_line_as_data_and_the_details(home):
    sent = script(home, reply(200, {"x-muse-provider": "groq", "x-muse-deployment": "groq-gpt-oss-20b", "x-muse-cost-usd": "0",
                                   "x-muse-task": "everyday", "x-muse-min-output": "raised:256->1024"}, text="42."))
    got = ok(home, "POST", "/v1/ask", {"text": CANARY})
    answer = got["answer"]
    assert got["streaming"] is False and answer["status"] == "answered" and answer["text"] == "42." and answer["question"] == CANARY
    route = answer["route"]
    assert route["provider"] == "groq" and route["name"] == "Groq" and route["cache"] is False and route["local"] is False
    assert route["cost"] == {"usd": "0.00", "local": "0", "approx": False}
    assert route["line"]["key"] == "free_refill" and route["line"]["params"]["service"] == "Groq"
    assert route["refill_hint"] == route["line"]["params"]["refill_at"] and route["refill_hint"].endswith("Z")
    assert answer["details"] == {"deployment": "groq-gpt-oss-20b", "task": "everyday", "tokens_in": 12, "tokens_out": 34,
                                 "min_output": True, "empty_reasoning": False, "privacy": "standard"}
    assert [a["id"] for a in answer["handoff"]["apps"]] == ["chatgpt", "claude", "gemini"]
    claude = answer["handoff"]["apps"][1]
    assert claude["url_app"].startswith("claude://claude.ai/new?q=what%20is") and claude["url_web"] == "https://claude.ai/new"
    assert "url_app" not in answer["handoff"]["apps"][0]
    assert sent == [{"messages": [{"role": "user", "content": CANARY}], "privacy": "standard", "task": "everyday", "allow_paid": False}]


@pytest.mark.parametrize("headers, key", [
    ({"x-muse-provider": "cache", "x-muse-cache": "hit"}, "cache"),
    ({"x-muse-provider": "ollama", "x-muse-cost-usd": "0"}, "local"),
    ({"x-muse-provider": "deepseek", "x-muse-cost-usd": "0.04"}, "paid"),
    ({"x-muse-provider": "novita", "x-muse-cost-usd": "0"}, "free"),
    ({}, "unknown"),
])
def test_the_line_key_follows_who_answered_and_what_it_cost(home, headers, key):
    script(home, reply(200, headers, text="ok"))
    line = ok(home, "POST", "/v1/ask", {"text": "hi"})["answer"]["route"]["line"]
    assert line["key"] == key
    if key == "paid":
        assert line["params"]["cost"] == {"usd": "0.04", "local": "1", "approx": True}


def test_a_refusal_gives_exactly_the_options_of_the_trailer_in_the_fixed_order(home):
    trailer = "[muse] free quota is used up until later | options=pay:0.04,handoff,wait:2026-11-13T16:00:00Z"
    script(home, reply(429, {"x-muse-ration": "defer:quota", "retry-after": "600"}, error=trailer))
    answer = ok(home, "POST", "/v1/ask", {"text": "hello there"})["answer"]
    assert answer["status"] == "choices" and answer["reason"] == "used_up" and "text" not in answer
    assert list(answer["choices"]) == ["wait", "handoff", "pay"]
    assert answer["choices"]["wait"] == {"until": "2026-11-13T16:00:00Z"}
    assert answer["choices"]["pay"] == {"usd": "0.04", "local": "1", "approx": True}
    assert [a["id"] for a in answer["choices"]["handoff"]["apps"]] == ["chatgpt", "claude", "gemini"]


@pytest.mark.parametrize("trailer, want", [
    ("x | options=wait:2026-11-13T16:00:00Z", ["wait"]),
    ("x | options=handoff", ["handoff"]),
    ("x | options=handoff,pay:1.5", ["handoff", "pay"]),
    ("x | options=none", []),
])
def test_only_the_real_choices_appear(home, trailer, want):
    script(home, reply(429, {"x-muse-ration": "refuse:paid-not-allowed"}, error=trailer))
    answer = ok(home, "POST", "/v1/ask", {"text": "hello"})["answer"]
    assert list(answer["choices"]) == want and answer["reason"] == "paid_needed"


def test_a_provider_limit_without_a_trailer_offers_only_a_wait_when_a_time_is_known(home):
    script(home, reply(429, {"x-muse-wait": "provider-limit", "retry-after": "120"}, error="rate limited"),
           reply(429, {}, error="rate limited"))
    first = ok(home, "POST", "/v1/ask", {"text": "a"})["answer"]
    assert list(first["choices"]) == ["wait"] and first["reason"] == "busy"
    assert ok(home, "POST", "/v1/ask", {"text": "b"})["answer"]["choices"] == {}


@pytest.mark.parametrize("status, headers, error, reason", [
    (403, {"x-muse-privacy": "private"}, "[muse] no deployment may receive a private request", "private_no_service"),
    (0, {}, "", "router_unreachable"),
    (500, {}, "boom", "busy"),
    (400, {}, "bad", "failed"),
])
def test_failures_have_a_reason_and_never_the_routers_words(home, status, headers, error, reason):
    script(home, reply(status, headers, error=error))
    answer = ok(home, "POST", "/v1/ask", {"text": "secret plan", "privacy": "private"})["answer"]
    assert answer["status"] == "failed" and answer["reason"] == reason
    assert error == "" or error not in json.dumps(answer)


def test_an_empty_answer_is_a_failure_not_a_blank(home):
    script(home, reply(200, {"x-muse-provider": "groq", "x-muse-answer": "empty-reasoning"}, text=""))
    assert ok(home, "POST", "/v1/ask", {"text": "x"})["answer"]["reason"] == "empty"


def test_a_paused_home_does_not_ask(home):
    sent = script(home)
    home.set_paused(True)
    assert ok(home, "POST", "/v1/ask", {"text": "x"})["answer"] ["reason"] == "paused" and sent == []


def test_ask_checks_its_input(home):
    script(home)
    for body in ({}, {"text": ""}, {"text": "   "}, {"text": "x" * 20001}, {"text": "x", "privacy": "secret"},
                 {"text": "x", "task": "huge"}, {"text": "x", "attachments": ["nope"]}, {"text": "x", "surprise": 1},
                 {"text": "x", "attachments": "feeds"}):
        assert_error(*call(home, "POST", "/v1/ask", body), 400, "invalid_request")
    assert_error(*call(home, "POST", "/v1/ask", {"text": "x", "conversation_id": "0123456789abcdef"}), 404, "not_found")
    assert_error(*call(home, "GET", "/v1/ask/0123456789abcdef"), 404, "not_found")
    for who in (HOST, VIEWER):
        assert_error(*call(home, "POST", "/v1/ask", {"text": "x"}, who), 403, "forbidden")
        assert_error(*call(home, "GET", "/v1/conversations", None, who), 403, "forbidden")


def test_conversations_are_kept_bounded_private_and_can_be_deleted(home, tmp_path, caplog):
    import os
    import stat

    caplog.set_level(logging.DEBUG)
    sent = script(home, reply(200, {"x-muse-provider": "groq"}, text="first answer"),
                  reply(200, {"x-muse-provider": "groq"}, text="second answer"))
    first = ok(home, "POST", "/v1/ask", {"text": CANARY})
    cid = first["conversation_id"]
    second = ok(home, "POST", "/v1/ask", {"text": "and a coconut?", "conversation_id": cid})
    assert second["conversation_id"] == cid
    # The earlier turn goes along as history, oldest first.
    assert [m["content"] for m in sent[1]["messages"]] == [CANARY, "first answer", "and a coconut?"]
    listed = ok(home, "GET", "/v1/conversations")
    assert [(c["id"], c["turns"], c["preview"]) for c in listed["conversations"]] == [(cid, 2, CANARY)]
    turns = ok(home, "GET", f"/v1/conversations/{cid}")["turns"]
    assert [t["text"] for t in turns] == ["first answer", "second answer"]
    assert ok(home, "GET", f"/v1/ask/{turns[0]['id']}")["answer"]["text"] == "first answer"
    path = app_ask.store_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Never logged, never in a record.
    assert CANARY not in caplog.text and "first answer" not in caplog.text
    records = json.dumps(ok(home, "GET", "/v1/receipts")["records"])
    assert CANARY not in records and "first answer" not in records and "Answered a question with the service groq" in records
    assert ok(home, "DELETE", f"/v1/conversations/{cid}") == {"deleted": True}
    assert CANARY.encode() not in path.read_bytes()  # erased from the file, not marked
    assert_error(*call(home, "GET", f"/v1/conversations/{cid}"), 404, "not_found")
    assert_error(*call(home, "DELETE", f"/v1/conversations/{cid}"), 404, "not_found")


def test_the_store_drops_the_oldest(monkeypatch, tmp_path):
    monkeypatch.setattr(app_ask, "MAX_CONVERSATIONS", 3)
    monkeypatch.setattr(app_ask, "MAX_TURNS", 2)
    now = [1000.0]
    store = app_ask.ConversationStore({"MUSE_HOME": str(tmp_path)}, clock=lambda: now[0])
    ids = []
    for n in range(5):
        now[0] += 1
        ids.append(store.add_turn(None, f"q{n}", {}, {"status": "answered", "text": f"a{n}"})[0])
    assert [c["id"] for c in store.list()] == ids[:1:-1]
    for n in range(4):
        now[0] += 1
        store.add_turn(ids[4], f"more{n}", {}, {"status": "answered", "text": "a"})
    assert [t["question"] for t in store.turns(ids[4])] == ["more2", "more3"]
    with pytest.raises(KeyError):
        store.add_turn("ffffffffffffffff", "q", {}, {})


def test_the_real_send_uses_the_homes_own_app_and_the_documented_headers(home, monkeypatch):
    from muse import apps, queue

    seen: list[tuple[str, dict, dict]] = []

    def post(url, headers, body, timeout):
        seen.append((url, dict(headers), json.loads(body)))
        return queue.Reply(200, {"x-muse-provider": "groq"}, json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode())

    monkeypatch.setattr(queue, "_post", post)
    monkeypatch.setenv("MUSE_PORT", "4999")
    ok(home, "POST", "/v1/ask", {"text": "hello", "privacy": "private", "task": "light"})
    url, headers, body = seen[0]
    assert url == "http://127.0.0.1:4999/v1/chat/completions" and body["model"] == "auto"
    assert headers["x-muse-privacy"] == "private" and headers["x-muse-task"] == "light" and "x-muse-allow-paid" not in headers
    registered = apps.load_apps()
    assert registered["home-ask"].priority == "interactive" and not registered["home-ask"].allow_paid
    assert apps.hash_token(headers["Authorization"].split()[1]) == registered["home-ask"].token_sha256
    app_api.backend(home).router_send([{"role": "user", "content": "x"}], privacy=None, task=None, allow_paid=True)
    assert seen[1][1]["x-muse-allow-paid"] == "true" and apps.load_apps()["home-ask-paid"].allow_paid


# ---------------------------------------------------------------- the three choices

TRAILER = "[muse] used up | options=wait:2026-11-13T16:00:00Z,handoff,pay:0.04"


def refused(home, text: str = "hello", trailer: str = TRAILER, *more: app_ask.RouterReply) -> tuple[str, list[dict]]:
    sent = script(home, reply(429, {"x-muse-ration": "defer:quota"}, error=trailer), *more)
    return ok(home, "POST", "/v1/ask", {"text": text})["answer"]["id"], sent


def test_wait_queues_the_question_with_a_fixed_title_and_the_answer_arrives_later(home):
    from pathlib import Path

    turn, _ = refused(home, CANARY)
    got = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "wait"})
    answer = got["answer"]
    assert answer["status"] == "queued" and answer["until"] == "2026-11-13T16:00:00Z" and got["recorded"] is True
    assert answer["title"] == {"key": "question_for_later", "params": {}}
    job = home.queue().get(answer["job_id"])
    assert job.payload == CANARY and job.task == "everyday" and not job.allow_paid
    running = ok(home, "GET", "/v1/status")["activity"]["running"]
    assert [(r["id"], r["kind"], r["title"]) for r in running] == [
        (job.id, "job", {"key": "question_for_later", "params": {}, "text": None})]
    assert CANARY not in json.dumps(ok(home, "GET", "/v1/status")) and CANARY not in json.dumps(ok(home, "GET", "/v1/jobs"))
    assert_error(*call(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "wait"}), 409, "conflict")  # already chosen
    # The queue's runner finishes it: the next look finds the answer.
    Path(job.out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(job.out_path).write_text("An African or European swallow?\n", encoding="utf-8")
    q = home.queue()
    with q._conn(write=True) as conn:
        conn.execute("UPDATE jobs SET status='done', finished_at=?, provider='groq', cost_usd=0, payload='' WHERE id=?", (time.time(), job.id))
    later = ok(home, "GET", f"/v1/ask/{turn}")["answer"]
    assert later["status"] == "answered" and later["text"] == "An African or European swallow?" and later["route"]["name"] == "Groq"
    done = ok(home, "GET", "/v1/status")["activity"]["done_today"]
    assert [(d["id"], d["ok"], d["title"]["key"]) for d in done] == [(job.id, True, "question_for_later")]


def test_handoff_is_recorded_and_sends_nothing(home):
    turn, sent = refused(home)
    assert_error(*call(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "handoff", "app": "copilot"}), 400, "invalid_request")
    got = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "handoff", "app": "claude"})
    assert got["answer"]["status"] == "handed_off" and got["answer"]["app"] == {"id": "claude", "name": "Claude"}
    assert len(sent) == 1  # nothing more went to the router
    record = ok(home, "GET", "/v1/receipts")["records"][0]
    assert record["action"] == "home.handoff" and "Claude" in record["summary"] and record["egress"] == "none"


def test_pay_under_the_limit_goes_at_once_with_allow_paid(home):
    turn, sent = refused(home, "hello", TRAILER, reply(200, {"x-muse-provider": "deepseek", "x-muse-cost-usd": "0.04"}, text="paid answer"))
    got = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "pay"})
    assert got["answer"]["status"] == "answered" and got["answer"]["route"]["line"]["key"] == "paid"
    assert sent[1]["allow_paid"] is True and sent[0]["allow_paid"] is False
    record = ok(home, "GET", "/v1/receipts")["records"][0]
    assert record["action"] == "home.ask" and record["cost"] == {"currency": "USD", "minor": 4} and record["egress"] == "service:deepseek"


def test_pay_over_the_limit_needs_a_second_explicit_call(home):
    trailer = "[muse] used up | options=wait:2026-11-13T16:00:00Z,pay:1.20"
    turn, sent = refused(home, "a long report", trailer, reply(200, {"x-muse-provider": "deepseek", "x-muse-cost-usd": "1.20"}, text="done"))
    got = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "pay"})["answer"]
    assert got["status"] == "over_limit" and len(sent) == 1  # nothing was sent, nothing spent
    over = got["over_limit"]
    # spent 0.03 + 1.20 = 1.23 against 0.9375: over by 0.2925, about NT$9.
    assert over["over"] == {"usd": "0.2925", "local": "9", "approx": True} and over["limit"]["local"] == "30"
    assert over["pay"]["usd"] == "1.20" and over["raise_today"]["to"]["usd"] == "1.23"
    assert list(got["choices"]) == ["wait", "pay"]  # the other choices are still there
    again = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "pay"})["answer"]
    assert again["status"] == "over_limit" and len(sent) == 1  # asking twice without the flag changes nothing
    paid = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "pay", "raise_today": True})["answer"]
    assert paid["status"] == "answered" and sent[1]["allow_paid"] is True
    assert app_api.backend(home).limit_today_usd() == Decimal("1.23")
    actions = [r["action"] for r in ok(home, "GET", "/v1/receipts")["records"]]
    assert actions[:2] == ["home.ask", "home.limit"]
    assert ok(home, "GET", "/v1/settings")["settings"]["daily_limit"]["local"] == "30"  # today only: the setting is untouched


def test_when_the_routers_own_cap_refuses_the_answer_says_so(home):
    turn, _ = refused(home, "hello", TRAILER, reply(429, {"x-muse-ration": "refuse:paid-cap"}, error="[muse] cap | options=wait:2026-11-14T00:00:00Z"))
    got = ok(home, "POST", f"/v1/ask/{turn}/choose", {"choice": "pay"})["answer"]
    assert got["status"] == "failed" and got["reason"] == "router_limit"


def test_a_choice_that_was_not_offered_is_refused(home):
    turn, _ = refused(home, "hello", "x | options=wait:2026-11-13T16:00:00Z")
    for body in ({"choice": "pay"}, {"choice": "handoff"}, {"choice": "steal"}, {}):
        assert_error(*call(home, "POST", f"/v1/ask/{turn}/choose", body), 400, "invalid_request")
    script(home, reply(200, {"x-muse-provider": "groq"}, text="hi"))
    answered = ok(home, "POST", "/v1/ask", {"text": "x"})["answer"]["id"]
    assert_error(*call(home, "POST", f"/v1/ask/{answered}/choose", {"choice": "wait"}), 409, "conflict")


# ---------------------------------------------------------------- chores


@pytest.fixture()
def folders(home, tmp_path):
    from muse.home import folders as folder_list

    feeds, out = tmp_path / "feeds", tmp_path / "out"
    feeds.mkdir()
    out.mkdir()
    folder_list.add("feeds", feeds)
    folder_list.add("documents", out, output=True)
    return feeds, out


def test_a_chore_is_understood_then_saved_listed_paused_edited_run_and_deleted(home, folders):
    feeds, out = folders
    sentence = "Every Monday 08:00, summarise my feeds"
    unclear = ok(home, "POST", "/v1/chores/understand", {"sentence": "Summarise my feeds"})
    assert unclear["saved"] is False and unclear["understood"]["ok"] is False
    assert {q["code"] for q in unclear["understood"]["questions"]} == {"when", "sources"}
    assert ok(home, "GET", "/v1/chores")["chores"] == []  # nothing was saved by asking
    understood = ok(home, "POST", "/v1/chores/understand", {"sentence": sentence, "folders": ["feeds"], "out": "documents"})["understood"]
    assert understood["ok"] and understood["spec"]["sources"] == [str(feeds)] and understood["spec"]["dest"] == str(out)
    assert understood["budget"] == "up to 40 requests, nothing paid" and "Monday" in understood["when"]
    made = ok(home, "POST", "/v1/chores", {"understood": understood}, status=201)["chore"]
    assert made["sentence"] == sentence and made["enabled"] and made["title"] == {"key": "chore", "params": {}, "text": sentence}
    assert set(made["lines"]) == {"what", "when", "where", "budget", "permission"} and made["paid"] is False
    cid = made["id"]
    assert [c["id"] for c in ok(home, "GET", "/v1/chores")["chores"]] == [cid]
    paused = ok(home, "PATCH", f"/v1/chores/{cid}", {"enabled": False})["chore"]
    assert paused["enabled"] is False and paused["next_run"] is None
    assert ok(home, "PATCH", f"/v1/chores/{cid}", {"enabled": True})["chore"]["next_run"]
    edited = ok(home, "POST", "/v1/chores/understand", {"sentence": "Every day at 07:30, write me a daily brief"})["understood"]
    assert ok(home, "PATCH", f"/v1/chores/{cid}", {"understood": edited})["chore"]["sentence"].startswith("Every day at 07:30")

    ran = []
    backend = app_api.backend(home)

    def run_chore(store, run):
        from muse.skills import chores

        ran.append(run.id)
        chores._finish(store, run, "done", "3 source(s) summarised, 0 not read; saved")

    backend.run_chore = run_chore
    started = ok(home, "POST", f"/v1/chores/{cid}/run", status=202)
    assert started["run"]["status"] == "running" and started["title"]["key"] == "chore"
    backend.wait_chore(started["run"]["id"])
    history = ok(home, "GET", f"/v1/chores/{cid}/history")["runs"]
    assert [(r["id"], r["status"], r["trigger"]) for r in history] == [(ran[0], "done", "manual")]
    done = ok(home, "GET", "/v1/status")["activity"]["done_today"]
    assert done[0]["kind"] == "chore" and done[0]["title"]["text"].startswith("Every day at 07:30") and done[0]["ok"] is True
    home.set_paused(True)
    assert_error(*call(home, "POST", f"/v1/chores/{cid}/run"), 409, "conflict")
    assert ok(home, "DELETE", f"/v1/chores/{cid}")["deleted"] is True
    assert_error(*call(home, "GET", f"/v1/chores/{cid}/history"), 404, "not_found")
    assert [r["action"] for r in ok(home, "GET", "/v1/receipts")["records"]][:1] == ["home.chore"]


def test_an_understood_object_is_checked_again_and_never_gets_money(home, folders, tmp_path):
    understood = ok(home, "POST", "/v1/chores/understand", {"sentence": "Every Monday 08:00, summarise my feeds", "folders": ["feeds"]})["understood"]
    bad = [
        {**understood, "ok": False},
        {**understood, "spec": {**understood["spec"], "freq": "hourly"}},
        {**understood, "spec": {**understood["spec"], "sources": [str(tmp_path / "not-there")]}},
        {**understood, "sentence": ""},
        "not an object",
    ]
    for item in bad:
        assert_error(*call(home, "POST", "/v1/chores", {"understood": item}), 400, "invalid_request")
    greedy = {**understood, "budget_values": {"requests": 9999, "tokens": 10**9, "paid_usd": 50}}
    made = ok(home, "POST", "/v1/chores", {"understood": greedy}, status=201)["chore"]
    assert made["budget"]["requests"] == 40 and made["budget"].get("paid_usd", 0) == 0 and made["paid"] is False
    assert_error(*call(home, "POST", "/v1/chores/understand", {"sentence": "x", "folders": ["secrets"]}), 400, "invalid_request")
    assert_error(*call(home, "POST", "/v1/chores/understand", {"sentence": "x", "out": "feeds"}), 400, "invalid_request")  # read-only folder
    assert_error(*call(home, "PATCH", f"/v1/chores/{made['id']}", {}), 400, "invalid_request")
    assert_error(*call(home, "PATCH", "/v1/chores/zzzzzzzz", {"enabled": False}), 404, "not_found")
    assert call(home, "GET", "/v1/chores", None, VIEWER)[0] == 200
    assert_error(*call(home, "POST", f"/v1/chores/{made['id']}/run", None, VIEWER), 403, "forbidden")


def test_the_homes_own_cards_carry_a_title_key_and_a_hosts_card_cannot(home):
    card_id = home.cards.create(kind="save_file", title="Save a file", thing="digest.md", under="Weekly digest chore",
                                extra={"size_bytes": 10}, title_key="save_file_chore", params={"label": "Weekly digest"})
    card = ok(home, "GET", f"/v1/cards/{card_id}")["card"]
    assert card["title_key"] == "save_file_chore" and card["params"] == {"label": "Weekly digest"} and card["title"] == "Save a file"
    plain = ok(home, "POST", "/v1/cards", {"kind": "message", "title": "Send", "thing": "x", "extra": {"preview": "hi"}}, status=201)["card"]
    assert plain["title_key"] is None and plain["params"] == {}
    assert_error(*call(home, "POST", "/v1/cards", {"kind": "message", "title": "Send", "thing": "x", "extra": {"preview": "hi"},
                                                    "title_key": "save_file_chore"}), 400, "invalid_request")
    from muse.home.cards import CardError

    for key, params in (("Bad Key", None), ("ok", {"x": ["list"]}), ("ok", {"x": "a\nb"}), (None, {"x": "y"}),
                        ("ok", {"k": "gsk_" + "a1B2c3D4" * 5})):
        with pytest.raises(CardError):
            home.cards.create(kind="save_file", title="Save a file", thing="a.md", extra={"size_bytes": 1}, title_key=key, params=params)


def test_cards_made_before_the_new_columns_still_read(home):
    import sqlite3

    from muse.home import cards

    card_id = home.cards.create(kind="save_file", title="Save a file", thing="a.md", extra={"size_bytes": 1})
    conn = sqlite3.connect(str(home.cards.path))
    conn.execute("ALTER TABLE cards DROP COLUMN title_key")
    conn.execute("ALTER TABLE cards DROP COLUMN params")
    conn.commit()
    conn.close()
    card = cards.CardStore().get(card_id)  # the columns are added back on open
    assert card["title_key"] is None and card["params"] == {}


# ---------------------------------------------------------------- services


def test_services_lists_what_is_connected_what_to_add_and_the_rest(home, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 20)
    got = ok(home, "GET", "/v1/services")
    by_id = {s["id"]: s for s in got["connected"]}
    gemini = by_id["gemini"]
    assert gemini["badge"] == "standard" and gemini["state"] == "ok" and gemini["estimated"] is True and gemini["can_test"]
    assert gemini["left"]["remaining"] <= gemini["left"]["limit"] and gemini["refill_at"].endswith("Z")
    assert gemini["good_for"] and gemini["checked_on"] == "2026-11-12"
    assert by_id["ollama"]["badge"] == "local" and by_id["ollama"]["unlimited"] and not by_id["ollama"]["can_disconnect"]
    assert by_id["cohere"]["state"] == "used_up" and by_id["openrouter"]["state"] == "busy" and by_id["openrouter"]["retry_at"]
    assert "jina" not in by_id  # not connected
    ranks = [a["rank"] for a in got["add"]]
    assert ranks == sorted(ranks) and all(a["method"] in ("one_tap", "guided") for a in got["add"])
    assert all(a["card"] in ("yes", "no", "unknown") and a["phone"] in ("yes", "no", "unknown") for a in got["add"])
    assert all(a["key_host"] is None or "/" not in a["key_host"] for a in got["add"])
    assert {s["id"] for s in got["subscriptions"]} == {"chatgpt", "claude", "gemini"} and got["resources"]
    assert got["local_runtime"] is True
    assert app_api.badge_of({"kind": "cloud", "policy": {"trains_on_inputs": "no", "human_review": "no"}}) == "private"
    assert app_api.badge_of({"kind": "cloud", "policy": {"trains_on_inputs": "no", "human_review": "unknown"}}) == "standard"
    assert call(home, "GET", "/v1/services", None, HOST)[0] == 200


def test_a_key_is_tested_with_one_request_and_never_shown(home, monkeypatch):
    from muse import connect

    key = "gsk_" + "a1B2" * 8
    ok(home, "POST", "/v1/keys/groq", {"key": key})
    seen = []

    def get(url, headers, timeout):
        seen.append((url, dict(headers)))
        return statuses.pop(0)

    statuses = [200, 401, 429, 500]
    monkeypatch.setattr(connect, "_default_http_get", get)
    results = [ok(home, "POST", "/v1/services/groq/test") for _ in range(4)]
    assert [r["result"] for r in results] == ["ok", "refused", "limited", "failed"]
    assert all(r["sent_a_prompt"] is False and key not in json.dumps(r) for r in results)
    assert len(seen) == 4 and seen[0][0].startswith("https://") and seen[0][1] == {"Authorization": f"Bearer {key}"}
    assert_error(*call(home, "POST", "/v1/services/cerebras/test"), 409, "conflict")  # not connected
    assert_error(*call(home, "POST", "/v1/services/nope/test"), 404, "not_found")
    assert_error(*call(home, "POST", "/v1/services/ollama/test"), 400, "invalid_request")


def test_disconnect_removes_the_key_from_this_home(home, tmp_path):
    ok(home, "POST", "/v1/keys/groq", {"key": "gsk_" + "a1B2" * 8})
    assert "GROQ_API_KEY" in (tmp_path / "settings.env").read_text()
    got = ok(home, "DELETE", "/v1/services/groq")
    assert got["removed"] is True and got["still_set_elsewhere"] is False and got["restart"]["needed"] is True and got["recorded"]
    assert "GROQ_API_KEY" not in (tmp_path / "settings.env").read_text()
    assert_error(*call(home, "DELETE", "/v1/services/groq"), 404, "not_found")
    assert ok(home, "GET", "/v1/receipts")["records"][0]["action"] == "key.remove"
    assert ok(home, "GET", "/v1/status")["restart_pending"] is True


def test_restart_is_refused_clearly_when_the_home_does_not_manage_the_router(home, monkeypatch):
    err = assert_error(*call(home, "POST", "/v1/home/restart-proxy"), 409, "conflict")
    assert "does not manage the router" in err["message"]
    import subprocess

    ran = []
    monkeypatch.setattr(app_api.AppBackend, "manages_proxy", lambda self: (True, ["muse-restart-for-test"]))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"", b""))
    got = ok(home, "POST", "/v1/home/restart-proxy")
    assert got["restarted"] is True and ran == [["muse-restart-for-test"]] and got["recorded"] is True
    assert ok(home, "GET", "/v1/status")["restart_pending"] is False
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"secret detail"))
    err = assert_error(*call(home, "POST", "/v1/home/restart-proxy"), 503, "unavailable")
    assert "secret detail" not in err["message"]


# ---------------------------------------------------------------- the one-tap flow

ISSUED_KEY = "sk-or-v1-" + "0123abcd" * 6


class _Exchange(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        ok_code = body.get("code") == "good-code"
        data = json.dumps({"key": ISSUED_KEY} if ok_code else {"error": "bad code"}).encode()
        self.send_response(200 if ok_code else 400)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def oauth(home):
    from muse import connect

    _Exchange.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Exchange)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    backend = app_api.backend(home)
    backend.flows.endpoints_for = lambda provider: connect.Endpoints(f"{base}/auth", f"{base}/keys", allow_insecure=True)
    yield backend
    backend.flows.close()
    server.shutdown()
    server.server_close()


def _wait_for(home, state_id: str, want: str) -> dict:
    for _ in range(100):
        got = ok(home, "GET", f"/v1/connect/flows/{state_id}")
        if got["status"] == want:
            return got
        time.sleep(0.05)
    raise AssertionError(got)


def _browser(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code


def test_one_tap_on_the_same_computer_listens_exchanges_and_stores(home, oauth, tmp_path):
    started = ok(home, "POST", "/v1/connect/openrouter/start", {"same_device": True}, status=201)
    assert started["mode"] == "loopback" and started["status"] == "pending" and started["name"] == "OpenRouter"
    assert started["facts"] == {"free_per_day": 50, "may_train": "yes"}
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started["url"]).query))
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["callback_url"].startswith("http://localhost:") and "/cb/" in query["callback_url"]
    assert ok(home, "GET", f"/v1/connect/flows/{started['state_id']}")["status"] == "pending"
    # A stray request with the wrong state does not complete it.
    assert _browser(query["callback_url"].replace("localhost", "127.0.0.1") + "?code=good-code&state=wrong") == 400
    flow2 = ok(home, "POST", "/v1/connect/openrouter/start", {"same_device": True}, status=201)
    q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(flow2["url"]).query))
    assert _browser(q2["callback_url"].replace("localhost", "127.0.0.1") + f"?code=good-code&state={q2['state']}") == 200
    done = _wait_for(home, flow2["state_id"], "connected")
    assert done["gained"]["name"] == "OpenRouter" and set(done["gained"]["text"]) == {"en", "zh-TW"}
    assert done["restart"]["needed"] is True and ISSUED_KEY not in json.dumps(done)
    assert _wait_for(home, started["state_id"], "failed")["reason"] == "bad_callback"
    text = (tmp_path / "settings.env").read_text()
    assert f"OPENROUTER_API_KEY={ISSUED_KEY}" in text and "MUSE_DAILY_PAID_CAP_USD=0.9375" in text
    sent = _Exchange.seen[-1]
    assert sent["code"] == "good-code" and sent["code_challenge_method"] == "S256" and len(sent["code_verifier"]) >= 43
    records = ok(home, "GET", "/v1/receipts")["records"]
    assert records[0]["action"] == "key.store" and ISSUED_KEY not in json.dumps(records)


def test_one_tap_from_a_remote_client_uses_a_pasted_code(home, oauth, tmp_path):
    started = ok(home, "POST", "/v1/connect/openrouter/start", {}, status=201)  # the default: the browser is elsewhere
    assert started["mode"] == "paste_code" and "callback_url" not in started["url"] and "state=" not in started["url"]
    sid = started["state_id"]
    assert_error(*call(home, "POST", f"/v1/connect/flows/{sid}/code", {"code": "has spaces in it"}), 400, "invalid_request")
    failed = ok(home, "POST", f"/v1/connect/flows/{sid}/code", {"code": "wrong-code"})
    assert failed["status"] == "failed" and failed["reason"] == "exchange_failed"
    assert_error(*call(home, "POST", f"/v1/connect/flows/{sid}/code", {"code": "good-code"}), 409, "conflict")  # it has ended
    again = ok(home, "POST", "/v1/connect/openrouter/start", {}, status=201)
    done = ok(home, "POST", f"/v1/connect/flows/{again['state_id']}/code", {"code": "good-code"})
    assert done["status"] == "connected" and ISSUED_KEY not in json.dumps(done)
    assert f"OPENROUTER_API_KEY={ISSUED_KEY}" in (tmp_path / "settings.env").read_text()


def test_flows_time_out_are_cancelled_and_are_capped(home, oauth):
    now = [time.time()]
    oauth.flows.clock = lambda: now[0]
    first = ok(home, "POST", "/v1/connect/openrouter/start", {}, status=201)
    now[0] += app_connect.TIMEOUT_S + 1
    assert ok(home, "GET", f"/v1/connect/flows/{first['state_id']}") ["reason"] == "timeout"
    ids = [ok(home, "POST", "/v1/connect/openrouter/start", {}, status=201)["state_id"] for _ in range(app_connect.MAX_FLOWS)]
    assert_error(*call(home, "POST", "/v1/connect/openrouter/start", {}), 409, "conflict")
    assert ok(home, "DELETE", f"/v1/connect/flows/{ids[0]}") == {"cancelled": True}
    assert ok(home, "GET", f"/v1/connect/flows/{ids[0]}")["reason"] == "cancelled"
    listening = ok(home, "POST", "/v1/connect/openrouter/start", {"same_device": True}, status=201)
    assert ok(home, "DELETE", f"/v1/connect/flows/{listening['state_id']}") == {"cancelled": True}
    now[0] += app_connect.KEEP_DONE_S + 1
    assert_error(*call(home, "GET", f"/v1/connect/flows/{ids[0]}"), 404, "not_found")  # forgotten
    assert_error(*call(home, "POST", "/v1/connect/groq/start", {}), 400, "invalid_request")  # not a one-tap service
    assert_error(*call(home, "GET", "/v1/connect/flows/nothex"), 404, "not_found")


def test_only_the_code_route_may_carry_something_key_shaped(home, oauth):
    sid = ok(home, "POST", "/v1/connect/openrouter/start", {}, status=201)["state_id"]
    shaped = "sk-or-v1-" + "ab12" * 10
    assert call(home, "POST", f"/v1/connect/flows/{sid}/code", {"code": shaped})[0] == 200  # reaches the handler
    assert_error(*call(home, "POST", "/v1/ask", {"text": f"my key is {shaped}"}), 400, "invalid_request")
    assert_error(*call(home, "PATCH", "/v1/settings", {"language": shaped}), 400, "invalid_request")


# ---------------------------------------------------------------- settings, memory, verify, about, presets


def test_settings_are_read_and_changed_through_the_settings_file(home, tmp_path):
    before = ok(home, "GET", "/v1/settings")["settings"]
    assert before["daily_limit"]["local"] == "30" and before["default_privacy"] == "standard" and before["currency"] == "TWD"
    got = ok(home, "PATCH", "/v1/settings", {"daily_limit_local": 50, "default_privacy": "private", "language": "zh-TW"})
    assert got["changed"] == ["daily_limit", "default_privacy", "language"] and got["restart"]["needed"] is True and got["recorded"]
    after = got["settings"]
    assert after["daily_limit"] == {"usd": "1.5625", "local": "50", "approx": False, "source": "setting", "router_usd": "1.5625",
                                    "in_step": True}
    assert after["default_privacy"] == "private" and after["language"] == "zh-TW" and after["restart_pending"] is True
    text = (tmp_path / "settings.env").read_text()
    assert "MUSE_DAILY_PAID_CAP_USD=1.5625" in text and "MUSE_DEFAULT_PRIVACY=private" in text and "MUSE_LANG=zh-TW" in text
    assert ok(home, "GET", "/v1/status")["money"]["daily_limit"]["local"] == "50"
    only_language = ok(home, "PATCH", "/v1/settings", {"language": "en"})
    assert only_language["restart"]["needed"] is False
    rate = ok(home, "PATCH", "/v1/settings", {"usd_rate": "31.5"})["settings"]
    assert rate["rate"] == "31.5" and rate["rate_date"] == time.strftime("%Y-%m-%d", time.gmtime())
    assert rate["daily_limit"]["local"] == "49" and rate["daily_limit"]["approx"] is True  # the dollar cap is what is stored
    usd = ok(home, "PATCH", "/v1/settings", {"currency": "USD", "daily_limit_local": "2.5"})["settings"]
    assert usd["currency"] == "USD" and usd["daily_limit"]["local"] == "2.50" and usd["daily_limit"]["usd"] == "2.50"
    assert ok(home, "PATCH", "/v1/settings", {"daily_limit_local": 0})["settings"]["daily_limit"]["local"] == "0.00"
    for body in ({}, {"daily_limit_local": -1}, {"daily_limit_local": "lots"}, {"default_privacy": "public"}, {"language": "fr"},
                 {"currency": "BTC"}, {"usd_rate": 0}, {"OPENROUTER_API_KEY": "x"}, {"daily_limit_local": True}):
        assert_error(*call(home, "PATCH", "/v1/settings", body), 400, "invalid_request")
    assert call(home, "GET", "/v1/settings", None, VIEWER)[0] == 200
    assert_error(*call(home, "PATCH", "/v1/settings", {"language": "en"}, VIEWER), 403, "forbidden")


def test_memory_is_an_honest_stub_and_forget_erases_the_conversations(home):
    script(home, reply(200, {"x-muse-provider": "groq"}, text="hi"))
    ok(home, "POST", "/v1/ask", {"text": CANARY})
    assert ok(home, "GET", "/v1/memory") == {"built": False, "items": [], "note": "nothing_remembered", "conversations": 1}
    gone = ok(home, "DELETE", "/v1/memory")
    assert gone["built"] is False and gone["items_removed"] == 0 and gone["conversations_removed"] == 1
    assert ok(home, "GET", "/v1/conversations")["conversations"] == []


def test_verify_runs_the_supply_chain_check_without_the_network(home, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("verify must not use the network"))
    got = ok(home, "POST", "/v1/verify")
    assert got["result"] in ("ok", "problem", "unknown") and got["lines"]
    assert all(set(line) == {"level", "text"} for line in got["lines"])


def test_about_and_presets(home):
    about = ok(home, "GET", "/v1/about")
    assert about["versions"]["api"] == "1" and about["versions"]["muse"] and about["catalogue"]["checked_on"]
    assert about["features"] == {"recovery": False, "memory": False, "streaming": False, "wallet": False, "export": False}
    presets = ok(home, "GET", "/v1/apps/presets")
    assert presets["endpoint"] == "http://127.0.0.1:4000/v1"
    by_kind = {p["kind"]: p for p in presets["presets"]}
    webui = by_kind["open-webui"]
    assert "URL: http://127.0.0.1:4000/v1   (no trailing slash)" in webui["steps"] and "API Key: {key}" in webui["steps"]
    assert webui["priority"] == "interactive" and webui["verified_on"] and all("{base}" not in s for p in by_kind.values() for s in p["steps"])
    assert any(not p["verified"] and p["verified_on"] is None for p in by_kind.values()) or all(p["verified"] for p in by_kind.values())
    assert_error(*call(home, "GET", "/v1/apps/presets", None, VIEWER), 403, "forbidden")


# ---------------------------------------------------------------- the fake home


def fake_call(home, method: str, path: str, body=None) -> tuple[int, dict]:
    resp = api.dispatch(home, auth.Device(fake.FAKE_DEVICE), method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


@pytest.fixture()
def fakes():
    made = []

    def start(state: str) -> fake.FakeHome:
        made.append(fake.FakeHome(state))
        return made[-1]

    yield start
    for h in made:
        h.close()


APP_GETS = ("/v1/services", "/v1/chores", "/v1/conversations", "/v1/settings", "/v1/about", "/v1/memory", "/v1/apps/presets")


@pytest.mark.parametrize("state", [s for s in fake.STATES if s != "error"])
def test_the_fake_answers_every_new_get_in_every_state(fakes, state):
    home = fakes(state)
    for path in APP_GETS:
        status, payload = fake_call(home, "GET", path)
        assert status == 200, (path, payload)
    services = fake_call(home, "GET", "/v1/services")[1]
    chores = fake_call(home, "GET", "/v1/chores")[1]["chores"]
    if state == "empty":
        assert services["connected"] == [] and chores == [] and services["add"][0]["id"] == "openrouter"
        assert services["add"][0]["method"] == "one_tap" and services["local_runtime"] is False
    else:
        assert {"private", "standard", "local"} <= {s["badge"] for s in services["connected"]}  # the Private badge can be shown
        assert len(chores) == 3 and [c["enabled"] for c in chores].count(False) == 1 and any(c["spare_only"] for c in chores)
        history = fake_call(home, "GET", f"/v1/chores/{chores[0]['id']}/history")[1]
        assert "runs" in history
    money = fake_call(home, "GET", "/v1/status")[1]["money"]
    assert money["daily_limit"]["local"] == "30"


def test_the_fake_error_state_answers_unavailable_on_the_new_routes(fakes):
    home = fakes("error")
    for method, path, body in [("GET", p, None) for p in APP_GETS] + [("POST", "/v1/ask", {"text": "hi"})]:
        assert_error(*fake_call(home, method, path, body), 503, "unavailable")


def test_the_fake_ask_script(fakes):
    normal = fakes("normal")
    answer = fake_call(normal, "POST", "/v1/ask", {"text": "What can you do?"})[1]["answer"]
    assert answer["status"] == "answered" and answer["route"]["name"] == "Groq" and answer["route"]["line"]["key"] == "free_refill"
    assert fake_call(normal, "POST", "/v1/ask", {"text": "你好"})[1]["answer"]["text"].startswith("（假的家）")
    private = fake_call(normal, "POST", "/v1/ask", {"text": "x", "privacy": "private"})[1]["answer"]
    assert private["status"] == "answered" and private["route"]["name"] == "Private demo service"
    local = fake_call(normal, "POST", "/v1/ask", {"text": "x", "privacy": "local"})[1]["answer"]
    assert local["route"]["line"]["key"] == "local"
    assert len(fake_call(normal, "GET", "/v1/conversations")[1]["conversations"]) == 5  # one seeded, four asked

    used = fakes("used")
    refused_ = fake_call(used, "POST", "/v1/ask", {"text": "hello"})[1]["answer"]
    assert refused_["status"] == "choices" and list(refused_["choices"]) == ["wait", "handoff", "pay"]
    assert refused_["choices"]["pay"] == {"usd": "0.04", "local": "1", "approx": True}
    paid = fake_call(used, "POST", f"/v1/ask/{refused_['id']}/choose", {"choice": "pay"})[1]["answer"]
    assert paid["status"] == "answered" and paid["route"]["line"]["key"] == "paid"
    big = fake_call(used, "POST", "/v1/ask", {"text": "x" * 200})[1]["answer"]
    over = fake_call(used, "POST", f"/v1/ask/{big['id']}/choose", {"choice": "pay"})[1]["answer"]
    assert over["status"] == "over_limit" and over["over_limit"]["pay"]["usd"] == "1.20"
    raised = fake_call(used, "POST", f"/v1/ask/{big['id']}/choose", {"choice": "pay", "raise_today": True})[1]["answer"]
    assert raised["status"] == "answered"
    waiting = fake_call(used, "POST", "/v1/ask", {"text": "later"})[1]["answer"]
    assert fake_call(used, "POST", f"/v1/ask/{waiting['id']}/choose", {"choice": "wait"})[1]["answer"]["status"] == "queued"
    none = fake_call(used, "POST", "/v1/ask", {"text": "x", "privacy": "private"})[1]["answer"]
    assert none["status"] == "failed" and none["reason"] == "private_no_service"
    assert fake_call(fakes("empty"), "POST", "/v1/ask", {"text": "x"})[1]["answer"]["reason"] == "router_unreachable"


def test_the_fake_files_answer_has_sources_with_file_paragraph_and_quote(fakes):
    home = fakes("normal")
    got = fake_call(home, "POST", "/v1/ask", {"text": "When is the rent due?", "attachments": ["documents"]})[1]["answer"]
    assert got["status"] == "answered" and got["details"]["skill"] == "files-answer" and got["attachments"] == ["documents"]
    assert got["route"]["line"] == {"key": "free", "params": {"service": "Groq"}}
    (source,) = got["sources"]
    assert source["file"] == "documents/Lease 2026.md" and source["paragraphs"] == [2]
    assert source["quote"].startswith("The rent is NT$18,000 a month")
    zh = fake_call(home, "POST", "/v1/ask", {"text": "租金什麼時候付？", "attachments": ["documents"]})[1]["answer"]
    assert zh["sources"][0]["paragraphs"] == [4] and zh["sources"][0]["quote"].startswith("租金每月")


def test_skill_sources_cuts_long_quotes_and_survives_odd_output():
    assert app_api.skill_sources({}) == []
    long = {"citations": [{"n": 1, "path": "a.md", "paragraphs": [1], "label": "a.md › paragraph 1"}, "junk"],
            "passages": [{"n": 1, "shown": "a.md", "paragraphs": [1], "text": "word " * 300}]}
    (s,) = app_api.skill_sources(long)
    assert len(s["quote"]) == app_api.MAX_QUOTE_CHARS and s["quote"].endswith("…") and s["file"] == "a.md"


def test_the_fake_one_tap_flow_completes_after_two_polls_and_first_run_can_then_ask(fakes):
    home = fakes("empty")
    status, started = fake_call(home, "POST", "/v1/connect/openrouter/start", {"same_device": True})
    assert status == 201 and started["mode"] == "loopback" and started["url"].startswith("https://openrouter.ai/")
    sid = started["state_id"]
    assert fake_call(home, "GET", f"/v1/connect/flows/{sid}")[1]["status"] == "pending"
    done = fake_call(home, "GET", f"/v1/connect/flows/{sid}")[1]
    assert done["status"] == "connected" and done["gained"]["name"] == "OpenRouter"
    assert [s["id"] for s in fake_call(home, "GET", "/v1/services")[1]["connected"]] == ["openrouter"]
    answer = fake_call(home, "POST", "/v1/ask", {"text": "What can you do?"})[1]["answer"]
    assert answer["status"] == "answered" and answer["route"]["name"] == "OpenRouter" and answer["route"]["cost"]["usd"] == "0.00"
    remote = fake_call(home, "POST", "/v1/connect/openrouter/start", {})[1]
    assert remote["mode"] == "paste_code"
    assert fake_call(home, "GET", f"/v1/connect/flows/{remote['state_id']}")[1]["status"] == "pending"
    assert fake_call(home, "POST", f"/v1/connect/flows/{remote['state_id']}/code", {"code": "any-code"})[1]["status"] == "connected"


def test_the_fake_chores_services_and_settings_writes(fakes):
    home = fakes("normal")
    understood = fake_call(home, "POST", "/v1/chores/understand", {"sentence": "Every Friday 18:00, summarise my feeds",
                                                                  "folders": ["feeds"], "out": "documents"})[1]["understood"]
    assert understood["ok"] is True
    status, made = fake_call(home, "POST", "/v1/chores", {"understood": understood})
    assert status == 201
    cid = made["chore"]["id"]
    status, started = fake_call(home, "POST", f"/v1/chores/{cid}/run")
    assert status == 202
    home.app.wait_chore(started["run"]["id"])
    run = fake_call(home, "GET", f"/v1/chores/{cid}/history")[1]["runs"][0]
    assert run["status"] == "waiting_card" and run["card_id"]
    card = fake_call(home, "GET", f"/v1/cards/{run['card_id']}")[1]["card"]
    assert card["title_key"] == "save_file_chore" and card["params"] == {"label": "Weekly digest"}
    assert fake_call(home, "POST", "/v1/services/groq/test")[1]["result"] == "ok"
    assert fake_call(home, "DELETE", "/v1/services/groq")[1]["removed"] is True
    assert "groq" not in {s["id"] for s in fake_call(home, "GET", "/v1/services")[1]["connected"]}
    assert fake_call(home, "PATCH", "/v1/settings", {"daily_limit_local": 60})[1]["settings"]["daily_limit"]["local"] == "60"
    assert fake_call(home, "GET", "/v1/status")[1]["money"]["daily_limit"]["local"] == "60"
    assert fake_call(home, "POST", "/v1/home/restart-proxy")[1]["restarted"] is True
    assert fake_call(home, "POST", "/v1/verify")[1]["result"] == "ok"
    assert (home.root / "fake.env").read_text().count("API_KEY") == 0  # a fake home stores no key
