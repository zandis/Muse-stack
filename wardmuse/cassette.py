"""Record one provider exchange, then replay it with the network closed.

`MUSE_PROVIDER_MODE` selects the mode:

- `live` (unset, empty, or anything else): the router calls providers as it
  always has. Nothing here starts.
- `record`: the hook points each provider call at a local proxy. The proxy
  forwards to the real base URL, then writes a cassette. The cassette holds a
  fingerprint of the request and the response. It does not hold the prompt,
  an API key, or an auth header.
- `replay`: the same proxy answers from cassettes and opens no outbound
  connection. A request with no cassette gets HTTP 404. A free-tier quota
  is not touched.

The gate (`scripts/chores_check.py --record / --replay`) uses this same store
at the router the skill talks to, so a recorded gate run can be repeated
offline. See `docs/testing/fidelity-and-replay.md`.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from wardmuse.catalog import _SECRET_PREFIXES, looks_secret

MODES = ("live", "record", "replay")
VERSION = 1
MAX_BODY = 1_000_000  # bytes stored from one response; larger ones are cut and marked
DROPPED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "api-key", "x-goog-api-key", "x-muse-cassette-upstream",
})
# Hop-by-hop and length headers. Replay computes its own length.
SKIP_RESPONSE_HEADERS = DROPPED_HEADERS | frozenset({
    "content-length", "transfer-encoding", "connection", "keep-alive", "host",
})
_WORD = re.compile(r"\S+")


class CassetteError(RuntimeError):
    """Replay or record was asked for and the local proxy could not start.

    Raised so the caller does not quietly fall through to a real provider.
    """


def mode_of(env: Mapping[str, str] | None) -> str:
    """`live`, `record`, or `replay`. Any other value is `live`."""
    raw = str(envget((env or {}), "MUSE_PROVIDER_MODE", "") or "").strip().lower()
    return raw if raw in ("record", "replay") else "live"


def directory_of(env: Mapping[str, str] | None) -> Path | None:
    raw = str(envget((env or {}), "MUSE_CASSETTE_DIR", "") or "").strip()
    return Path(raw) if raw else None


def redact_text(text: str) -> str:
    """Replace key-shaped substrings. Ordinary words and hashes stay."""
    text = _SECRET_PREFIXES.sub("[redacted]", text)

    def one(match: re.Match[str]) -> str:
        word = match.group(0)
        return "[redacted]" if looks_secret(word) else word

    return _WORD.sub(one, text)


def _header_items(headers: Mapping[str, str] | None) -> list[tuple[str, str]]:
    if not headers:
        return []
    out = []
    for key, value in headers.items():
        name = str(key).strip().lower()
        if not name or name in SKIP_RESPONSE_HEADERS:
            continue
        out.append((name, redact_text(str(value))))
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def match_key(method: str, path: str, body: bytes) -> str:
    """Identity of one call: method, path, and the hash of the raw body.

    The body itself is not kept. Two calls with the same bytes replay the
    same response; a different prompt is a different key.
    """
    path = path.split("?", 1)[0]
    material = f"{method.upper()}\n{path}\n{_sha256(body)}".encode()
    return _sha256(material)


def _request_view(method: str, path: str, body: bytes) -> dict[str, Any]:
    """The fingerprint a person can read. Message text is not included."""
    view: dict[str, Any] = {
        "method": method.upper(),
        "path": path.split("?", 1)[0],
        "body_sha256": _sha256(body),
    }
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model:
            view["model"] = model[:200]
        if "stream" in payload:
            view["stream"] = bool(payload.get("stream"))
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                view["max_tokens"] = value
                break
        messages = payload.get("messages")
        if isinstance(messages, list):
            view["messages"] = len(messages)
        embedding = payload.get("input")
        if isinstance(embedding, list):
            view["inputs"] = len(embedding)
        elif isinstance(embedding, str):
            view["inputs"] = 1
    return view


def _chunks_of(text: str, content_type: str) -> list[str] | None:
    if "event-stream" not in content_type and "\ndata:" not in f"\n{text}":
        return None
    frames = [frame for frame in text.split("\n\n") if frame.strip()]
    return frames or None


@dataclass
class Exchange:
    """One recorded call. `request` is a fingerprint; `response` is what came back."""

    match: str
    request: dict[str, Any]
    status: int
    headers: list[tuple[str, str]]
    body: str
    chunks: list[str] | None
    elapsed_ms: int
    truncated: bool = False

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "headers": self.headers,
            "body": self.body,
        }
        if self.chunks is not None:
            response["chunks"] = self.chunks
        if self.truncated:
            response["truncated"] = True
        return {"version": VERSION, "match": self.match, "request": self.request, "response": response}

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "Exchange":
        response = payload.get("response") if isinstance(payload.get("response"), Mapping) else {}
        headers = response.get("headers") or []
        pairs = [(str(k), str(v)) for k, v in headers if isinstance(k, str)] if isinstance(headers, list) else []
        chunks = response.get("chunks")
        return cls(
            match=str(payload.get("match") or ""),
            request=dict(payload.get("request") or {}),
            status=int(response.get("status") or 0),
            headers=pairs,
            body=str(response.get("body") or ""),
            chunks=[str(c) for c in chunks] if isinstance(chunks, list) else None,
            elapsed_ms=int(response.get("elapsed_ms") or 0),
            truncated=bool(response.get("truncated")),
        )


class Store:
    """A directory of cassette files. Replay walks them in name order."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._used: dict[str, int] = {}
        self._loaded = False
        self._by_match: dict[str, list[Exchange]] = {}

    def save(self, exchange: Exchange) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        name = f"{exchange.match[:16]}-{time.time_ns()}.json"
        path = self.directory / name
        text = json.dumps(exchange.to_json(), ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        with self._lock:
            self._by_match.setdefault(exchange.match, []).append(exchange)
            self._loaded = True
        return path

    def save_call(
        self,
        method: str,
        path: str,
        body: bytes,
        status: int,
        headers: Mapping[str, str] | None,
        response_body: bytes,
        elapsed_ms: int,
    ) -> Path:
        raw_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        content_type = raw_headers.get("content-type", "")
        truncated = len(response_body) > MAX_BODY
        kept = response_body[:MAX_BODY]
        text = redact_text(kept.decode("utf-8", "replace"))
        chunks = _chunks_of(text, content_type)
        exchange = Exchange(
            match=match_key(method, path, body),
            request=_request_view(method, path, body),
            status=status,
            headers=_header_items(raw_headers),
            body=text,
            chunks=chunks,
            elapsed_ms=max(0, int(elapsed_ms)),
            truncated=truncated,
        )
        return self.save(exchange)

    def _load(self) -> None:
        if self._loaded:
            return
        found: dict[str, list[Exchange]] = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.rglob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict) or not payload.get("match"):
                    continue
                exchange = Exchange.from_json(payload)
                found.setdefault(exchange.match, []).append(exchange)
        self._by_match = found
        self._loaded = True

    def replay(self, method: str, path: str, body: bytes) -> Exchange | None:
        """The next unused recording for this call, then the last one again."""
        key = match_key(method, path, body)
        with self._lock:
            self._load()
            rows = self._by_match.get(key) or []
            if not rows:
                return None
            index = self._used.get(key, 0)
            self._used[key] = index + 1
            return rows[min(index, len(rows) - 1)]


