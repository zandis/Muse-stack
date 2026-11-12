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


class FakeProxy:
    """Plays scripted (status, headers, body) answers in order; after the script, a 200 completion."""

    def __init__(self) -> None:
        self.script: deque[tuple[int, dict[str, str], bytes]] = deque()
        self.requests: list[dict[str, Any]] = []
        self.on_request: Any = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                body = json.loads(raw or b"{}")
                proxy.requests.append({
                    "path": self.path, "auth": self.headers.get("authorization", ""), "privacy": self.headers.get("x-muse-privacy"),
                    "model": body.get("model"), "max_tokens": body.get("max_tokens"), "prompt": body["messages"][0]["content"],
                    "task": self.headers.get("x-muse-task"),
                })
                if proxy.on_request:
                    proxy.on_request(len(proxy.requests))
                status, headers, data = proxy.script.popleft() if proxy.script else (
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
