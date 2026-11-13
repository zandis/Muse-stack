"""A scriptable OpenAI-compatible upstream for integration tests.

Stdlib only. Each request is logged (path, model, a marker flag) so tests can
prove which upstream was or was not contacted. Behaviour is scripted per model
name, so one server can stand in for several providers.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

GEMINI_DAILY_BODY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaDimensions": {"location": "global", "model": "gemini-x"},
                        "quotaValue": "250",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
        ],
    }
}


@dataclass
class Logged:
    path: str
    model: str
    status: int
    ts: float
    max_tokens: int | None = None  # what the proxy sent: max_tokens or max_completion_tokens


@dataclass
class FakeUpstream:
    """Behaviours: ok, slow_ok, ok_reasoning_only, reasoning_below:<N>, 429_minute, 429_retry_after:<s>, 429_daily, 401, 400, 500.

    `reasoning_below:<N>` is a reasoning model: a request whose max_tokens (or
    max_completion_tokens) is below N gets the reasoning-only reply, any other
    request (a larger cap, or none) the normal one.
    """

    behaviours: dict[str, str] = field(default_factory=dict)
    limit_headers: dict[str, dict[str, str]] = field(default_factory=dict)  # per model, replaces the default x-ratelimit-*
    replies: dict[str, str] = field(default_factory=dict)  # per model, the chat text of an `ok` reply (default "fake reply")
    default: str = "ok"
    slow_seconds: float = 3.0
    log: list[Logged] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    def base(self, prefix: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{prefix}/v1"

    def set(self, model: str, behaviour: str) -> None:
        with self._lock:
            self.behaviours[model] = behaviour

    def set_headers(self, model: str, headers: dict[str, str]) -> None:
        """Rate-limit headers this model returns on success ({} = none at all)."""
        with self._lock:
            self.limit_headers[model] = dict(headers)

    def set_reply(self, model: str, text: str) -> None:
        """The text this model answers a chat with (for callers that need a scripted answer, e.g. a JSON object)."""
        with self._lock:
            self.replies[model] = text

    def calls(self, model: str | None = None) -> list[Logged]:
        with self._lock:
            return [item for item in self.log if model is None or item.model == model]

    def clear_log(self) -> None:
        with self._lock:
            self.log.clear()

    def start(self) -> "FakeUpstream":
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # keep test output quiet
                return

            def do_GET(self) -> None:
                self._send(200, {"object": "list", "data": []}, {})

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                model = str(body.get("model", ""))
                with upstream._lock:
                    behaviour = upstream.behaviours.get(model, upstream.default)
                sent_max = body.get("max_tokens", body.get("max_completion_tokens"))
                sent_max = sent_max if isinstance(sent_max, int) and not isinstance(sent_max, bool) else None
                if behaviour.startswith("reasoning_below:"):
                    starved = sent_max is not None and sent_max < int(behaviour.split(":", 1)[1])
                    behaviour = "ok_reasoning_only" if starved else "ok"
                status, payload, headers = upstream._respond(behaviour, model, self.path)
                with upstream._lock:
                    upstream.log.append(Logged(self.path, model, status, time.time(), sent_max))
                if behaviour == "slow_ok":
                    time.sleep(upstream.slow_seconds)
                self._send(status, payload, headers)

            def _send(self, status: int, payload: Any, headers: dict[str, str]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def _respond(self, behaviour: str, model: str, path: str) -> tuple[int, Any, dict[str, str]]:
        if behaviour in {"ok", "slow_ok"}:
            limits = {
                "x-ratelimit-limit-requests": "1000",
                "x-ratelimit-remaining-requests": "998",
                "x-ratelimit-reset-requests": "2m59.56s",
                "x-ratelimit-limit-tokens": "6000",
                "x-ratelimit-remaining-tokens": "5900",
                "x-ratelimit-reset-tokens": "7.66s",
            }
            with self._lock:
                if model in self.limit_headers:
                    limits = dict(self.limit_headers[model])
                reply = self.replies.get(model, "fake reply")
            if path.endswith("/embeddings"):
                return 200, {
                    "object": "list",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                    "model": model,
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                }, limits
            return 200, {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }, limits
        if behaviour == "ok_reasoning_only":
            # A reasoning model that spent a tiny max_tokens before writing anything visible.
            return 200, {
                "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "", "reasoning_content": "thinking it over"},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 16, "total_tokens": 27,
                          "completion_tokens_details": {"reasoning_tokens": 16}},
            }, {}
        if behaviour == "429_minute":
            return 429, {"error": {"message": "Rate limit reached for requests per minute", "type": "requests", "code": "rate_limit_exceeded"}}, {
                "retry-after": "2",
            }
        if behaviour.startswith("429_retry_after:"):
            # A plain rate-limit 429 that says how long to stay away, in seconds.
            return 429, {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}, {
                "retry-after": behaviour.split(":", 1)[1],
            }
        if behaviour == "429_daily":
            return 429, GEMINI_DAILY_BODY, {}
        if behaviour == "401":
            return 401, {"error": {"message": "Invalid API Key", "type": "invalid_request_error", "code": "invalid_api_key"}}, {}
        if behaviour == "400":
            return 400, {"error": {"message": "bad request: unknown parameter", "type": "invalid_request_error"}}, {}
        if behaviour == "500":
            return 500, {"error": {"message": "internal error"}}, {"date": formatdate(usegmt=True)}
        raise ValueError(f"unknown behaviour {behaviour}")