def secret_problems(directory: Path) -> list[str]:
    """Paths under `directory` that contain a key-shaped string or an auth header.

    A cassette that fails this must not be committed. The check looks at the
    file text, so a key hiding in a fingerprint, a header, or a chunk is seen.
    """
    problems: list[str] = []
    if not directory.is_dir():
        return problems
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            problems.append(f"{path}: {type(exc).__name__}")
            continue
        if looks_secret(text) or _SECRET_PREFIXES.search(text):
            problems.append(f"{path}: key-shaped text")
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        for name, _value in _walk_headers(payload):
            if name.strip().lower() in DROPPED_HEADERS:
                problems.append(f"{path}: header {name}")
                break
    return problems


def _walk_headers(payload: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        headers = payload.get("headers")
        if isinstance(headers, list):
            for item in headers:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    found.append((str(item[0]), str(item[1])))
                elif isinstance(item, dict) and "name" in item:
                    found.append((str(item["name"]), str(item.get("value", ""))))
        elif isinstance(headers, dict):
            found.extend((str(k), str(v)) for k, v in headers.items())
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found.extend(_walk_headers(value))
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, (dict, list)):
                found.extend(_walk_headers(value))
    return found


def upstream_url(base: str, path: str) -> str:
    """Join the provider base (`.../v1`) with the path the proxy received."""
    base = base.rstrip("/")
    path = path.split("?", 1)[0]
    if path.startswith("/v1/") and base.endswith("/v1"):
        return base + path[len("/v1"):]
    if path.startswith("/"):
        return base + path
    return base + "/" + path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:  # noqa: ARG002
        return None


