"""A tiny client for the home control API. Standard library only.

Used by `muse home call` and the tests; also the smallest honest example of
how an app talks to the home:

    client = HomeClient(token=token)                       # MUSE_HOME/home.sock
    status, payload = client.call("GET", "/v1/status")
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any, Mapping

from muse.home import server as _server


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class HomeClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        socket_path: Path | str | None = None,
        tcp_port: int | None = None,
        environ: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.tcp_port = tcp_port
        if tcp_port is None and not _server.unix_sockets_available():
            self.tcp_port = _server.DEFAULT_TCP_PORT
        self.socket_path = None if self.tcp_port is not None else str(socket_path or _server.default_socket_path(environ))

    def _connection(self) -> http.client.HTTPConnection:
        if self.tcp_port is not None:
            return http.client.HTTPConnection("127.0.0.1", self.tcp_port, timeout=self.timeout)
        return _UnixConnection(str(self.socket_path), self.timeout)

    def request(self, method: str, path: str, body: Any = None, headers: Mapping[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
        """(status, parsed JSON or None, response headers). Raises OSError when the home is not there."""
        send = dict(headers or {})
        if self.token and "Authorization" not in send:
            send["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            send.setdefault("Content-Type", "application/json")
        conn = self._connection()
        try:
            conn.request(method, path, body=data, headers=send)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                payload = None
            return resp.status, payload, {k.lower(): v for k, v in resp.getheaders()}
        finally:
            conn.close()

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        status, payload, _ = self.request(method, path, body)
        return status, payload
