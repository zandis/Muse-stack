"""An OpenAI-compatible provider that answers from a script.

Used by router tests and by the cassette recorder. It speaks the chat and
embedding endpoints on 127.0.0.1 and never contacts a real provider.

Scenarios (set per model name):

- `ok`                      a normal chat completion
- `slow`                    the same, after a short pause
- `empty_reasoning`         HTTP 200, empty content, reasoning tokens spent
- `reasoning_below:<N>`     empty reasoning when max_tokens is below N, else `ok`
- `429_retry_after:<sec>`  a 429 with Retry-After
- `429_groq_rpm`            Groq's per-minute 429, with its rate-limit headers
- `429_groq_tpd`            Groq's tokens-per-day 429, with a "try again in" hint
- `429_gemini_day`          Gemini's PerDay quota body (the short retryDelay is a decoy)
- `429_gemini_minute`       Gemini's PerMinute quota body
- `429_unclassified`        a 429 whose text says "quota" and names no clock
- `429_bare`                a 429 with no quota word and no clock
- `403_quota`               HTTP 403 whose message says the quota is used up
- `sse_error`               HTTP 200, `text/event-stream`, an error object after a reasoning delta

`tests/integration/fake_upstream.py` is the older stand-in the proxy harness
grew up with. This one is the script the fidelity tests share.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Groq's RPM wording, shortened. The "try again in" hint is the clock.
GROQ_RPM = (
    "Rate limit reached for model `openai/gpt-oss-120b` on requests per minute (RPM): "
    "Limit 30, Used 30, Requested 1. Please try again in 2s."
)
GROQ_TPD = (
    "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` "
    "on tokens per day (TPD): Limit 200000, Used 193487, Requested 7464. Please try again in 6m50.832s."
)
GEMINI_DAY = {
    "error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaValue": "250",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
        ],
    }
}
GEMINI_MINUTE = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "quota",
        "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
        ],
    }
}


@dataclass
class Logged:
    path: str
    model: str
    status: int
    ts: float
    max_tokens: int | None = None
    stream: bool = False


@dataclass(frozen=True)
class Scripted:
    status: int
    payload: Any
    headers: dict[str, str]
    chunks: tuple[str, ...] | None = None
    delay: float = 0.0

    def body(self) -> bytes:
        if self.chunks is not None:
            return ("\n\n".join(self.chunks) + "\n\n").encode()
        return json.dumps(self.payload).encode()


def _chat(text: str, *, content: str | None = None, reasoning: str | None = None, finish: str = "stop",
          reasoning_tokens: int = 0) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text if content is None else content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    usage: dict[str, Any] = {"prompt_tokens": 11, "completion_tokens": 7 + reasoning_tokens, "total_tokens": 18 + reasoning_tokens}
    if reasoning_tokens:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


def _embeddings() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "model": "fake",
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def script_for(name: str, *, path: str = "", max_tokens: int | None = None, reply: str = "fake reply") -> Scripted:
    """The response `name` produces. Unknown names raise `ValueError`."""
    if path.endswith("/embeddings") and name in {"ok", "slow", ""}:
        return Scripted(200, _embeddings(), {"content-type": "application/json"}, delay=0.25 if name == "slow" else 0.0)
    if name in {"ok", "slow", ""}:
        return Scripted(200, _chat(reply), {"content-type": "application/json"}, delay=0.25 if name == "slow" else 0.0)
    if name == "empty_reasoning":
        return Scripted(
            200,
            _chat("", content="", reasoning="thinking it over", finish="length", reasoning_tokens=16),
            {"content-type": "application/json"},
        )
    if name.startswith("reasoning_below:"):
        floor = int(name.split(":", 1)[1])
        starved = max_tokens is not None and max_tokens < floor
        return script_for("empty_reasoning" if starved else "ok", reply=reply)
    if name.startswith("429_retry_after:"):
        seconds = name.split(":", 1)[1]
        return Scripted(
            429,
            {"error": {"message": "Rate limit reached", "type": "rate_limit", "code": 429}},
            {"content-type": "application/json", "retry-after": seconds},
        )
    if name == "429_groq_rpm":
        return Scripted(
            429,
            {"error": {"message": GROQ_RPM, "type": "rate_limit", "code": 429}},
            {
                "content-type": "application/json",
                "retry-after": "2",
                "x-ratelimit-limit-requests": "14400",
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "2s",
                "x-ratelimit-limit-tokens": "6000",
                "x-ratelimit-remaining-tokens": "5900",
                "x-ratelimit-reset-tokens": "2s",
            },
        )
    if name == "429_groq_tpd":
        return Scripted(
            429,
            {"error": {"message": GROQ_TPD, "type": "rate_limit", "code": 429}},
            {"content-type": "application/json", "retry-after": "411"},
        )
    if name == "429_gemini_day":
        return Scripted(429, GEMINI_DAY, {"content-type": "application/json"})
    if name == "429_gemini_minute":
        return Scripted(429, GEMINI_MINUTE, {"content-type": "application/json", "retry-after": "7"})
    if name == "429_unclassified":
        return Scripted(
            429,
            {"error": {"message": "You exceeded your quota", "code": 429}},
            {"content-type": "application/json"},
        )
    if name == "429_bare":
        return Scripted(429, {"error": {"message": "too many requests", "code": 429}}, {"content-type": "application/json"})
    if name == "403_quota":
        return Scripted(
            403,
            {"error": {"code": 403, "status": "RESOURCE_EXHAUSTED", "message": "Your quota has been exceeded for this model."}},
            {"content-type": "application/json"},
        )
    if name == "sse_error":
        frames = (
            'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
            'data: {"error":{"message":"You exceeded your quota","code":429}}',
            "data: [DONE]",
        )
        return Scripted(200, None, {"content-type": "text/event-stream"}, chunks=frames)
    raise ValueError(f"unknown scenario {name!r}")


@dataclass
class FakeProvider:
    """One local server. Behaviour is chosen by the model name in the JSON body."""

    scripts: dict[str, str] = field(default_factory=dict)
    replies: dict[str, str] = field(default_factory=dict)
    default: str = "ok"
    slow_seconds: float = 0.25
    log: list[Logged] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def base(self, prefix: str = "") -> str:
        """`http://127.0.0.1:<port><prefix>/v1`, the shape the config renderer uses."""
        return f"{self.origin()}{prefix}/v1"

    def set(self, model: str, scenario: str) -> None:
        script_for(scenario)  # reject a bad name at setup, not on the first request
        with self._lock:
            self.scripts[model] = scenario

    def set_reply(self, model: str, text: str) -> None:
        with self._lock:
            self.replies[model] = text

    def calls(self, model: str | None = None) -> list[Logged]:
        with self._lock:
            return [item for item in self.log if model is None or item.model == model or item.model.endswith("/" + (model or ""))]

    def clear_log(self) -> None:
        with self._lock:
            self.log.clear()

    def _scenario(self, model: str) -> str:
        suffix = model.split("/")[-1]
        return self.scripts.get(model) or self.scripts.get(suffix) or self.default

    def start(self) -> "FakeProvider":
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self._send(200, b'{"object":"list","data":[]}', {"content-type": "application/json"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                model = str(body.get("model", "")) if isinstance(body, dict) else ""
                sent = body.get("max_tokens", body.get("max_completion_tokens")) if isinstance(body, dict) else None
                sent_max = sent if isinstance(sent, int) and not isinstance(sent, bool) else None
                stream = bool(body.get("stream")) if isinstance(body, dict) else False
                with provider._lock:
                    scenario = provider._scenario(model)
                    reply = provider.replies.get(model) or provider.replies.get(model.split("/")[-1]) or "fake reply"
                scripted = script_for(scenario, path=self.path, max_tokens=sent_max, reply=reply)
                if scenario == "slow":
                    scripted = Scripted(scripted.status, scripted.payload, scripted.headers, scripted.chunks, provider.slow_seconds)
                if scripted.delay:
                    time.sleep(scripted.delay)
                payload = scripted.body()
                with provider._lock:
                    provider.log.append(Logged(self.path, model, scripted.status, time.time(), sent_max, stream or scripted.chunks is not None))
                self._send(scripted.status, payload, scripted.headers)

            def _send(self, status: int, payload: bytes, headers: dict[str, str]) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