def _forward(
    url: str,
    method: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body if method == "POST" else None, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as err:
        try:
            data = err.read(MAX_BODY + 1)
        except OSError:
            data = b""
        return err.code, {k.lower(): v for k, v in err.headers.items()}, data


@dataclass
class Proxy:
    """OpenAI-shaped HTTP server. `record` forwards; `replay` never connects out."""

    mode: str
    directory: Path
    timeout: float = 120.0
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _store: Store = field(init=False, repr=False)
    outbound: int = field(default=0, init=False)  # forwarded calls; replay leaves this at 0

    def __post_init__(self) -> None:
        if self.mode not in ("record", "replay"):
            raise CassetteError(f"cassette proxy mode must be record or replay, not {self.mode!r}")
        self._store = Store(self.directory)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def origin(self) -> str:
        """`http://127.0.0.1:<port>` with no path."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def v1(self) -> str:
        """Base URL LiteLLM should use (`.../v1`)."""
        return self.origin + "/v1"

    def start(self) -> "Proxy":
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self._reply(200, {"content-type": "application/json"}, b'{"object":"list","data":[]}')

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                if proxy.mode == "replay":
                    exchange = proxy._store.replay("POST", self.path, body)
                    if exchange is None:
                        message = {"error": {"message": "no cassette matches this request", "type": "cassette_miss"}}
                        self._reply(404, {"content-type": "application/json"}, json.dumps(message).encode())
                        return
                    payload = exchange.body.encode("utf-8")
                    headers = {k: v for k, v in exchange.headers}
                    headers.setdefault("content-type", "application/json")
                    self._reply(exchange.status or 200, headers, payload)
                    return
                upstream = self.headers.get("x-muse-cassette-upstream", "").strip()
                if not upstream.startswith(("http://", "https://")):
                    message = {"error": {"message": "cassette record mode has no upstream URL", "type": "cassette_record"}}
                    self._reply(502, {"content-type": "application/json"}, json.dumps(message).encode())
                    return
                forward_headers = {}
                for key, value in self.headers.items():
                    name = key.lower()
                    if name in ("host", "content-length", "x-muse-cassette-upstream", "connection"):
                        continue
                    forward_headers[name] = value
                started = time.perf_counter()
                proxy.outbound += 1
                status, resp_headers, resp_body = _forward(
                    upstream_url(upstream, self.path), "POST", forward_headers, body, proxy.timeout,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                proxy._store.save_call("POST", self.path, body, status, resp_headers, resp_body, elapsed)
                safe = {k: v for k, v in resp_headers.items() if k not in SKIP_RESPONSE_HEADERS}
                safe.setdefault("content-type", "application/json")
                self._reply(status, safe, resp_body[:MAX_BODY])

            def _reply(self, status: int, headers: Mapping[str, str], payload: bytes) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() in ("content-length", "transfer-encoding"):
                        continue
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


_singleton: Proxy | None = None
_singleton_key: tuple[str, str] | None = None
_singleton_lock = threading.Lock()


def ensure_proxy(mode: str, directory: Path) -> Proxy:
    """One local proxy for this process. A different mode or directory replaces it."""
    global _singleton, _singleton_key
    key = (mode, str(Path(directory)))
    with _singleton_lock:
        if _singleton is not None and _singleton_key == key and _singleton._server is not None:
            return _singleton
        if _singleton is not None:
            _singleton.stop()
        proxy = Proxy(mode, directory).start()
        _singleton = proxy
        _singleton_key = key
        return proxy


def stop_proxy() -> None:
    """Stop the process-wide proxy, if `reroute` started one."""
    global _singleton, _singleton_key
    with _singleton_lock:
        if _singleton is not None:
            _singleton.stop()
        _singleton = None
        _singleton_key = None


def _api_base(kwargs: Mapping[str, Any]) -> str:
    direct = kwargs.get("api_base")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    params = kwargs.get("litellm_params")
    if isinstance(params, Mapping):
        nested = params.get("api_base")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def reroute(kwargs: dict[str, Any], env: Mapping[str, str] | None) -> bool:
    """Point this deployment call at the cassette proxy.

    False in `live` mode, and when the base URL is not an http(s) provider
    (a local `os.environ/...` reference stays as it is). True when `kwargs`
    was changed and must be returned to LiteLLM. Raises `CassetteError` if
    replay was asked for and the proxy did not start: the call must not go
    on to a real provider.
    """
    mode = mode_of(env)
    if mode == "live":
        return False
    original = _api_base(kwargs)
    if original and not original.startswith(("http://", "https://")):
        return False
    if mode == "record" and not original.startswith(("http://", "https://")):
        return False
    directory = directory_of(env)
    if directory is None:
        raise CassetteError("MUSE_CASSETTE_DIR is not set")
    try:
        proxy = ensure_proxy(mode, directory)
    except OSError as exc:
        raise CassetteError("the cassette proxy did not start") from exc
    headers = kwargs.get("extra_headers")
    if not isinstance(headers, dict):
        headers = {}
        kwargs["extra_headers"] = headers
    if original:
        headers["x-muse-cassette-upstream"] = original
    kwargs["api_base"] = proxy.v1
    params = kwargs.get("litellm_params")
    if isinstance(params, dict):
        params["api_base"] = proxy.v1
    return True
