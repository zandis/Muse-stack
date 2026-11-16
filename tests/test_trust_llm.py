"""GatewayLLM against a scriptable local HTTP server (no LiteLLM needed)."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wardmuse.trust.contracts import LLMRefused, LLMUnavailable
from wardmuse.trust.llm import GatewayLLM, RequestTooLarge, UnverifiedRoute

KEY = "sk-muse-test-SECRET-0123456789abcdef"
GOOD = {"x-muse-provider": "local_openai", "x-muse-deployment": "local-model__smart", "x-muse-privacy": "local"}


def completion(text: str = "hello") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


class Server:
    """Answers each POST with the next scripted (status, headers, body-bytes); logs requests."""

    def __init__(self) -> None:
        self.script: list[tuple[int, dict[str, str], bytes]] = []
        self.requests: list[dict[str, Any]] = []
        self.delay = 0.0
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                return

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                server.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw})
                if server.delay:
                    time.sleep(server.delay)
                status, headers, body = server.script.pop(0) if server.script else (200, GOOD, json.dumps(completion()).encode())
                self.send_response(status)
                self.send_header("content-length", str(len(body)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.05), daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def add(self, status: int, headers: dict[str, str] | None = None, body: Any = None) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body if body is not None else completion()).encode()
        self.script.append((status, headers if headers is not None else GOOD, raw))

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    s = Server()
    yield s
    s.stop()


def client(server: Server, **kw: Any) -> GatewayLLM:
    env = {"MUSE_PROXY_URL": server.url, "MUSE_TEST_KEY": KEY, "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
    kw.setdefault("probe_ttl", 0)  # most tests script one response; the probe has its own tests
    return GatewayLLM(key_env="MUSE_TEST_KEY", environ=env, timeout=kw.pop("timeout", 5), **kw)


MSG = [{"role": "user", "content": "hi"}]


def test_success_sends_privacy_and_bearer_and_reads_route(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # must be ignored
    result = client(server).complete(MSG, privacy="local", alias="fast", max_tokens=50)
    assert result.text == "hello" and result.provider == "local_openai" and result.deployment == "local-model__smart"
    assert result.privacy == "local" and result.tokens_in == 3 and result.tokens_out == 2
    req = server.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["headers"]["x-muse-privacy"] == "local" and req["headers"]["authorization"] == f"Bearer {KEY}"
    assert json.loads(req["body"]) == {"model": "fast", "messages": MSG, "max_tokens": 50, "stream": False}


def test_stricter_class_than_requested_is_fine(server):
    server.add(200, {**GOOD, "x-muse-privacy": "local"})
    assert client(server).complete(MSG, privacy="private").privacy == "local"


@pytest.mark.parametrize("headers", [
    {},                                                         # hook not loaded
    {"x-muse-privacy": "local"},                                # no provider
    {"x-muse-provider": "gemini", "x-muse-deployment": "g"},    # no privacy
    {**GOOD, "x-muse-privacy": "standard"},                     # weaker
    {**GOOD, "x-muse-privacy": "bogus"},                        # unknown
])
def test_strict_route_refuses_unproven_responses(server, headers):
    server.add(200, headers, completion("SHOULD-NOT-BE-SEEN"))
    with pytest.raises(UnverifiedRoute) as info:
        client(server).complete(MSG, privacy="local")
    assert isinstance(info.value, LLMRefused) and info.value.content_sent
    assert "SHOULD-NOT-BE-SEEN" not in str(info.value)


def test_non_strict_returns_unproven_text(server):
    server.add(200, {}, completion("plain proxy"))
    result = client(server, strict_route=False).complete(MSG, privacy="local")
    assert result.text == "plain proxy" and result.provider is None and result.privacy is None


@pytest.mark.parametrize("status,headers,body,exc,needle", [
    (403, {}, {"error": {"message": "[muse] no deployment of 'smart' may receive a 'local' request"}}, LLMRefused, "'local'"),
    (400, {}, {"error": {"message": "[muse] invalid x-muse-privacy: nope"}}, LLMRefused, "invalid"),
    (403, {}, {"error": {"message": "Authentication Error, not allowed"}}, LLMUnavailable, "not a privacy decision"),
    (401, {}, {"error": {"message": f"Invalid key {KEY}"}}, LLMUnavailable, "$MUSE_TEST_KEY"),
    (429, {"retry-after": "7"}, {"error": {"message": "rate limited"}}, LLMUnavailable, "retry after 7s"),
    (429, {}, {"error": {"message": "[muse] blocked by a provider quota; soonest reset 2026-11-12T09:00:00Z (exhausted: daily)"}},
     LLMUnavailable, "soonest retry 2026-11-12T09:00:00Z"),
    (500, {}, {"error": {"message": "boom"}}, LLMUnavailable, "HTTP 500"),
    (503, {}, b"<html>bad gateway</html>", LLMUnavailable, "HTTP 503"),
    (404, {}, {"error": {"message": "no such model"}}, LLMUnavailable, "HTTP 404"),
    (200, GOOD, b"not json", LLMUnavailable, "not with a chat completion"),
    (200, GOOD, {"choices": []}, LLMUnavailable, "not with a chat completion"),
])
def test_status_mapping(server, status, headers, body, exc, needle):
    server.add(status, headers, body)
    with pytest.raises(exc) as info:
        client(server).complete(MSG, privacy="local")
    assert needle in str(info.value)
    if exc is LLMUnavailable:
        assert not isinstance(info.value, LLMRefused)
    assert KEY not in str(info.value) and KEY not in repr(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None or KEY not in repr(info.value.__context__)


def test_refusal_is_not_an_unverified_route(server):
    server.add(403, {}, {"error": {"message": "[muse] nope"}})
    with pytest.raises(LLMRefused) as info:
        client(server).complete(MSG, privacy="local")
    assert not getattr(info.value, "content_sent", False)


def test_network_errors_and_timeouts():
    dead = GatewayLLM("http://127.0.0.1:9", key_env="NOPE", environ={})
    with pytest.raises(LLMUnavailable, match="unreachable"):
        dead.complete(MSG, privacy="local")
    s = Server()
    s.delay = 1.5
    try:
        with pytest.raises(LLMUnavailable, match="timed out"):
            client(s, timeout=0.3).complete(MSG, privacy="local")
    finally:
        s.stop()


def test_size_caps(server):
    with pytest.raises(RequestTooLarge):
        client(server, max_request_bytes=1000).complete([{"role": "user", "content": "x" * 2000}], privacy="local")
    assert server.requests == []
    server.add(200, GOOD, completion("y" * 5000))
    with pytest.raises(LLMUnavailable, match="cap"):
        client(server, max_response_bytes=1000).complete(MSG, privacy="local")


def test_redirects_are_not_followed(server):
    other = Server()
    try:
        server.add(307, {"location": other.url + "/v1/chat/completions"}, b"")
        with pytest.raises(LLMUnavailable):
            client(server).complete(MSG, privacy="local")
        assert other.requests == []
    finally:
        other.stop()


def test_unknown_privacy_class_never_sends(server):
    with pytest.raises(ValueError):
        client(server).complete(MSG, privacy="secret-ish")
    assert server.requests == []


def test_key_never_in_repr_or_attributes(server):
    llm = client(server)
    assert KEY not in repr(llm) and "MUSE_TEST_KEY" in repr(llm)
    assert all(KEY not in repr(v) for k, v in vars(llm).items() if k != "_environ")


def test_no_key_configured_sends_no_authorization(server):
    GatewayLLM(server.url, key_env="MISSING_KEY", environ={}).complete(MSG, privacy="local")
    assert "authorization" not in server.requests[0]["headers"]


def test_probe_refuses_before_any_content_is_sent(server):
    server.add(200, {}, completion("plain proxy"))  # the probe's answer: no proof of enforcement
    llm = client(server, probe_ttl=300)
    with pytest.raises(UnverifiedRoute) as info:
        llm.complete([{"role": "user", "content": "RECORD-TEXT-MARKER"}], privacy="local")
    assert info.value.content_sent is False
    assert len(server.requests) == 1 and b"RECORD-TEXT-MARKER" not in server.requests[0]["body"]
    assert json.loads(server.requests[0]["body"])["messages"] == [{"role": "user", "content": "ping"}]


def test_probe_is_cached_per_class_and_alias(server):
    now = [0.0]
    llm = client(server, probe_ttl=60, clock=lambda: now[0])
    llm.complete(MSG, privacy="local", alias="fast")
    llm.complete(MSG, privacy="local", alias="fast")
    assert len(server.requests) == 3  # one probe, two calls
    llm.complete(MSG, privacy="private", alias="fast")
    assert len(server.requests) == 5  # new class: new probe
    now[0] = 61
    llm.complete(MSG, privacy="local", alias="fast")
    assert len(server.requests) == 7  # expired


def test_unproven_response_after_a_good_probe_still_refused_and_resets_cache(server):
    now = [0.0]
    llm = client(server, probe_ttl=60, clock=lambda: now[0])
    server.add(200)          # probe ok
    server.add(200, {}, completion("x"))  # hook vanished between probe and call
    with pytest.raises(UnverifiedRoute) as info:
        llm.complete(MSG, privacy="local")
    assert info.value.content_sent is True
    llm.complete(MSG, privacy="local")
    assert len(server.requests) == 4  # cache was cleared, so it probed again
