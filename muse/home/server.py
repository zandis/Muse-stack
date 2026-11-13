"""The transport for the home control API. Standard library only.

Where it listens:
  * Linux and macOS: a Unix domain socket, `MUSE_HOME/home.sock`, mode 0600
    inside a 0700 directory. Only the same user can open it, and the server
    also asks the kernel who is on the other end (SO_PEERCRED on Linux,
    LOCAL_PEERCRED on macOS and FreeBSD) and refuses any other uid. Where the
    platform does not say, that check is skipped and a note is logged once.
  * Windows, or `--tcp PORT`: 127.0.0.1 only. There is no peer uid on TCP, so
    the device token is the whole defence; the Host header is checked against
    DNS rebinding, as the dashboard server does.

Safety, the same measures as muse/dashboard/server.py plus what writes need:
loopback or a private socket only; every request needs a device token
(muse/home/auth.py) and is refused the same way whether the token is missing or
wrong; no CORS headers are ever sent, so a web page cannot read a reply; bodies
are bounded (muse.home.api.MAX_BODY_BYTES) and must declare their length; each
connection has a read timeout and serves one request; paths are matched against
a fixed route table, never against the file system; nothing from a request
(path, query, body, headers) is written to a log.

Two limits against abuse, both in memory and both small:
  * Wrong tokens. Each peer (the uid on the Unix socket, the address on TCP)
    may fail to sign in AUTH_FAIL_LIMIT times in AUTH_FAIL_WINDOW_S seconds;
    after that its failed requests get 429 `too_many_requests` with a
    Retry-After instead of 401, until the window has passed. A request with a
    correct token is always served: device tokens are 256 random bits, so the
    limit is there to bound noise and work, and it must not let one program
    lock the person's own app out by sending rubbish.
  * Connections. At most MAX_CONNECTIONS are served at once; one more is
    answered 503 `unavailable` with a Retry-After at once and closed.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Hashable, Mapping

import json

from muse import paths
from muse.home import api
from muse.home.auth import TokenFileVerifier, Verifier

log = logging.getLogger("muse.home")

DEFAULT_TCP_PORT = 4181
SOCKET_NAME = "home.sock"
REQUEST_TIMEOUT_S = 15.0
MAX_SOCKET_PATH_BYTES = 100  # sun_path is 104 bytes on macOS, 108 on Linux
MAX_CONNECTIONS = 64  # served at once; the next one gets 503 at once
AUTH_FAIL_LIMIT = 10  # wrong tokens a peer may send ...
AUTH_FAIL_WINDOW_S = 60.0  # ... in this many seconds, before its failures get 429
MAX_TRACKED_PEERS = 256

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def unix_sockets_available() -> bool:
    return hasattr(socket, "AF_UNIX") and os.name != "nt"


def default_socket_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / SOCKET_NAME


def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process on the other end of a Unix socket, or None where the platform does not say."""
    try:
        if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        if sys.platform == "darwin" or sys.platform.startswith("freebsd"):
            # getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED=1) -> struct xucred {u_int version; uid_t uid; short ngroups; gid_t groups[16]}
            raw = sock.getsockopt(0, 0x001, struct.calcsize("2Ih16I"))
            _version, uid = struct.unpack_from("2I", raw)
            return int(uid)
    except (OSError, struct.error, AttributeError):
        return None
    return None


class FailureLimiter:
    """Counts failed sign-ins per peer in a sliding window. In memory only; forgotten when the server stops."""

    def __init__(self, limit: int = AUTH_FAIL_LIMIT, window_s: float = AUTH_FAIL_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic, max_peers: int = MAX_TRACKED_PEERS) -> None:
        self.limit, self.window_s, self.clock, self.max_peers = int(limit), float(window_s), clock, int(max_peers)
        self._lock = threading.Lock()
        self._fails: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def fail(self, peer: Hashable) -> int | None:
        """Note one failed sign-in. None while the peer is within its limit; otherwise the seconds to wait."""
        now = self.clock()
        with self._lock:
            times = self._fails.pop(peer, None) or deque()
            while times and times[0] <= now - self.window_s:
                times.popleft()
            over = len(times) >= self.limit
            if not over:
                times.append(now)  # failures beyond the limit are not added, so the wait cannot be stretched for ever
            self._fails[peer] = times  # most recently seen last
            while len(self._fails) > self.max_peers:
                self._fails.popitem(last=False)
            if not over:
                return None
            return max(1, int(times[0] + self.window_s - now + 0.999))


