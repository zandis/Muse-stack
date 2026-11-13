"""Test doubles for the queue: a scriptable stand-in for the proxy and an injectable clock."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CANARY_PROMPT = "CANARY-PROMPT-91c2f0"
CANARY_REPLY = "CANARY-REPLY-4be7a1"


def completion(text: str = CANARY_REPLY, tin: int = 11, tout: int = 7) -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                       "usage": {"prompt_tokens": tin, "completion_tokens": tout}}).encode()


def error(message: str, status: int = 429) -> bytes:
    return json.dumps({"error": {"message": message, "type": "x", "code": status}}).encode()


def embeddings(vectors: list[list[float]], tokens: int = 5) -> bytes:
    return json.dumps({"object": "list", "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
                       "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}).encode()


def ration_429(code: str = "defer:reserve", retry_after: int | None = 600, options: str | None = "wait:2026-11-14T00:00:00Z",
               wait: str | None = None) -> tuple[int, dict[str, str], bytes]:
    """A 429 as the real router sends it: Retry-After, x-muse-ration (or x-muse-wait) and the ` | options=` trailer."""
    headers: dict[str, str] = {}
    if code:
        headers["x-muse-ration"] = code
    if wait:
        headers["x-muse-wait"] = wait
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    message = "[muse] held back: nothing free fits right now" + (f" | options={options}" if options is not None else "")
    return 429, headers, error(message, 429)


def privacy_403(cls: str = "local") -> tuple[int, dict[str, str], bytes]:
    """The router's privacy refusal: no deployment may receive a request of this class."""
    return 403, {"x-muse-privacy": cls}, error(
        f"[muse] no deployment of 'cheap' may receive a '{cls}' request (privacy policy): only local models qualify", 403)


class FakeProxy:
    """Plays scripted (status, headers, body) answers in order; after the script, a 200 completion."""

    def __init__(self) -> None:
        self.script: deque[tuple[int, dict[str, str], bytes]] = deque()
        self.requests: list[dict[str, Any]] = []
        self.on_request: Any = None
        self.responder: Any = None  # callable(request dict) -> (status, headers, body) | None, used once the script is empty
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw or b"{}")
                messages = body.get("messages") or []
                request = {
                    "path": self.path, "auth": self.headers.get("authorization", ""), "privacy": self.headers.get("x-muse-privacy"),
                    "model": body.get("model"), "max_tokens": body.get("max_tokens"),
                    "prompt": messages[0]["content"] if messages else None,
                    "task": self.headers.get("x-muse-task"),
                    # For the skills tests: the whole request (an embeddings call has `input`, no messages).
                    "messages": messages, "input": body.get("input"), "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
                proxy.requests.append(request)
                if proxy.on_request:
                    proxy.on_request(len(proxy.requests))
                answer = None
                if proxy.script:
                    answer = proxy.script.popleft()
                elif proxy.responder is not None:
                    answer = proxy.responder(request)  # (status, headers, body) or None for the default
                if answer is None and self.path.endswith("/embeddings"):
                    texts = body.get("input") or []
                    answer = (200, {"x-muse-provider": "freea", "x-muse-deployment": "e-model", "x-muse-cost-usd": "0.000000"},
                              embeddings([[1.0, 0.0]] * len(texts if isinstance(texts, list) else [texts])))
                status, headers, data = answer if answer is not None else (
                    200, {"x-muse-provider": "freea", "x-muse-deployment": "a-model", "x-muse-cost-usd": "0.000500"}, completion())
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def push(self, status: int, headers: dict[str, str] | None = None, body: bytes | None = None) -> None:
        self.script.append((status, headers or {}, body if body is not None else error("x", status)))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Clock:
    """A clock the runner's sleep advances. Records every sleep so tests can prove nothing busy-polls."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []
        self.on_sleep: Any = None

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, "a zero or negative sleep is a busy loop"
        self.sleeps.append(seconds)
        self.t += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))
