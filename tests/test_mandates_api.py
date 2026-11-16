"""Permissions through the home API (muse/home/mandates_api.py): list, draft, issue, request, stop, declined.

Signing needs biscuit-python; without it every route but the role check answers 503 `unavailable`, which runs
everywhere. The rest skips without the library.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import logging
import os
from pathlib import Path

import pytest

from home_fakes import assert_error
from wardmuse import apps, keys
from wardmuse import permissions as P
from wardmuse.home import api, auth

needs_library = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")

OWNER = auth.Device("mei-phone", role="owner")
HOST = auth.Device("bot-tools", role="host")
OTHER_HOST = auth.Device("other-tools", role="host")
VIEWER = auth.Device("wall-screen", role="viewer")


class Clock:
    def __init__(self) -> None:
        self.now = float(1_794_528_000)  # 2026-11-13T00:00:00Z

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "keys.env"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "MUSE_APPS_FILE", "MUSE_PERMISSIONS"]:
        monkeypatch.delenv(name, raising=False)
    return api.Home(proxy_running=lambda: False, clock=Clock())


def call(home, who, method, path, body=None):
    resp = api.dispatch(home, who, method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


def ok(home, who, method, path, body=None, status=200):
    got, payload = call(home, who, method, path, body)
    assert got == status, (got, payload)
    return payload


HELPER_FIELDS = {"holder": "helper:home", "actions": ["files.read", "llm.call"],
                 "resources": [{"kind": "folder", "value": "/data/notes"}, {"kind": "provider", "value": "router"}],
                 "days": 7, "privacy": "private"}


def host_fields(device=HOST.name, **more):
    return {"holder": f"device:{device}", "actions": ["llm.call"], "resources": [{"kind": "provider", "value": "router"}],
            "limits": [{"kind": "requests", "period": "day", "amount": 200}], "days": 30, **more}


def test_without_the_library_the_routes_say_so(home, monkeypatch):
    monkeypatch.setattr(P, "available", lambda: False)
    assert_error(*call(home, OWNER, "GET", "/v1/mandates"), 503, "unavailable")
    assert_error(*call(home, OWNER, "POST", "/v1/mandates/draft", HELPER_FIELDS), 503, "unavailable")
    # The tools server's check falls back to "no permission" rather than an error.
    assert ok(home, HOST, "POST", "/v1/mandates/check", {"action": "read"})["has_mandate"] is False


def test_a_host_cannot_stop_and_a_viewer_can_only_read(home):
    assert_error(*call(home, HOST, "POST", "/v1/mandates/m1_00/stop"), 403, "forbidden")
    for method, path in (("POST", "/v1/mandates"), ("POST", "/v1/mandates/draft"), ("POST", "/v1/mandates/m1_00/stop"),
                         ("GET", "/v1/mandates/declined")):
        assert_error(*call(home, VIEWER, method, path, {} if method == "POST" else None), 403, "forbidden")
    assert_error(*call(home, HOST, "GET", "/v1/mandates"), 403, "forbidden")


@needs_library
def test_draft_shows_the_sentence_and_plan_and_issues_nothing(home):
    got = ok(home, OWNER, "POST", "/v1/mandates/draft", HELPER_FIELDS, status=201)["draft"]
    assert got["issued"] is False and got["holder"] == "helper:home" and "helper" in got["sentence"]
    assert any("/data/notes" in line or "notes" in line for line in [got["sentence"], *got["lines"]])
    assert {t["target"] for t in got["targets"]} == {"skills", "records"}
    assert ok(home, VIEWER, "GET", "/v1/mandates")["mandates"] == []  # a draft is not a permission
    zh = ok(home, OWNER, "POST", "/v1/mandates/draft", {**HELPER_FIELDS, "lang": "zh-TW"}, status=201)["draft"]
    assert zh["sentence"] != got["sentence"]
    assert_error(*call(home, OWNER, "POST", "/v1/mandates/draft", {**HELPER_FIELDS, "actions": ["files.read"], "confirm": "never",
                                                                    "days": 0}), 400, "invalid_request")
    assert_error(*call(home, OWNER, "POST", "/v1/mandates/draft", {**HELPER_FIELDS, "privacy": 'a"b'}), 400, "invalid_request")


@needs_library
def test_the_owner_issues_lists_and_stops_with_records(home):
    draft = ok(home, OWNER, "POST", "/v1/mandates/draft", host_fields("reader-app", holder="app:reader"), status=201)["draft"]
    issued = ok(home, OWNER, "POST", "/v1/mandates", {"draft": draft["id"]}, status=201)
    m = issued["mandate"]
    assert m["holder"] == "app:reader" and m["state"] == "active" and issued["recorded"] is True
    # The plain facts, so an app can write the sentence in its own words.
    assert m["actions"] == ["llm.call"] and m["resources"] == [{"kind": "provider", "value": "router"}]
    assert m["limits"] == [{"kind": "requests", "unit": "", "decimals": 0, "period": "day", "amount": 200}]
    assert m["privacy"] == "private"
    assert {t: s["state"] for t, s in m["targets"].items()} == {"router": "active", "records": "active"}
    token = issued["token"]
    assert issued["token_shown_once"] is True and token.startswith("sk-muse-app-")
    app = apps.load_apps()[issued["app"]]
    assert app.mandate == m["id"] and app.daily_requests == 200 and app.allow_paid is False and app.default_privacy == "private"
    # The draft is used up; the list shows the permission to a viewer, with no token anywhere.
    assert_error(*call(home, OWNER, "POST", "/v1/mandates", {"draft": draft["id"]}), 404, "not_found")
    listed = ok(home, VIEWER, "GET", "/v1/mandates")
    assert [x["id"] for x in listed["mandates"]] == [m["id"]] and token not in json.dumps(listed)
    assert listed["enforcement"]["skills"] == "advise" and listed["enforcement"]["not_built"] == ["payments", "connectors", "health"]
    assert ok(home, VIEWER, "GET", "/v1/mandates/artifacts") == listed
    stopped = ok(home, OWNER, "POST", f"/v1/mandates/{m['name']}/stop")
    assert stopped["stopped"] and stopped["complete"] and stopped["recorded"] and stopped["was"] == "active"
    assert {t: s["state"] for t, s in stopped["targets"].items()} == {"router": "revoked", "records": "revoked"}
    assert issued["app"] not in apps.load_apps()
    after = ok(home, VIEWER, "GET", "/v1/mandates")["mandates"][0]
    assert after["state"] == "stopped"
    actions = [r["action"] for r in home.records().records()]
    assert actions.count("mandate.issue") == 1 and actions.count("mandate.revoke") == 1
    # Stopping again is harmless.
    assert ok(home, OWNER, "POST", f"/v1/mandates/{m['id'][3:]}/stop")["was"] == "stopped"  # the id without m1_ also names it
    assert_error(*call(home, OWNER, "POST", "/v1/mandates/m1_ffff/stop"), 404, "not_found")


@needs_library
def test_a_host_only_asks_and_the_owner_approving_the_card_issues_it(home):
    assert_error(*call(home, HOST, "POST", "/v1/mandates/draft", host_fields(OTHER_HOST.name)), 403, "forbidden")
    assert_error(*call(home, HOST, "POST", "/v1/mandates/draft", HELPER_FIELDS), 403, "forbidden")
    draft = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    asked = ok(home, HOST, "POST", "/v1/mandates", {"draft": draft["id"]}, status=202)
    assert asked["requested"] is True and asked["issued"] is False
    card = asked["card"]
    assert card["kind"] == "grant_permission" and card["extra"]["sentence"] == draft["sentence"]
    assert ok(home, OWNER, "GET", "/v1/mandates")["mandates"] == []  # nothing issued yet
    # Asking again for the same thing while it waits is refused; a host cannot answer its own card.
    draft2 = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    assert_error(*call(home, HOST, "POST", "/v1/mandates", {"draft": draft2["id"]}), 409, "conflict")
    assert_error(*call(home, HOST, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "approve"}), 403, "forbidden")
    reply = ok(home, OWNER, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "approve"})
    settled = reply["write"]
    assert settled["state"] == "issued" and settled["mandate"]["holder"] == f"device:{HOST.name}"
    assert settled["token"].startswith("sk-muse-app-")  # the new app key, to the person who approved, once
    listed = ok(home, OWNER, "GET", "/v1/mandates")["mandates"]
    assert [x["holder"] for x in listed] == [f"device:{HOST.name}"]
    # The host's own permission now answers the tools server's check.
    got = ok(home, HOST, "POST", "/v1/mandates/check", {"action": "write", "resource": "/etc/hosts"})
    assert got["has_mandate"] is True and got["decision"] == "ask"
    assert ok(home, OTHER_HOST, "POST", "/v1/mandates/check", {"action": "read"})["has_mandate"] is False
    # A host still cannot stop it, not even its own.
    assert_error(*call(home, HOST, "POST", f"/v1/mandates/{listed[0]['name']}/stop"), 403, "forbidden")
    ok(home, OWNER, "POST", f"/v1/mandates/{listed[0]['name']}/stop")
    assert ok(home, HOST, "POST", "/v1/mandates/check", {"action": "read", "resource": "/x"})["decision"] == "deny"


@needs_library
def test_a_declined_request_is_listed_for_a_week_and_not_asked_again(home):
    draft = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    card = ok(home, HOST, "POST", "/v1/mandates", {"draft": draft["id"]}, status=202)["card"]
    reply = ok(home, OWNER, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "decline"})
    assert reply["write"]["state"] == "declined"
    declined = ok(home, HOST, "GET", "/v1/mandates/declined")
    assert declined["window_days"] == 7 and [d["card_id"] for d in declined["declined"]] == [card["id"]]
    assert declined["declined"][0]["holder"] == f"device:{HOST.name}"
    assert declined["declined"][0]["sentence"] == draft["sentence"]  # what the person was shown
    zh = ok(home, HOST, "GET", "/v1/mandates/declined?lang=zh-TW")["declined"][0]["sentence"]
    assert zh != draft["sentence"] and HOST.name in zh
    assert_error(*call(home, HOST, "GET", "/v1/mandates/declined?lang=fr"), 400, "invalid_request")
    assert ok(home, OTHER_HOST, "GET", "/v1/mandates/declined")["declined"] == []  # a host sees only its own
    assert len(ok(home, OWNER, "GET", "/v1/mandates/declined")["declined"]) == 1
    again = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(days=10), status=201)["draft"]  # other dates, same ask
    err = assert_error(*call(home, HOST, "POST", "/v1/mandates", {"draft": again["id"]}), 409, "conflict")
    assert "declined" in err["message"]
    different = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(actions=["skills.run"],
                   resources=[{"kind": "home", "value": "skills"}], limits=[]), status=201)["draft"]
    assert ok(home, HOST, "POST", "/v1/mandates", {"draft": different["id"]}, status=202)["requested"]
    home.clock.now += 7 * 86400 + 60  # a week later the same ask may be made again
    later = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    assert ok(home, HOST, "POST", "/v1/mandates", {"draft": later["id"]}, status=202)["requested"]
    assert ok(home, HOST, "GET", "/v1/mandates/declined")["declined"] == []


@needs_library
def test_an_expired_request_card_is_settled_as_expired(home):
    draft = ok(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    card = ok(home, HOST, "POST", "/v1/mandates", {"draft": draft["id"]}, status=202)["card"]
    home.clock.now += 2 * 86400
    assert ok(home, OWNER, "GET", "/v1/mandates")["mandates"] == []
    from wardmuse.compiler.table import Table

    assert Table(home.environ, home.clock).request(card["id"])["state"] == "expired"


@needs_library
def test_person_confirmed_is_the_one_place_the_owner_check_lives(home, monkeypatch):
    from wardmuse.home import mandates_api

    draft = ok(home, OWNER, "POST", "/v1/mandates/draft", HELPER_FIELDS, status=201)["draft"]
    monkeypatch.setattr(mandates_api, "person_confirmed", lambda home, req: False)
    # Without the person's confirmation even the owner's device only asks, and cannot stop.
    assert ok(home, OWNER, "POST", "/v1/mandates", {"draft": draft["id"]}, status=202)["requested"]
    assert_error(*call(home, OWNER, "POST", "/v1/mandates/m1_00/stop"), 403, "forbidden")


@needs_library
def test_no_token_key_or_root_key_in_any_reply_record_or_log(home, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    replies = []

    def keep(*a, **kw):
        got = ok(*a, **kw)
        replies.append(json.dumps(got))
        return got

    draft = keep(home, OWNER, "POST", "/v1/mandates/draft", host_fields("reader", holder="app:reader"), status=201)["draft"]
    issued = keep(home, OWNER, "POST", "/v1/mandates", {"draft": draft["id"]}, status=201)
    keep(home, OWNER, "GET", "/v1/mandates")
    keep(home, OWNER, "POST", f"/v1/mandates/{issued['mandate']['name']}/stop")
    hdraft = keep(home, HOST, "POST", "/v1/mandates/draft", host_fields(), status=201)["draft"]
    card = keep(home, HOST, "POST", "/v1/mandates", {"draft": hdraft["id"]}, status=202)["card"]
    keep(home, OWNER, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "approve"})
    keep(home, HOST, "POST", "/v1/mandates/check", {"action": "read", "resource": "/x"})
    from wardmuse.compiler.core import Compiler
    from wardmuse.permissions.store import FileKeySource

    c = Compiler(home.environ)
    root_private = FileKeySource(environ=home.environ).path.read_text().strip()
    secrets = [root_private, root_private.split("/", 1)[1]] + [c.store.read(n) for n in c.store.names()]
    app_tokens = []
    for r in replies:
        got = json.loads(r)
        for part in (got, got.get("write") or {}):
            if "token" in part:
                app_tokens.append(part["token"])
    assert len(app_tokens) == 2
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    record_text = json.dumps(home.records().records())
    table_bytes = (Path(envget(os.environ, "MUSE_HOME")) / "compiler.sqlite").read_bytes()
    everything = "".join(replies)
    for s in secrets:
        assert s not in everything and s not in logs and s not in record_text and s.encode() not in table_bytes
    for t in app_tokens:
        assert everything.count(t) == 1 and t not in logs and t not in record_text and t.encode() not in table_bytes