class _Handler(BaseHTTPRequestHandler):
    # A request line too broken to parse is treated as HTTP/0.9 by default, which on some
    # Python versions means the reply has no status line and no headers. Always answer
    # with a full response.
    default_request_version = "HTTP/1.0"
    server_version = "MuseHome"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # the request line holds the path and query: never logged

    def address_string(self) -> str:  # a Unix socket has no client address
        return "local"

    # -- writing ---------------------------------------------------------
    def _send(self, resp: api.Response) -> None:
        body = json.dumps(resp.payload, ensure_ascii=False).encode("utf-8")
        self.send_response(resp.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in BASE_HEADERS.items():
            self.send_header(k, v)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _fail(self, err: api.ApiError, who: str = "-") -> None:
        log.info("%s %s -> %d %s", self.command, "-", err.status, who)
        self._send(api.error_response(err))

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """http.server calls this for a request it cannot parse. Answer in the API's one error shape."""
        if code == 501:
            err = api.ApiError("method_not_allowed", "That call does not take this method.")
        else:
            err = api.ApiError("invalid_request", "The request could not be read.")
        if not getattr(self, "command", None):
            self.command = "-"
        try:
            self._send(api.error_response(err))
        except OSError:
            pass

    # -- reading ---------------------------------------------------------
    def _declared_length(self) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise api.ApiError("length_required", "Send the body with a Content-Length.")
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        if not raw.strip().isdigit() or len(raw.strip()) > 12:
            raise api.ApiError("invalid_request", "The request could not be read.")
        return int(raw.strip())

    def _handle(self) -> None:
        server: HomeServer = self.server  # type: ignore[assignment]
        try:
            if not server.host_ok(self.headers.get("Host")):
                raise api.ApiError("forbidden", "This address is not the home. Use the address the home printed when it started.")
            if not server.peer_ok(self.connection):
                raise api.ApiError("forbidden", "Only the person who runs the home can use this socket.")
            length = self._declared_length()
        except api.ApiError as err:
            self._fail(err)
            return
        try:
            device = api.authenticate(server.verifier, self.headers.get("Authorization"))
        except api.ApiError as err:
            if 0 < length <= api.MAX_BODY_BYTES:
                self.rfile.read(length)  # let a small body finish so the client can read the 401
            wait = server.auth_failures.fail(server.peer_key(self.connection, self.client_address))
            if wait is not None:
                err = api.ApiError("too_many_requests", "Too many requests with a wrong device token. Wait, then sign in "
                                                        "with a token from `muse home token add`.", retry_after=wait)
            self._fail(err)
            return
        if length > api.MAX_BODY_BYTES:
            self._fail(api.ApiError("payload_too_large", "The request body is too large."), device.name)
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._fail(api.ApiError("invalid_request", "The request could not be read."), device.name)
            return
        resp = api.dispatch(server.home, device, self.command, self.path, body)
        log.info("%s %s -> %d %s", self.command, resp.route, resp.status, device.name)
        self._send(resp)

    do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = do_HEAD = do_OPTIONS = _handle


class HomeServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Shared behaviour; see UnixHomeServer and TcpHomeServer."""

    daemon_threads = True
    request_queue_size = 32
    is_tcp = False

    home: api.Home
    verifier: Verifier
    max_connections = MAX_CONNECTIONS

    def _init_limits(self) -> None:
        self.auth_failures = FailureLimiter()
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def host_ok(self, host: str | None) -> bool:
        return True

    def peer_ok(self, conn: socket.socket) -> bool:
        return True

    def peer_key(self, conn: socket.socket, client_address: Any) -> Hashable:
        """Whom failed sign-ins are counted against."""
        return "local"

    # -- the connection cap ----------------------------------------------
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self._refuse_busy(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)  # starts the thread that runs process_request_thread
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    @staticmethod
    def _refuse_busy(request: Any) -> None:
        """Answer 503 without reading the request or starting a thread. Best effort: the client may already be gone."""
        err = api.ApiError("unavailable", "The home is busy with other requests. Try again in a moment.", retry_after=1)
        body = json.dumps(err.payload()).encode("utf-8")
        head = (f"HTTP/1.1 {err.status} Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nRetry-After: 1\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n")
        try:
            request.settimeout(1.0)
            request.sendall(head.encode("ascii") + body)
            # Say we are done, then take what the client had already sent, so that closing does not reset the
            # connection under the reply. A very short wait: this runs on the accepting thread.
            request.shutdown(socket.SHUT_WR)
            request.settimeout(0.05)
            request.recv(65536)
        except OSError:
            pass

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return  # a client that went away
        log.error("request handling failed (%s)", type(exc).__name__)  # no traceback: it could quote a request

    @property
    def where(self) -> str:
        raise NotImplementedError


class UnixHomeServer(HomeServer):
    address_family = getattr(socket, "AF_UNIX", socket.AF_INET)

    def __init__(self, path: Path, home: api.Home, verifier: Verifier, *, check_peer: bool = True) -> None:
        self.home, self.verifier = home, verifier
        self.socket_path = Path(path)
        self.check_peer = check_peer
        self._uid = os.getuid()
        self._noted = False
        self._inode: int | None = None
        self._init_limits()
        super().__init__(str(self.socket_path), _Handler)

    def server_bind(self) -> None:
        path = self.socket_path
        if len(os.fsencode(str(path))) > MAX_SOCKET_PATH_BYTES:
            raise ValueError(f"the socket path is too long ({path}); set MUSE_HOME to a shorter folder, or use --tcp")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            log.warning("home: the folder holding the socket is open to other users; the socket itself is still 0600")
        if path.is_symlink() or path.exists():
            if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
                raise ValueError(f"{path} exists and is not a socket; remove it or choose another path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink(missing_ok=True)  # left behind by a server that is gone
            except OSError as exc:
                if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
                    raise ValueError(f"cannot tell whether a home is already running on {path}") from None
                path.unlink(missing_ok=True)
            else:
                raise ValueError(f"a home is already running on {path}")
            finally:
                probe.close()
        old = os.umask(0o177)  # the socket file is 0600 from the instant it exists
        try:
            self.socket.bind(str(path))
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self._inode = path.stat().st_ino
        self.server_address = str(path)

    def server_close(self) -> None:
        super().server_close()
        try:
            if self._inode is not None and self.socket_path.stat().st_ino == self._inode:
                self.socket_path.unlink()
        except OSError:
            pass

    def peer_ok(self, conn: socket.socket) -> bool:
        if not self.check_peer:
            return True
        uid = peer_uid(conn)
        if uid is None:
            if not self._noted:
                self._noted = True
                log.warning("home: this platform does not report who is on the other end of the socket; "
                            "relying on the socket's 0600 mode and the device token")
            return True
        return uid == self._uid

    def peer_key(self, conn: socket.socket, client_address: Any) -> Hashable:
        uid = peer_uid(conn)
        return "local" if uid is None else ("uid", uid)

    @property
    def where(self) -> str:
        return f"unix:{self.socket_path}"


class TcpHomeServer(HomeServer):
    allow_reuse_address = True
    is_tcp = True

    def __init__(self, port: int, home: api.Home, verifier: Verifier, host: str = "127.0.0.1") -> None:
        if host != "127.0.0.1":
            raise ValueError("the home control API only listens on 127.0.0.1")
        self.home, self.verifier = home, verifier
        self._init_limits()
        super().__init__((host, port), _Handler)
        bound = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{bound}", f"localhost:{bound}"}

    def host_ok(self, host: str | None) -> bool:
        return (host or "").strip().lower() in self.allowed_hosts

    def peer_key(self, conn: socket.socket, client_address: Any) -> Hashable:
        return ("ip", client_address[0]) if isinstance(client_address, tuple) and client_address else "local"

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def where(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def make_server(
    home: api.Home | None = None,
    verifier: Verifier | None = None,
    *,
    socket_path: Path | str | None = None,
    tcp_port: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> HomeServer:
    """Build (but do not start) the server.

    `tcp_port` given (0 picks a free port), or no Unix sockets on this platform:
    127.0.0.1. Otherwise a Unix socket at `socket_path` or MUSE_HOME/home.sock.
    """
    home = home if home is not None else api.Home(environ)
    verifier = verifier if verifier is not None else TokenFileVerifier(environ)
    if tcp_port is None and not unix_sockets_available():
        tcp_port = DEFAULT_TCP_PORT
    if tcp_port is not None:
        return TcpHomeServer(int(tcp_port), home, verifier)
    return UnixHomeServer(Path(socket_path) if socket_path else default_socket_path(environ), home, verifier)


def serve_in_thread(server: HomeServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return thread


def _stop_request(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def serve(server: HomeServer, banner: str = "Muse home") -> None:
    """Serve until asked to stop, then remove the socket.

    Both an interrupt and a terminate request stop it cleanly. They are set
    here on purpose: a service manager stops a program with SIGTERM, and a
    program started detached (`nohup`, a background job) inherits SIGINT
    ignored, so neither default can be relied on.
    """
    import signal

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, _stop_request)
    print(f"{banner}: {server.where}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
