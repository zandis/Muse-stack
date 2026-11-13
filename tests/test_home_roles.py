"""Device roles: every route names a scope, and a role gets exactly the routes its scopes cover.

The table EXPECTED is written out by hand, on purpose: a route added to the
server without a line here fails `test_the_table_covers_every_route`, so a new
route cannot go out without someone deciding who may call it.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time

import pytest

from home_fakes import assert_error, client_for, running, split_response
from muse import keys
from muse.home import api, auth, server

O, H, V = auth.OWNER, auth.HOST, auth.VIEWER

# (method, path template) -> the roles that may call it. Nobody else may.
EXPECTED: dict[tuple[str, str], set[str]] = {
    ("GET", "/v1/status"): {O, H, V},
    ("GET", "/v1/catalogue"): {O, H, V},
    ("POST", "/v1/connect/{provider}"): {O},
    ("POST", "/v1/keys/{provider}"): {O},
    ("GET", "/v1/jobs"): {O, H, V},
    ("POST", "/v1/jobs"): {O, H},
    ("GET", "/v1/jobs/{id}"): {O, H, V},
    ("DELETE", "/v1/jobs/{id}"): {O, H},
    ("GET", "/v1/cards"): {O, H, V},
    ("POST", "/v1/cards"): {O, H},
    ("GET", "/v1/cards/{id}"): {O, H, V},
    ("POST", "/v1/cards/{id}/answer"): {O},
    ("POST", "/v1/pause"): {O},
    ("POST", "/v1/resume"): {O},
    ("GET", "/v1/apps"): {O},
    ("POST", "/v1/apps"): {O},
    ("PATCH", "/v1/apps/{name}"): {O},
    ("DELETE", "/v1/apps/{name}"): {O},
    ("POST", "/v1/apps/{name}/rotate"): {O},
    ("GET", "/v1/receipts"): {O, V},
    ("POST", "/v1/receipts"): {O, H},
    ("GET", "/v1/mandates/artifacts"): {O},
}

PARAMS = {"provider": "groq", "id": "0123456789ab", "name": "some-name"}
KEY_CANARY = "gsk_CANARYkey0123456789abcdefABCDEF"


def device(role: str, name: str | None = None) -> auth.Device:
    return auth.Device(name or f"a-{role}", role=role)


def fill(template: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PARAMS[m.group(1)], template)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "keys.env"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "MUSE_APPS_FILE"]:
        monkeypatch.delenv(name, raising=False)
    return api.Home(proxy_running=lambda: False)


def call(home, who: auth.Device, method: str, path: str, body=None) -> tuple[int, dict]:
    raw = b"" if body is None else json.dumps(body).encode()
    resp = api.dispatch(home, who, method, path, raw)
    return resp.status, resp.payload


# ---------------------------------------------------------------- the table


def test_the_table_covers_every_route():
    assert set(EXPECTED) == set(api.route_table())


def test_every_route_declares_a_known_scope_and_the_roles_follow_from_it():
    for route in api.ROUTES:
        assert route.scope in auth.SCOPES, (route.method, route.path)
        assert set(api.route_roles(route)) == EXPECTED[(route.method, route.path)], (route.method, route.path)
    assert {r.scope for r in api.ROUTES} <= set(auth.SCOPES)
    assert auth.ROLE_SCOPES[O] == frozenset(auth.SCOPES)
    assert auth.ROLE_SCOPES[H] < auth.ROLE_SCOPES[O] and auth.ROLE_SCOPES[V] < auth.ROLE_SCOPES[O]


@pytest.mark.parametrize("role", auth.ROLES)
@pytest.mark.parametrize("route", api.ROUTES, ids=lambda r: f"{r.method} {r.path}")
def test_every_route_for_every_role(home, route, role):
    status, payload = call(home, device(role), route.method, fill(route.path))
    if role in EXPECTED[(route.method, route.path)]:
        # Allowed: whatever the handler then says (bad input, no such thing, done), it is not the role check.
        assert not (status == 403 and "not allowed to make this call" in payload["error"]["message"]), payload
    else:
        err = assert_error(status, payload, 403, "forbidden")
        assert "not allowed to make this call" in err["message"]


def test_a_route_without_a_scope_is_refused_for_everyone(home, monkeypatch):
    bare = api.Route("GET", "/v1/surprise", lambda h, r: (200, {"reached": True}))
    wrong = api.Route("GET", "/v1/typo", lambda h, r: (200, {"reached": True}), "status.raed")
    monkeypatch.setattr(api, "ROUTES", api.ROUTES + (bare, wrong))
    for role in auth.ROLES:
        for path in ("/v1/surprise", "/v1/typo"):
            assert_error(*call(home, device(role), "GET", path), 403, "forbidden")
    assert api.route_roles(bare) == [] and api.route_roles(wrong) == []


def test_a_role_nobody_knows_gets_nothing(home):
    for route in api.ROUTES:
        assert_error(*call(home, auth.Device("x", role="admin"), route.method, fill(route.path)), 403, "forbidden")
    assert not auth.allows("admin", "status.read") and not auth.allows(O, "") and not auth.allows(O, "nonsense")


def test_the_role_is_checked_before_the_body_is_read_or_the_home_is_asked(home, monkeypatch):
    monkeypatch.setattr(home, "check_available", lambda: (_ for _ in ()).throw(AssertionError("asked the home")))
    resp = api.dispatch(home, device(H), "POST", "/v1/resume", b"{not json")
    assert resp.status == 403 and resp.route == "/v1/resume"
    # A route that does not exist is still 404, and a wrong method 405: the route table is no secret.
    assert call(home, device(V), "GET", "/v1/nope")[0] == 404
    assert call(home, device(V), "PUT", "/v1/status")[0] == 405


# ---------------------------------------------------------------- what a host token must not do


def test_a_host_cannot_resume_or_pause(home):
    owner, host = device(O), device(H, "bot-tools")
    assert call(home, owner, "POST", "/v1/pause")[0] == 200
    assert_error(*call(home, host, "POST", "/v1/resume"), 403, "forbidden")
    assert home.is_paused() is True
    assert call(home, owner, "POST", "/v1/resume")[0] == 200
    assert_error(*call(home, host, "POST", "/v1/pause"), 403, "forbidden")
    assert home.is_paused() is False
    assert call(home, host, "GET", "/v1/status")[1]["paused"] is False  # it may still see the switch


def test_a_host_cannot_store_a_key_or_start_a_connection(home, tmp_path):
    host = device(H, "bot-tools")
    status, payload = call(home, host, "POST", "/v1/keys/groq", {"key": KEY_CANARY})
    assert_error(status, payload, 403, "forbidden")
    assert KEY_CANARY not in json.dumps(payload)
    assert not (tmp_path / "keys.env").exists() and not home.key_configured("GROQ_API_KEY")
    assert_error(*call(home, host, "POST", "/v1/connect/groq"), 403, "forbidden")


def test_a_host_cannot_see_or_manage_apps(home):
    from muse import apps

    owner, host = device(O), device(H, "bot-tools")
    assert call(home, owner, "POST", "/v1/apps", {"name": "editor", "priority": "interactive"})[0] == 201
    before = apps.apps_path().read_text()
    for method, path, body in (("GET", "/v1/apps", None), ("POST", "/v1/apps", {"name": "mine", "priority": "interactive", "allow_paid": True}),
                               ("PATCH", "/v1/apps/editor", {"allow_paid": True}), ("DELETE", "/v1/apps/editor", None),
                               ("POST", "/v1/apps/editor/rotate", None)):
        status, payload = call(home, host, method, path, body)
        assert_error(status, payload, 403, "forbidden")
        assert "token" not in payload
    assert apps.apps_path().read_text() == before


def test_a_host_cannot_answer_a_card_not_even_someone_elses(home):
    host = device(H, "bot-tools")
    own = call(home, host, "POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "a.md"})[1]["card"]["id"]
    other = home.cards.create(kind="save_file", title="Save a file", thing="b.md")
    for card_id in (own, other):
        assert_error(*call(home, host, "POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"}), 403, "forbidden")
        assert home.cards.get(card_id)["status"] == "pending"
    # The person answers from their own device.
    assert call(home, device(O), "POST", f"/v1/cards/{own}/answer", {"decision": "approve"})[1]["card"]["status"] == "approved"


def test_a_host_sees_only_the_cards_it_asked_for(home):
    host, other_host, owner, viewer = device(H, "bot-tools"), device(H, "other-tools"), device(O), device(V)
    mine = call(home, host, "POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "mine.md"})[1]["card"]["id"]
    theirs = call(home, other_host, "POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "theirs.md"})[1]["card"]["id"]
    homes = home.cards.create(kind="spend", title="Pay", thing="NT$30", cost={"amount": "30", "currency": "TWD"},
                              extra={"payee": "ACME", "limit_left": {"amount": "70", "currency": "TWD"}})
    assert call(home, host, "GET", f"/v1/cards/{mine}")[1]["card"]["thing"] == "mine.md"
    for card_id in (theirs, homes):
        status, payload = call(home, host, "GET", f"/v1/cards/{card_id}")
        assert_error(status, payload, 404, "not_found")
        assert payload == call(home, host, "GET", "/v1/cards/0123456789abcdef")[1]  # the same as a card that never was
    listed = call(home, host, "GET", "/v1/cards")[1]
    assert [c["id"] for c in listed["cards"]] == [mine]
    assert listed["counts"] == {"pending": 1, "approved": 0, "declined": 0, "expired": 0}
    assert "theirs.md" not in json.dumps(listed) and "NT$30" not in json.dumps(listed)
    assert call(home, host, "GET", "/v1/cards?status=approved")[1]["cards"] == []
    for everyone in (owner, viewer):
        seen = call(home, everyone, "GET", "/v1/cards")[1]
        assert {c["id"] for c in seen["cards"]} == {mine, theirs, homes} and seen["counts"]["pending"] == 3
        assert call(home, everyone, "GET", f"/v1/cards/{theirs}")[0] == 200


def test_a_host_sees_and_cancels_only_its_own_jobs(home):
    host, other_host, owner, viewer = device(H, "bot-tools"), device(H, "other-tools"), device(O), device(V)
    mine = call(home, host, "POST", "/v1/jobs", {"prompt": "tag my notes", "alias": "cheap"})[1]["job"]["id"]
    theirs = call(home, other_host, "POST", "/v1/jobs", {"prompt": "something else", "alias": "cheap"})[1]["job"]["id"]
    persons = call(home, owner, "POST", "/v1/jobs", {"prompt": "the person's own", "alias": "cheap"})[1]["job"]["id"]
    by_hand = home.queue().add(kind="prompt", payload="queued with muse queue add", alias="cheap").id
    assert [j["id"] for j in call(home, host, "GET", "/v1/jobs")[1]["jobs"]] == [mine]
    assert call(home, host, "GET", f"/v1/jobs/{mine}")[1]["job"]["status"] == "queued"
    for job_id in (theirs, persons, by_hand):
        for method in ("GET", "DELETE"):
            status, payload = call(home, host, method, f"/v1/jobs/{job_id}")
            assert_error(status, payload, 404, "not_found")
            assert payload == call(home, host, method, "/v1/jobs/ffffffffffff")[1]
        assert home.queue().get(job_id).status == "queued"
    assert call(home, host, "DELETE", f"/v1/jobs/{mine}")[1]["job"]["status"] == "cancelled"
    for everyone in (owner, viewer):
        assert {j["id"] for j in call(home, everyone, "GET", "/v1/jobs")[1]["jobs"]} == {mine, theirs, persons, by_hand}
        assert call(home, everyone, "GET", f"/v1/jobs/{by_hand}")[0] == 200
    assert_error(*call(home, viewer, "DELETE", f"/v1/jobs/{theirs}"), 403, "forbidden")
    assert call(home, owner, "DELETE", f"/v1/jobs/{theirs}")[1]["job"]["status"] == "cancelled"


def test_a_viewer_changes_nothing(home):
    viewer = device(V)
    for route in api.ROUTES:
        if route.method != "GET":
            assert_error(*call(home, viewer, route.method, fill(route.path)), 403, "forbidden")
    assert home.is_paused() is False and home.cards.counts()["pending"] == 0 and home.queue().list() == []


# ---------------------------------------------------------------- the token store keeps the role


def test_a_role_is_stored_listed_and_kept_through_a_rotation():
    host, token = auth.add_token("bot-tools", role="host")
    viewer, _ = auth.add_token("wall-screen", role="viewer")
    plain, _ = auth.add_token("mei-phone")
    assert (host.role, viewer.role, plain.role) == ("host", "viewer", "owner")
    data = json.loads(auth.tokens_path().read_text())
    assert {d["name"]: d["role"] for d in data["devices"]} == {"bot-tools": "host", "wall-screen": "viewer", "mei-phone": "owner"}
    assert {d.name: d.role for d in auth.list_devices()} == {"bot-tools": "host", "wall-screen": "viewer", "mei-phone": "owner"}
    v = auth.TokenFileVerifier()
    assert v.verify(token).role == "host"
    rotated, new = auth.rotate_token("bot-tools")
    assert rotated.role == "host" and v.verify(new).role == "host" and v.verify(token) is None
    with pytest.raises(auth.TokenError, match="role must be one of owner, host, viewer"):
        auth.add_token("x", role="admin")
    assert "x" not in {d.name for d in auth.list_devices()}


def test_a_token_file_from_before_roles_means_owner_and_stays_readable():
    _, token = auth.add_token("old-phone")
    path = auth.tokens_path()
    data = json.loads(path.read_text())
    for entry in data["devices"]:
        del entry["role"]  # the file as an earlier version wrote it
    path.write_text(json.dumps(data))
    found = auth.TokenFileVerifier().verify(token)
    assert found.role == "owner" and found.can("pause.write")
    auth.add_token("new-host", role="host")  # a change rewrites the file and keeps the old device as it was
    after = {d["name"]: d["role"] for d in json.loads(path.read_text())["devices"]}
    assert after == {"old-phone": "owner", "new-host": "host"}
    assert auth.TokenFileVerifier().verify(token).role == "owner"


def test_a_role_this_version_does_not_know_cannot_sign_in():
    _, token = auth.add_token("odd")
    path = auth.tokens_path()
    for bad in ("superuser", "", None, 7, ["owner"]):
        data = json.loads(path.read_text())
        data["devices"][0]["role"] = bad
        path.write_text(json.dumps(data))
        assert auth.TokenFileVerifier().verify(token) is None, bad
        assert auth.list_devices() == []


def test_the_static_verifier_takes_roles():
    v = auth.StaticVerifier({"phone": "tok-a", "bot": "tok-b"}, roles={"bot": "host"})
    assert v.verify("tok-a").role == "owner" and v.verify("tok-b").role == "host"
    with pytest.raises(auth.TokenError):
        auth.StaticVerifier({"x": "t"}, roles={"x": "root"})


# ---------------------------------------------------------------- through a real server, with real token files


def test_a_host_token_made_by_hosts_setup_is_refused_over_the_wire(home, tmp_path, monkeypatch):
    from muse import presets
    from muse.mcp import hosts

    done = hosts.setup(presets.get_host("openmausbot"))
    host_token = done.token_file.read_text().strip()
    assert {d.name: d.role for d in auth.list_devices()} == {done.device: "host"}
    _, owner_token = auth.add_token("mei-phone")
    with running(home, auth.TokenFileVerifier()) as srv:
        bot, phone = client_for(srv, host_token), client_for(srv, owner_token)
        assert phone.call("POST", "/v1/pause")[0] == 200
        assert_error(*bot.call("POST", "/v1/resume"), 403, "forbidden")
        assert_error(*bot.call("POST", "/v1/keys/groq", {"key": KEY_CANARY}), 403, "forbidden")
        assert_error(*bot.call("POST", "/v1/apps", {"name": "mine", "priority": "interactive"}), 403, "forbidden")
        assert_error(*bot.call("GET", "/v1/apps"), 403, "forbidden")
        assert bot.call("GET", "/v1/status")[1]["paused"] is True
        asked = bot.call("POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "a.md"})[1]["card"]
        assert asked["job_ref"] == f"device:{done.device}"
        assert_error(*bot.call("POST", f"/v1/cards/{asked['id']}/answer", {"decision": "approve"}), 403, "forbidden")
        assert phone.call("POST", f"/v1/cards/{asked['id']}/answer", {"decision": "approve"})[0] == 200
        assert bot.call("GET", f"/v1/cards/{asked['id']}")[1]["card"]["status"] == "approved"
    assert home.is_paused() is True


# ---------------------------------------------------------------- abuse limits


def test_the_failure_limiter_counts_per_peer_in_a_sliding_window():
    now = [1000.0]
    lim = server.FailureLimiter(limit=3, window_s=60, clock=lambda: now[0])
    assert [lim.fail("a") for _ in range(3)] == [None, None, None]
    assert lim.fail("a") == 60 and lim.fail("b") is None  # another peer is not affected
    now[0] += 20
    assert lim.fail("a") == 40  # a refused attempt does not push the wait out
    now[0] += 39.5
    assert lim.fail("a") == 1
    now[0] += 1
    assert lim.fail("a") is None  # the window has passed


def test_the_failure_limiter_forgets_the_peer_seen_longest_ago():
    lim = server.FailureLimiter(limit=1, window_s=60, clock=lambda: 0.0, max_peers=2)
    assert lim.fail("a") is None and lim.fail("b") is None and lim.fail("c") is None
    assert lim.fail("b") == 60 and lim.fail("c") == 60
    assert lim.fail("a") is None  # forgotten, so it starts again; memory stays bounded
    assert len(lim._fails) == 2


@pytest.mark.parametrize("tcp", [False, True])
def test_wrong_tokens_are_rate_limited_and_a_right_one_is_still_served(home, tcp):
    with running(home, tcp=tcp) as srv:
        stranger, known = client_for(srv, "wrong-token"), client_for(srv)
        for _ in range(server.AUTH_FAIL_LIMIT):
            assert_error(*stranger.call("GET", "/v1/status"), 401, "unauthorized")
        status, payload, headers = stranger.request("GET", "/v1/status")
        err = assert_error(status, payload, 429, "too_many_requests")
        assert 1 <= err["retry_after"] <= server.AUTH_FAIL_WINDOW_S and headers["retry-after"] == str(err["retry_after"])
        assert client_for(srv, None).call("POST", "/v1/resume")[0] == 429  # no token at all counts the same
        assert known.call("GET", "/v1/status")[0] == 200  # the person's own app is not locked out
        srv.auth_failures.clock = lambda: 10**9  # the window passes
        assert_error(*stranger.call("GET", "/v1/status"), 401, "unauthorized")


def test_connections_beyond_the_cap_get_503_at_once_and_the_slot_comes_back(home):
    with running(home) as srv:
        srv._slots = threading.BoundedSemaphore(1)

        def connect() -> socket.socket:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(str(srv.socket_path))
            return sock

        held = connect()
        held.sendall(b"GET /v1/status HTTP/1.1\r\nHost: localhost\r\n")  # an unfinished request holds the one slot
        try:
            deadline, status = time.monotonic() + 5, None
            while time.monotonic() < deadline and status != 503:
                extra = connect()  # refused without being read: the reply comes although nothing was sent
                try:
                    raw = b""
                    while chunk := extra.recv(65536):
                        raw += chunk
                except OSError:
                    continue
                finally:
                    extra.close()
                if raw:
                    status, headers, body = split_response(raw)
            assert status == 503 and headers["retry-after"] == "1" and headers["connection"] == "close"
            assert_error(status, json.loads(body), 503, "unavailable")
        finally:
            held.close()
        deadline, got = time.monotonic() + 5, None
        while time.monotonic() < deadline and got != 200:  # the slot comes back once the held connection is seen closed
            try:
                got = client_for(srv).call("GET", "/v1/status")[0]
            except OSError:
                got = None
            time.sleep(0.02)
        assert got == 200
    assert server.MAX_CONNECTIONS == 64
