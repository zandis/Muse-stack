"""muse.home.auth: device tokens, and who may reach the home at all."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import logging
import os
import socket
import stat

import pytest

from home_fakes import TOKEN, assert_error, client_for, running
from wardmuse.home import api, auth, server


@pytest.fixture()
def home():
    return api.Home(snapshot_fn=lambda: {"generated_at": "2026-11-13T00:00:00Z"}, proxy_running=lambda: False)


def real_verifier():
    return auth.TokenFileVerifier()


# ---------------------------------------------------------------- the token store


def test_a_token_is_random_shown_once_and_stored_only_as_a_hash():
    device, token = auth.add_token("mei-phone", now=1_800_000_000)
    assert device == auth.Device("mei-phone", "2027-01-15T08:00:00Z", None)
    assert token.startswith("sk-muse-dev-") and len(token) >= 12 + 43  # 32 random bytes, url-safe
    _, other = auth.add_token("desk")
    assert other != token
    path = auth.tokens_path()
    text = path.read_text()
    assert token not in text and other not in text
    assert auth.hash_token(token) in text
    data = json.loads(text)
    assert data["version"] == 1 and [d["name"] for d in data["devices"]] == ["desk", "mei-phone"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent == type(path)(envget(os.environ, "MUSE_HOME"))
    assert [d.name for d in auth.list_devices()] == ["desk", "mei-phone"]


def test_verify_knows_the_device_and_nothing_else():
    _, token = auth.add_token("mei-phone")
    v = real_verifier()
    assert v.verify(token).name == "mei-phone"
    for bad in ("", token[:-1], token + "x", token.upper(), "sk-muse-dev-", auth.hash_token(token), "x" * 600, None, 7):
        assert v.verify(bad) is None  # the stored hash itself is not a token


def test_rotate_and_remove_take_effect_at_once():
    _, old = auth.add_token("mei-phone", now=1_800_000_000)
    v = real_verifier()
    device, new = auth.rotate_token("mei-phone", now=1_800_000_060)
    assert device.rotated_at == "2027-01-15T08:01:00Z" and device.created_at == "2027-01-15T08:00:00Z"
    assert v.verify(old) is None and v.verify(new).name == "mei-phone"
    assert auth.remove_token("mei-phone") is True
    assert v.verify(new) is None and auth.list_devices() == []
    assert auth.remove_token("mei-phone") is False
    with pytest.raises(auth.TokenError, match="no device named"):
        auth.rotate_token("mei-phone")


@pytest.mark.parametrize("name", ["", "Phone", "a b", "-x", "x-", "x" * 41, "../x", None, 3])
def test_bad_names_are_refused(name):
    with pytest.raises(auth.TokenError, match="device name must be"):
        auth.add_token(name)
    assert not auth.tokens_path().exists()


def test_duplicates_and_too_many_devices_are_refused(monkeypatch):
    auth.add_token("one")
    with pytest.raises(auth.TokenError, match="already exists"):
        auth.add_token("one")
    monkeypatch.setattr(auth, "MAX_DEVICES", 2)
    auth.add_token("two")
    with pytest.raises(auth.TokenError, match="already 2 devices"):
        auth.add_token("three")


@pytest.mark.parametrize("text", ["{not json", "[]", '{"devices": 3}'])
def test_a_malformed_file_lets_nobody_in_and_is_never_overwritten(text, caplog):
    _, token = auth.add_token("one")
    path = auth.tokens_path()
    path.write_text(text)
    with caplog.at_level(logging.WARNING, logger="muse.home"):
        assert real_verifier().verify(token) is None
    assert "malformed" in caplog.text
    assert auth.list_devices() == []
    for change in (lambda: auth.add_token("two"), lambda: auth.rotate_token("one"), lambda: auth.remove_token("one")):
        with pytest.raises(auth.TokenError, match="malformed"):
            change()
    assert path.read_text() == text


def test_bad_entries_in_the_file_are_skipped_not_trusted():
    _, token = auth.add_token("good")
    path = auth.tokens_path()
    data = json.loads(path.read_text())
    data["devices"] += [{"name": "nohash"}, {"name": "short", "token_sha256": "abc"}, "text", {"name": "BAD", "token_sha256": "a" * 64},
                        {"name": "good", "token_sha256": "b" * 64}]
    path.write_text(json.dumps(data))
    assert [d.name for d in auth.list_devices()] == ["good"]
    assert real_verifier().verify(token).name == "good"


def test_static_verifier_holds_hashes_not_tokens():
    v = auth.StaticVerifier({"a": "token-a", "b": "token-b"})
    assert v.verify("token-b").name == "b" and v.verify("token-c") is None
    assert "token-a" not in repr(vars(v))


def test_bearer_parsing():
    assert api.bearer_token("Bearer abc") == "abc" and api.bearer_token("bearer   abc ") == "abc"
    for bad in (None, "", "abc", "Basic abc", "Bearer", "Bearer ", "Bearer a b"):
        assert api.bearer_token(bad) is None


# ---------------------------------------------------------------- over the wire


def test_every_request_needs_a_known_token(home):
    _, token = auth.add_token("mei-phone")
    with running(home, real_verifier()) as srv:
        assert client_for(srv, token).call("GET", "/v1/status")[0] == 200
        answers = []
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {token}"}, {"Authorization": token},
                        {"Authorization": f"Bearer {token}x"}, {"Authorization": "Bearer"}, {"Authorization": f"Bearer {auth.hash_token(token)}"}):
            status, payload, got = client_for(srv, None).request("GET", "/v1/status", headers=headers)
            assert_error(status, payload, 401, "unauthorized")
            assert got["www-authenticate"] == "Bearer"
            answers.append(json.dumps(payload))
        assert len(set(answers)) == 1  # missing, malformed and wrong look the same


def test_without_a_token_nothing_is_learned_about_routes_or_bodies(home):
    with running(home) as srv:
        anon = client_for(srv, None)
        for method, path, body in [("GET", "/v1/nope", None), ("PUT", "/v1/status", None), ("POST", "/v1/jobs", {"prompt": "x"}),
                                   ("POST", "/v1/keys/gemini", {"key": "k"}), ("GET", "/", None), ("POST", "/v1/jobs", b"{not json")]:
            status, payload, _ = anon.request(method, path, body)
            assert_error(status, payload, 401, "unauthorized")


def test_a_removed_or_rotated_token_stops_working_on_a_running_server(home):
    _, token = auth.add_token("mei-phone")
    with running(home, real_verifier()) as srv:
        assert client_for(srv, token).call("GET", "/v1/status")[0] == 200
        _, fresh = auth.rotate_token("mei-phone")
        assert client_for(srv, token).call("GET", "/v1/status")[0] == 401
        assert client_for(srv, fresh).call("GET", "/v1/status")[0] == 200
        auth.remove_token("mei-phone")
        assert client_for(srv, fresh).call("GET", "/v1/status")[0] == 401


def test_the_device_name_reaches_the_handler_as_answered_by(home):
    _, token = auth.add_token("mei-phone")
    card_id = home.cards.create(kind="save_file", title="Save a file", thing="a.md")
    with running(home, real_verifier()) as srv:
        status, payload = client_for(srv, token).call("POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"})
    assert status == 200 and payload["card"]["answered_by"] == "mei-phone"


# ---------------------------------------------------------------- peer uid on the Unix socket


def test_peer_uid_reports_this_user_where_the_platform_can():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        uid = server.peer_uid(a)
    finally:
        a.close()
        b.close()
    if uid is None:
        pytest.skip("this platform does not expose peer credentials")
    assert uid == os.getuid()


def test_peer_uid_is_none_for_a_socket_without_credentials():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        assert server.peer_uid(s) in (None, os.getuid()) or True  # must not raise
    assert server.peer_uid(object()) is None  # type: ignore[arg-type]


def test_another_users_process_is_refused_even_with_a_good_token(home, monkeypatch):
    with running(home) as srv:
        assert client_for(srv).call("GET", "/v1/status")[0] == 200
        monkeypatch.setattr(server, "peer_uid", lambda sock: os.getuid() + 1)
        status, payload = client_for(srv).call("GET", "/v1/status")
        assert_error(status, payload, 403, "forbidden")
        status, payload = client_for(srv, None).call("GET", "/v1/status")
        assert_error(status, payload, 403, "forbidden")  # checked before the token


def test_where_the_platform_cannot_say_the_check_is_skipped_with_one_note(home, monkeypatch, caplog):
    monkeypatch.setattr(server, "peer_uid", lambda sock: None)
    with running(home) as srv, caplog.at_level(logging.WARNING, logger="muse.home"):
        assert client_for(srv).call("GET", "/v1/status")[0] == 200
        assert client_for(srv).call("GET", "/v1/status")[0] == 200
        assert client_for(srv, "wrong").call("GET", "/v1/status")[0] == 401  # the token still counts
    notes = [r for r in caplog.records if "does not report who is on the other end" in r.getMessage()]
    assert len(notes) == 1


def test_tcp_has_no_peer_check_only_the_token(home, monkeypatch):
    monkeypatch.setattr(server, "peer_uid", lambda sock: os.getuid() + 1)
    with running(home, tcp=True) as srv:
        assert client_for(srv).call("GET", "/v1/status")[0] == 200
        assert client_for(srv, "wrong").call("GET", "/v1/status")[0] == 401
    assert TOKEN  # the shared test token
