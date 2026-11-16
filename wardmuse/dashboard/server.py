"""Read-only dashboard server. Standard library only.

Routes: GET / (the page), GET /api/status (snapshot JSON), GET /healthz.
There are no write routes. The page shows which AI services a person uses, so
the server binds loopback, checks the Host header against DNS rebinding, and
sends a strict Content-Security-Policy.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import ipaddress
import json
import os
import re
import socket
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
INDEX_PATH = HERE / "index.html"
DEMO_PATH = HERE / "demo_snapshot.json"

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

SnapshotFn = Callable[[], dict]

_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _default_snapshot() -> dict:
    from wardmuse.status import build_snapshot  # written elsewhere; import lazily

    return build_snapshot()


def _shift_times(obj: Any, delta: float) -> Any:
    if isinstance(obj, str) and _TS_RE.match(obj):
        t = datetime.strptime(obj, _TS_FMT).replace(tzinfo=timezone.utc)
        return datetime.fromtimestamp(t.timestamp() + delta, timezone.utc).strftime(_TS_FMT)
    if isinstance(obj, list):
        return [_shift_times(x, delta) for x in obj]
    if isinstance(obj, dict):
        return {k: _shift_times(v, delta) for k, v in obj.items()}
    return obj


def demo_snapshot(now: datetime | None = None) -> dict:
    """The bundled example, with every timestamp moved so it looks current."""
    data = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
    now = now or datetime.now(timezone.utc)
    base = datetime.strptime(data["generated_at"], _TS_FMT).replace(tzinfo=timezone.utc)
    return _shift_times(data, now.timestamp() - base.timestamp())


class _Handler(BaseHTTPRequestHandler):
    # See muse/home/server.py: always answer a broken request line with a full response.
    default_request_version = "HTTP/1.0"
    server_version = "MuseDash"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # quiet: paths and query strings are never logged

    # -- helpers ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        if getattr(self.server, "remote_ok", False):
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.server.allowed_hosts

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"error": "forbidden host", "hint": "Open the dashboard at http://127.0.0.1:<port>/."})
            return
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/":
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self._json(500, {"error": "index.html missing", "hint": "Reinstall muse."})
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._json(200, {"ok": True})
        elif path == "/api/status":
            self._status(parse_qs(parts.query))
        else:
            self._json(404, {"error": "not found"})

    def _status(self, query: dict) -> None:
        try:
            if query.get("demo") == ["1"]:
                data = demo_snapshot()
            else:
                data = self.server.snapshot_fn()
            body = json.dumps(data).encode("utf-8")
        except ImportError:
            self._json(503, {
                "error": "status unavailable",
                "hint": "The status module (muse.status) could not be loaded. Check that muse is installed correctly.",
            })
            return
        except Exception as exc:  # never leak a traceback to the page
            self._json(503, {
                "error": "status unavailable",
                "hint": f"The snapshot could not be built ({type(exc).__name__}). Run `muse status` in a terminal to see why.",
            })
            return
        self._send(200, body, "application/json; charset=utf-8")

    def _no_writes(self) -> None:
        if not self._host_ok():
            self._json(403, {"error": "forbidden host"})
            return
        self._send(405, b'{"error": "read-only"}', "application/json; charset=utf-8", {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no_writes  # type: ignore[assignment]

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], snapshot_fn: SnapshotFn, remote_ok: bool = False):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)
        port = self.server_address[1]
        self.snapshot_fn = snapshot_fn
        self.remote_ok = remote_ok
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def make_server(host: str = "127.0.0.1", port: int = 4180, snapshot_fn: SnapshotFn | None = None) -> DashboardServer:
    """Build (but do not start) the server. port=0 picks a free port."""
    remote = False
    if not is_loopback(host):
        if envget(os.environ, "MUSE_DASH_ALLOW_REMOTE") != "1":
            raise ValueError(
                f"refusing to bind {host!r}: the dashboard shows which AI services you use. "
                "Set MUSE_DASH_ALLOW_REMOTE=1 to allow a non-loopback address."
            )
        print(
            f"WARNING: dashboard bound to {host}; anyone who can reach it can see your AI usage. "
            "Host-header checks are off.",
            file=sys.stderr,
        )
        remote = True
    return DashboardServer((host, port), snapshot_fn or _default_snapshot, remote_ok=remote)


def serve(host: str = "127.0.0.1", port: int = 4180, open_browser: bool = False,
          snapshot_fn: SnapshotFn | None = None) -> None:
    try:
        srv = make_server(host, port, snapshot_fn)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{srv.server_address[1]}/"
    print(f"Muse dashboard: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
