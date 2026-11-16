"""muse.home.server: the transport. Socket modes, size limits, timeouts, Host checks, no CORS."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import logging
import os
import socket
import stat
import time
from pathlib import Path

import pytest

from home_fakes import TOKEN, assert_error, client_for, raw_exchange, running, short_dir, split_response, verifier
from wardmuse.home import api, server
from wardmuse.home.client import HomeClient

AUTH = f"Authorization: Bearer {TOKEN}\r\n".encode()


@pytest.fixture()
def home():
    return api.Home(snapshot_fn=lambda: {"generated_at": "2026-11-13T00:00:00Z"}, proxy_running=lambda: False)


def get(srv, target: bytes, extra: bytes = b"", host: bytes = b"localhost") -> tuple[int, dict, bytes]:
    return split_response(raw_exchange(srv, b"GET " + target + b" HTTP/1.1\r\nHost: " + host + b"\r\n" + AUTH + extra + b"\r\n"))


# ---------------------------------------------------------------- the socket file


def test_the_default_socket_is_private_inside_a_private_folder(home):
    srv = server.make_server(home, verifier())
    try:
        path = Path(envget(os.environ, "MUSE_HOME")) / "home.sock"
        assert srv.socket_path == path and srv.where == f"unix:{path}" and not srv.is_tcp
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        srv.server_close()
    assert not path.exists()  # removed when the server stops


def test_the_socket_is_0600_even_under_a_loose_umask(home):
    old = os.umask(0o000)
    try:
        with running(home) as srv:
            assert stat.S_IMODE(srv.socket_path.stat().st_mode) == 0o600
    finally:
        os.umask(old)
    assert os.umask(old) == old  # and the server put the umask back


def test_a_missing_folder_is_created_private(home):
    with short_dir() as folder:
        path = folder / "deep" / "er" / "home.sock"
        with running(home, socket_path=path):
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_stale_socket_is_replaced_but_a_live_home_is_not(home):
    with short_dir() as folder:
        path = folder / "home.sock"
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(path))
        dead.close()  # the file stays, nobody listens
        with running(home, socket_path=path) as srv:
            assert client_for(srv).call("GET", "/v1/status")[0] == 200
            with pytest.raises(ValueError, match="already running"):
                server.make_server(home, verifier(), socket_path=path)
            assert client_for(srv).call("GET", "/v1/status")[0] == 200  # the first one is untouched


def test_it_refuses_to_replace_something_that_is_not_a_socket(home):
    with short_dir() as folder:
        path = folder / "home.sock"
        path.write_text("precious")
        with pytest.raises(ValueError, match="not a socket"):
            server.make_server(home, verifier(), socket_path=path)
        assert path.read_text() == "precious"
        link = folder / "link.sock"
        link.symlink_to(path)
        with pytest.raises(ValueError, match="not a socket"):
            server.make_server(home, verifier(), socket_path=link)


def test_a_path_too_long_for_a_socket_is_explained(home):
    with short_dir() as folder:
        with pytest.raises(ValueError, match="too long"):
            server.make_server(home, verifier(), socket_path=folder / ("x" * 120) / "home.sock")


def test_closing_does_not_remove_a_socket_that_is_no_longer_ours(home):
    with short_dir() as folder:
        path = folder / "home.sock"
        srv = server.make_server(home, verifier(), socket_path=path)
        path.unlink()
        path.write_text("someone else's file")
        srv.server_close()
        assert path.read_text() == "someone else's file"


# ---------------------------------------------------------------- TCP


def test_tcp_binds_loopback_only_and_checks_the_host_header(home):
    with pytest.raises(ValueError, match="only listens on 127.0.0.1"):
        server.TcpHomeServer(0, home, verifier(), host="0.0.0.0")
    with running(home, tcp=True) as srv:
        assert srv.server_address[0] == "127.0.0.1" and srv.where == f"http://127.0.0.1:{srv.port}"
        port = str(srv.port).encode()
        assert get(srv, b"/v1/status", host=b"127.0.0.1:" + port)[0] == 200
        assert get(srv, b"/v1/status", host=b"localhost:" + port)[0] == 200
        for host in (b"evil.example:" + port, b"127.0.0.1", b"127.0.0.1:1", b"attacker.test"):
            status, _, body = get(srv, b"/v1/status", host=host)
            assert_error(status, json.loads(body), 403, "forbidden")
        raw = raw_exchange(srv, b"GET /v1/status HTTP/1.0\r\n" + AUTH + b"\r\n")  # no Host at all
        assert split_response(raw)[0] == 403


def test_tcp_is_chosen_when_asked_or_when_there_are_no_unix_sockets(home, monkeypatch):
    srv = server.make_server(home, verifier(), tcp_port=0)
    try:
        assert srv.is_tcp
    finally:
        srv.server_close()
    monkeypatch.setattr(server, "unix_sockets_available", lambda: False)
    monkeypatch.setattr(server, "DEFAULT_TCP_PORT", 0)
    srv = server.make_server(home, verifier())
    try:
        assert srv.is_tcp and srv.server_address[0] == "127.0.0.1"
        assert HomeClient(TOKEN, tcp_port=srv.port).tcp_port == srv.port
    finally:
        srv.server_close()


# ---------------------------------------------------------------- replies


@pytest.mark.parametrize("tcp", [False, True])
def test_replies_are_json_uncached_and_carry_no_cors(home, tcp):
    with running(home, tcp=tcp) as srv:
        host = f"127.0.0.1:{srv.port}".encode() if tcp else b"localhost"
        status, headers, body = get(srv, b"/v1/status", b"Origin: https://evil.example\r\n", host=host)
        assert status == 200 and json.loads(body)["api_version"] == "1"
        assert headers["content-type"] == "application/json; charset=utf-8"
        assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
        assert headers["connection"] == "close" and headers["server"] == "MuseHome"
        assert int(headers["content-length"]) == len(body)
        raw = raw_exchange(srv, b"OPTIONS /v1/status HTTP/1.1\r\nHost: " + host + b"\r\nOrigin: https://evil.example\r\n"
                           b"Access-Control-Request-Method: GET\r\nAccess-Control-Request-Headers: authorization\r\n" + AUTH + b"\r\n")
        o_status, o_headers, _ = split_response(raw)
        assert o_status == 405
        for h in (headers, o_headers):
            assert not any(k.startswith("access-control-") for k in h)
        pre = split_response(raw_exchange(srv, b"OPTIONS /v1/status HTTP/1.1\r\nHost: " + host + b"\r\nOrigin: https://evil.example\r\n\r\n"))
        assert pre[0] == 401 and not any(k.startswith("access-control-") for k in pre[1])  # a browser preflight has no token


def test_head_has_no_body_and_odd_methods_are_refused(home):
    with running(home) as srv:
        raw = raw_exchange(srv, b"HEAD /v1/status HTTP/1.1\r\nHost: x\r\n" + AUTH + b"\r\n")
        status, headers, body = split_response(raw)
        assert status == 405 and body == b"" and headers["allow"] == "GET"
        for method in (b"TRACE", b"CONNECT", b"BREW"):
            status, _, body = split_response(raw_exchange(srv, method + b" /v1/status HTTP/1.1\r\nHost: x\r\n" + AUTH + b"\r\n"))
            assert_error(status, json.loads(body), 405, "method_not_allowed")


@pytest.mark.parametrize("target", [
    b"/v1/../etc/passwd", b"/v1/cards/../../etc/passwd", b"/v1/cards/..%2f..%2fetc%2fpasswd", b"/../../etc/passwd", b"/etc/passwd",
    b"/v1/status/", b"/v1//status", b"/v1/apps/..", b"/v1/jobs/%2e%2e", b"/v1/cards/" + b"a" * 600, b"/index.html", b"/", b"//evil/v1/status",
    b"/v1/status%00", b"/V1/STATUS", b"/v2/status",
])
def test_paths_are_matched_against_the_route_table_never_the_file_system(home, target):
    with running(home) as srv:
        status, _, body = get(srv, target)
        assert_error(status, json.loads(body), 404, "not_found")
        assert b"root:" not in body and target not in body


def test_garbage_gets_the_one_error_shape_and_the_server_survives(home):
    with running(home) as srv:
        for junk in (b"\x00\x01\x02 nonsense\r\n\r\n", b"GET\r\n\r\n", b"GET /v1/status HTTP/9.9\r\n\r\n", b"GET " + b"/a" * 40000 + b" HTTP/1.1\r\n\r\n"):
            raw = raw_exchange(srv, junk)
            if raw:
                status, headers, body = split_response(raw)
                assert_error(status, json.loads(body), 400, "invalid_request")
                assert headers["content-type"].startswith("application/json")
        assert raw_exchange(srv, b"") == b""
        assert client_for(srv).call("GET", "/v1/status")[0] == 200


# ---------------------------------------------------------------- bounded requests


def post(srv, headers: bytes, body: bytes = b"") -> tuple[int, dict, bytes]:
    return split_response(raw_exchange(srv, b"POST /v1/pause HTTP/1.1\r\nHost: x\r\n" + AUTH + headers + b"\r\n" + body))


def test_a_body_over_the_limit_is_refused_before_it_is_read(home):
    with running(home) as srv:
        too_big = api.MAX_BODY_BYTES + 1
        started = time.monotonic()
        status, _, body = post(srv, f"Content-Length: {too_big}\r\n".encode())  # the body is never sent
        assert_error(status, json.loads(body), 413, "payload_too_large")
        assert time.monotonic() - started < 3  # it did not wait for the bytes
        status, _, body = post(srv, f"Content-Length: {too_big}\r\n".encode(), b'{"pad": "' + b"a" * 4096)  # the start of one
        assert_error(status, json.loads(body), 413, "payload_too_large")
        assert client_for(srv).call("GET", "/v1/jobs")[1] == {"jobs": []}


def test_a_body_at_the_limit_is_read(home):
    with running(home) as srv:
        body = b'{"pad": "' + b"a" * (api.MAX_BODY_BYTES - 11) + b'"}'
        assert len(body) == api.MAX_BODY_BYTES
        status, _, reply = post(srv, f"Content-Length: {len(body)}\r\n".encode(), body)
        assert_error(status, json.loads(reply), 400, "invalid_request")  # read and judged, not cut off


def test_an_unauthenticated_caller_cannot_make_the_home_read_a_large_body(home):
    with running(home) as srv:
        raw = raw_exchange(srv, f"POST /v1/jobs HTTP/1.1\r\nHost: x\r\nContent-Length: {10**9}\r\n\r\n".encode())
        assert split_response(raw)[0] == 401


@pytest.mark.parametrize("headers, status, code", [
    (b"Transfer-Encoding: chunked\r\n", 411, "length_required"),
    (b"Content-Length: lots\r\n", 400, "invalid_request"),
    (b"Content-Length: -5\r\n", 400, "invalid_request"),
    (b"Content-Length: 9999999999999999\r\n", 400, "invalid_request"),
])
def test_a_body_must_declare_a_sane_length(home, headers, status, code):
    with running(home) as srv:
        got, _, body = post(srv, headers)
        assert_error(got, json.loads(body), status, code)


def test_a_silent_or_stalled_client_is_dropped_after_the_timeout(home, monkeypatch):
    monkeypatch.setattr(server._Handler, "timeout", 0.4)
    with running(home) as srv:
        started = time.monotonic()
        assert raw_exchange(srv, b"", timeout=5) == b""  # says nothing: closed on it
        assert raw_exchange(srv, b"POST /v1/pause HTTP/1.1\r\nHost: x\r\n" + AUTH + b"Content-Length: 50\r\n\r\n{", timeout=5) == b""
        assert time.monotonic() - started < 4
        assert client_for(srv).call("GET", "/v1/status")[0] == 200  # others are still served
    assert not home.is_paused()


def test_many_requests_at_once_are_all_answered(home):
    import threading

    results: list[int] = []
    with running(home) as srv:
        def one() -> None:
            results.append(client_for(srv).call("GET", "/v1/status")[0])

        threads = [threading.Thread(target=one) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert results == [200] * 24


def test_the_log_names_the_route_and_device_never_the_request(home, caplog):
    secret_path = "/v1/cards/CANARY-PATH-77aa?status=CANARY-QUERY-88bb"
    with running(home) as srv, caplog.at_level(logging.DEBUG):
        client_for(srv).call("GET", "/v1/status")
        client_for(srv).call("GET", secret_path)
        client_for(srv).call("GET", "/v1/cards/0123456789abcdef?x=CANARY-QUERY-88bb")
        client_for(srv, "CANARY-TOKEN-99cc").call("GET", "/v1/status")
        client_for(srv).call("POST", "/v1/jobs", {"prompt": "CANARY-BODY-66dd"})
    text = caplog.text
    assert "GET /v1/status -> 200 test-phone" in text
    assert "GET /v1/cards/{id} -> 400 test-phone" in text
    assert "POST /v1/jobs -> 201 test-phone" in text
    for canary in ("CANARY-PATH", "CANARY-QUERY", "CANARY-TOKEN", "CANARY-BODY", TOKEN):
        assert canary not in text
