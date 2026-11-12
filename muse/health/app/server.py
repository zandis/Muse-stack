"""Web server for the health assistant. Standard library only.

This server has WRITE endpoints and handles the vault passphrase, so it is
deliberately strict: loopback bind, Host allow-list, one cookie session with a
CSRF token, Origin / Sec-Fetch-Site checks, size and time limits on every body,
unlock back-off, strict CSP, and error bodies that never carry exception text,
paths or record content.

Session design
  * POST /api/unlock (or /api/setup) creates ONE session: a random 256-bit id in
    an HttpOnly, SameSite=Strict cookie (Secure when not loopback) plus a random
    256-bit CSRF token returned in the JSON body. Only a hash of the id is kept.
  * Every state-changing request needs: valid cookie, matching X-CSRF-Token,
    Content-Type: application/json, and a same-origin Origin / Sec-Fetch-Site.
  * One session per vault. A new successful unlock replaces the old one.
  * Idle time is enforced here (default 10 minutes), not just in the page.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from muse.health.app.services import APP_VERSION, NotSupported, Runtime, Services
from muse.health.contracts import BotReply, Item, Mandate, Receipt, Record, VaultError, VaultLocked

HERE = Path(__file__).resolve().parent
INDEX_PATH = HERE / "index.html"

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
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

COOKIE = "muse_sid"
MAX_BODY = 64 * 1024
MAX_IMPORT_BODY = 2_500_000
BODY_TIMEOUT = 15.0
MIN_PASSPHRASE = 8
MAX_PASSPHRASE = 256
DEFAULT_IDLE = 600
IDLE_CHOICES = (1, 5, 10, 15, 30, 60)

RECORD_KINDS = ("lab", "report", "imaging_report", "pathology_report", "medication_list",
                "visit", "note", "document", "measurement")
ITEM_KINDS = ("medication", "appointment", "reminder", "question_list")
IMPORT_EXTS = ("txt", "md", "pdf", "json", "csv")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")

# Safe, fixed messages. The page maps `error` codes to its own wording.
MESSAGES = {
    "forbidden_host": "This address is not allowed.",
    "cross_site": "Cross-site requests are not allowed.",
    "locked": "Unlock first.",
    "csrf": "The request was not accepted.",
    "unsupported_media_type": "Send JSON.",
    "too_large": "The request is too large.",
    "length_required": "A Content-Length is required.",
    "bad_request": "The request could not be understood.",
    "bad_json": "The request could not be understood.",
    "invalid": "Some of the information is not valid.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
    "unlock_failed": "Could not unlock.",
    "rate_limited": "Too many attempts. Wait and try again.",
    "vault_exists": "A vault already exists.",
    "no_vault": "There is no vault yet.",
    "consent_required": "The current consent version must be accepted.",
    "weak_passphrase": "The passphrase is too short.",
    "not_supported": "This is not supported by this installation.",
    "not_allowed": "The assistant is not allowed to do that.",
    "import_failed": "This file could not be read.",
    "internal": "Something went wrong.",
    "timeout": "The request timed out.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, headers: dict[str, str] | None = None, extra: dict | None = None):
        super().__init__(code)
        self.status, self.code, self.headers, self.extra = status, code, headers or {}, extra or {}


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Unlock back-off
# --------------------------------------------------------------------------
class UnlockGuard:
    """Per-server limiter. The first two wrong tries are free (typos); from the
    third on each failure doubles the wait (1, 2, 4 ... capped). Independently,
    at most `per_minute` attempts are accepted in any 60 seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, base: float = 1.0,
                 cap: float = 300.0, per_minute: int = 10):
        self.clock, self.base, self.cap, self.per_minute = clock, base, cap, per_minute
        self.fails = 0
        self.blocked_until = 0.0
        self.attempts: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> float:
        """Seconds to wait (0 = go ahead). A go-ahead counts as an attempt."""
        with self._lock:
            now = self.clock()
            while self.attempts and now - self.attempts[0] >= 60:
                self.attempts.popleft()
            if self.blocked_until > now:
                return self.blocked_until - now
            if len(self.attempts) >= self.per_minute:
                return 60 - (now - self.attempts[0])
            self.attempts.append(now)
            return 0.0

    def fail(self) -> None:
        with self._lock:
            self.fails += 1
            if self.fails >= 3:
                self.blocked_until = self.clock() + min(self.cap, self.base * 2 ** (self.fails - 3))

    def success(self) -> None:
        with self._lock:
            self.fails = 0
            self.blocked_until = 0.0


@dataclasses.dataclass
class Session:
    sid_hash: str
    csrf: str
    runtime: Runtime
    last_seen: float
    idle: int


# --------------------------------------------------------------------------
# Serialisation. Receipts and records are cut down to what the page shows.
# --------------------------------------------------------------------------
def rec_json(r: Record) -> dict:
    return {"id": r.id, "kind": r.kind, "title": r.title, "occurred_on": r.occurred_on,
            "source": r.source, "added_at": r.added_at, "size": r.size}


def item_json(i: Item) -> dict:
    return {"id": i.id, "kind": i.kind, "data": i.data, "created_at": i.created_at, "updated_at": i.updated_at}


def mandate_json(m: Mandate, now: str) -> dict:
    active = m.revoked_at is None and (m.expires_at is None or m.expires_at > now)
    return {"id": m.id, "grantee": m.grantee, "scopes": list(m.scopes), "purpose": m.purpose,
            "record_kinds": list(m.record_kinds), "created_at": m.created_at, "expires_at": m.expires_at,
            "revoked_at": m.revoked_at, "active": active}


def receipt_json(r: Receipt) -> dict:
    return {"seq": r.seq, "ts": r.ts, "actor": r.actor, "action": r.action, "mandate_id": r.mandate_id,
            "record_count": len(r.record_refs), "egress": r.egress, "model": r.model,
            "privacy": r.privacy, "detail": r.detail}


# --------------------------------------------------------------------------
# Validation. Strict types; unknown fields are ignored.
# --------------------------------------------------------------------------
def _str(body: dict, key: str, lo: int = 0, hi: int = 1000, required: bool = True, strip: bool = True) -> str | None:
    v = body.get(key)
    if v is None:
        if required:
            raise ApiError(400, "invalid")
        return None
    if not isinstance(v, str):
        raise ApiError(400, "invalid")
    if strip:
        v = v.strip()
    if not (lo <= len(v) <= hi):
        raise ApiError(400, "invalid")
    return v


def _date(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", v):
        raise ApiError(400, "invalid")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ApiError(400, "invalid") from None
    return v


def _item_data(v: Any) -> dict:
    if not isinstance(v, dict) or not v or len(v) > 20:
        raise ApiError(400, "invalid")
    out: dict[str, Any] = {}
    for k, x in v.items():
        if not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k):
            raise ApiError(400, "invalid")
        if isinstance(x, str):
            if len(x) > 2000:
                raise ApiError(400, "invalid")
            out[k] = x.strip()
        elif isinstance(x, bool) or (isinstance(x, (int, float)) and abs(x) < 1e12):
            out[k] = x
        elif isinstance(x, list) and len(x) <= 50 and all(isinstance(s, str) and len(s) <= 500 for s in x):
            out[k] = [s.strip() for s in x]
        else:
            raise ApiError(400, "invalid")
    return out


def _qint(q: dict, key: str, default: int, lo: int, hi: int) -> int:
    vals = q.get(key)
    if not vals:
        return default
    try:
        n = int(vals[0])
    except ValueError:
        raise ApiError(400, "invalid") from None
    if not (lo <= n <= hi):
        raise ApiError(400, "invalid")
    return n


def _id(s: str) -> str:
    if not ID_RE.match(s):
        raise ApiError(404, "not_found")
    return s


def _excerpt(text: str, n: int = 240) -> str:
    t = " ".join(text.split())
    return t[:n] + ("…" if len(t) > n else "")


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------
# (method, regex, handler name, needs_session, csrf_for_get)
ROUTES: list[tuple[str, str, str, bool]] = [
    ("GET", r"/", "page", False),
    ("GET", r"/index\.html", "page", False),
    ("GET", r"/healthz", "healthz", False),
    ("GET", r"/api/state", "state", False),
    ("POST", r"/api/setup", "setup", False),
    ("POST", r"/api/unlock", "unlock", False),
    ("POST", r"/api/lock", "lock", True),
    ("POST", r"/api/session/touch", "touch", True),
    ("POST", r"/api/session/idle", "set_idle", True),
    ("GET", r"/api/records", "records_list", True),
    ("POST", r"/api/records", "records_add", True),
    ("GET", r"/api/records/([^/]+)", "records_get", True),
    ("DELETE", r"/api/records/([^/]+)", "records_delete", True),
    ("POST", r"/api/import", "import_", True),
    ("GET", r"/api/items", "items_list", True),
    ("POST", r"/api/items", "items_add", True),
    ("PATCH", r"/api/items/([^/]+)", "items_patch", True),
    ("DELETE", r"/api/items/([^/]+)", "items_delete", True),
    ("POST", r"/api/chat", "chat", True),
    ("POST", r"/api/changes/([^/]+)/(confirm|reject)", "change", True),
    ("GET", r"/api/mandates", "mandates_list", True),
    ("POST", r"/api/mandates/grant-default", "mandates_default", True),
    ("POST", r"/api/mandates/([^/]+)/revoke", "mandates_revoke", True),
    ("GET", r"/api/receipts", "receipts_list", True),
    ("GET", r"/api/receipts/verify", "receipts_verify", True),
    ("GET", r"/api/export", "export", True),
    ("POST", r"/api/passphrase", "passphrase", True),
    ("POST", r"/api/vault/delete", "vault_delete", True),
]
_COMPILED = [(m, re.compile(rx), fn, auth) for m, rx, fn, auth in ROUTES]
SENSITIVE_GET = {"export"}  # GETs that also demand the CSRF header


class _Handler(BaseHTTPRequestHandler):
    server_version = "MuseHealth"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = BODY_TIMEOUT  # socket timeout for every read (slow-loris)

    # -- logging: method, route template, status. No query, no body. -----
    _log_path = "-"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        if getattr(self.server, "quiet", False):
            return
        sys.stderr.write(f"muse-app {self.command or '-'} {self._log_path} {code}\n")

    # -- low-level response ----------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in extra or []:
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, code: int, payload: Any, extra: list[tuple[str, str]] | None = None) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _fail(self, e: ApiError, cookies: list[tuple[str, str]] | None = None) -> None:
        body = {"error": e.code, "message": MESSAGES.get(e.code, MESSAGES["internal"])}
        body.update(e.extra)
        self._drain()
        self._json(e.status, body, list(e.headers.items()) + (cookies or []))

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:  # noqa: D401
        """http.server calls this for malformed requests; keep the house format."""
        self._json(code, {"error": "bad_request", "message": MESSAGES["bad_request"]})

    def _drain(self) -> None:
        """Best effort: swallow a small unread body so the client sees our reply, not a reset."""
        if getattr(self, "_body_read", False):
            return
        self._body_read = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        deadline = time.monotonic() + 2.0
        left = min(max(n, 0), 1 << 20)
        try:
            while left > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    # -- checks -------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = self.server.allowed_hosts
        if allowed is None:  # remote mode without an explicit list
            return bool(host)
        return host in allowed

    def _check_origin(self) -> None:
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            raise ApiError(403, "cross_site")
        origin = self.headers.get("Origin")
        if origin is not None:
            parts = urlsplit(origin)
            host = (self.headers.get("Host") or "").strip().lower()
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != host:
                raise ApiError(403, "cross_site")

    def _session(self, touch: bool = True) -> Session | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
            morsel = jar.get(COOKIE)
        except Exception:
            return None
        if morsel is None:
            return None
        return self.server.find_session(morsel.value, touch)

    def _read_body(self, limit: int) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "unsupported_media_type")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "length_required")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise ApiError(411, "length_required")
        try:
            n = int(raw_len)
        except ValueError:
            raise ApiError(400, "bad_request") from None
        if n < 0:
            raise ApiError(400, "bad_request")
        if n > limit:
            raise ApiError(413, "too_large")
        chunks, got, deadline = [], 0, time.monotonic() + BODY_TIMEOUT
        try:
            while got < n:
                if time.monotonic() > deadline:
                    raise ApiError(408, "timeout")
                self.connection.settimeout(max(0.05, deadline - time.monotonic()))
                chunk = self.rfile.read(min(65536, n - got))
                if not chunk:
                    raise ApiError(400, "bad_request")
                chunks.append(chunk)
                got += len(chunk)
        except (socket.timeout, TimeoutError):
            raise ApiError(408, "timeout") from None
        self._body_read = True
        if n == 0:
            return {}
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, RecursionError):
            raise ApiError(400, "bad_json") from None
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json")
        return data

    # -- dispatch -----------------------------------------------------------
    def _handle(self) -> None:
        self._body_read = False
        self._set_cookies: list[tuple[str, str]] = []
        try:
            self._route()
        except ApiError as e:
            self._fail(e, self._set_cookies)
        except VaultLocked:
            self.server.end_session()
            self._fail(ApiError(401, "locked"), [self.server.clear_cookie()])
        except LookupError:
            self._fail(ApiError(404, "not_found"))
        except VaultError as exc:  # messages are content-free by contract; still never echoed
            missing = str(exc).startswith(("no such", "malformed id"))
            self._fail(ApiError(404, "not_found") if missing else ApiError(400, "invalid"))
        except NotSupported:
            self._fail(ApiError(501, "not_supported"))
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as exc:  # never leak text, paths or content
            sys.stderr.write(f"muse-app internal error in {self._log_path}: {type(exc).__name__}\n")
            self._fail(ApiError(500, "internal"))

    def _route(self) -> None:
        method = self.command
        if not self._host_ok():
            raise ApiError(403, "forbidden_host")
        path = urlsplit(self.path).path
        matched_any = False
        for m, rx, fn, needs_session in _COMPILED:
            mt = rx.fullmatch(path)
            if not mt:
                continue
            matched_any = True
            if m != method and not (method == "HEAD" and m == "GET"):
                continue
            self._log_path = rx.pattern.replace("([^/]+)", "{id}").replace("(confirm|reject)", "{act}").replace("\\", "")
            if path.startswith("/api/"):
                self._check_origin()
            changing = method in ("POST", "PATCH", "DELETE")
            sess = None
            if needs_session:
                sess = self._session()
                if sess is None:
                    raise ApiError(401, "locked", extra={}, headers={})
                if changing or fn in SENSITIVE_GET:
                    sent = (self.headers.get("X-CSRF-Token") or "").encode("utf-8", "replace")
                    if not hmac.compare_digest(sent, sess.csrf.encode()):
                        raise ApiError(403, "csrf")
            body: dict = {}
            if changing:
                body = self._read_body(MAX_IMPORT_BODY if fn == "import_" else MAX_BODY)
            getattr(self, "h_" + fn)(sess, body, *mt.groups())
            return
        if matched_any:
            raise ApiError(405, "method_not_allowed", headers={"Allow": "GET, POST, PATCH, DELETE"})
        raise ApiError(404, "not_found")

    def do_GET(self) -> None: self._handle()          # noqa: N802
    def do_POST(self) -> None: self._handle()         # noqa: N802
    def do_PATCH(self) -> None: self._handle()        # noqa: N802
    def do_DELETE(self) -> None: self._handle()       # noqa: N802
    def do_HEAD(self) -> None: self._handle()         # noqa: N802
    def do_PUT(self) -> None: self._handle()          # noqa: N802
    def do_OPTIONS(self) -> None: self._handle()      # noqa: N802

    # -- helpers for handlers ----------------------------------------------
    def _q(self) -> dict:
        return parse_qs(urlsplit(self.path).query, max_num_fields=10)

    def _receipt(self, sess: Session, action: str, **kw: Any) -> None:
        try:
            sess.runtime.receipts.append("user", action, **kw)
        except Exception as exc:
            sys.stderr.write(f"muse-app receipt failed: {type(exc).__name__}\n")

    # -- public pages ---------------------------------------------------------
    def h_page(self, sess: Any, body: dict) -> None:
        try:
            data = INDEX_PATH.read_bytes()
        except OSError:
            raise ApiError(500, "internal") from None
        self._send(200, data, "text/html; charset=utf-8")

    def h_healthz(self, sess: Any, body: dict) -> None:
        self._json(200, {"ok": True})

    def h_state(self, sess: Any, body: dict) -> None:
        srv = self.server
        t = srv.services.tenant
        sess = self._session(touch=False)
        out = {
            "tenant": {"display_name": t.display_name, "issuer": t.issuer, "locale": t.locale,
                       "consent_version": t.consent_version, "consent_text": t.consent_text,
                       "emergency_text": t.emergency_text, "pilot_ref": t.pilot_ref, "contact": t.contact},
            "features": dict(t.features),
            "version": srv.services.version or APP_VERSION,
            "vault_exists": bool(srv.services.vault_exists()),
            "unlocked": sess is not None,
            "limits": {"min_passphrase": MIN_PASSPHRASE, "max_import_bytes": (MAX_IMPORT_BODY * 3 // 4) - 1024,
                       "idle_choices": list(IDLE_CHOICES)},
        }
        if sess is not None:
            out["csrf"] = sess.csrf
            out["idle_seconds"] = sess.idle
            out["idle_remaining"] = max(0, int(sess.idle - (srv.clock() - sess.last_seen)))
        self._json(200, out)

    # -- session lifecycle ---------------------------------------------------
    def _start(self, runtime: Runtime) -> None:
        sid = self.server.new_sid()
        csrf, idle = self.server.begin_session(runtime, sid)
        self._json(200, {"ok": True, "csrf": csrf, "idle_seconds": idle}, [self.server.make_cookie(sid)])

    def h_setup(self, sess: Any, body: dict) -> None:
        srv = self.server
        pw = _str(body, "passphrase", 1, MAX_PASSPHRASE, strip=False)
        cv = _str(body, "consent_version", 0, 100)
        if len(pw) < MIN_PASSPHRASE:
            raise ApiError(400, "weak_passphrase")
        if not hmac.compare_digest(cv.encode(), (srv.services.tenant.consent_version or "").encode()):
            raise ApiError(400, "consent_required")
        with srv.op_lock:
            if srv.services.vault_exists():
                raise ApiError(409, "vault_exists")
            vault = srv.services.create_vault(pw)
            rt = srv.services.make_runtime(vault)
            rt.receipts.append("user", "consent.accept", detail=f"consent {srv.services.tenant.consent_version}")
            m = srv.services.grant_default_mandate(rt.mandates)
            rt.receipts.append("user", "mandate.grant", mandate_id=m.id, detail="default assistant mandate")
        self._start(rt)

    def h_unlock(self, sess: Any, body: dict) -> None:
        srv = self.server
        pw = body.get("passphrase")
        if not isinstance(pw, str) or not (1 <= len(pw) <= MAX_PASSPHRASE):
            raise ApiError(400, "invalid")
        wait = srv.guard.check()
        if wait > 0:
            n = max(1, int(wait + 0.999))
            raise ApiError(429, "rate_limited", headers={"Retry-After": str(n)}, extra={"retry_after": n})
        with srv.op_lock:
            if not srv.services.vault_exists():
                srv.guard.fail()
                raise ApiError(401, "unlock_failed")
            try:
                vault = srv.services.open_vault()
                vault.unlock(pw)
                rt = srv.services.make_runtime(vault)
            except Exception as exc:
                if type(exc).__name__ != "WrongPassphrase":
                    sys.stderr.write(f"muse-app unlock failed: {type(exc).__name__}\n")
                srv.guard.fail()
                raise ApiError(401, "unlock_failed") from None
        srv.guard.success()
        self._start(rt)

    def h_lock(self, sess: Session, body: dict) -> None:
        self.server.end_session()
        self._json(200, {"ok": True}, [self.server.clear_cookie()])

    def h_touch(self, sess: Session, body: dict) -> None:
        self._json(200, {"ok": True, "idle_remaining": sess.idle})

    def h_set_idle(self, sess: Session, body: dict) -> None:
        m = body.get("minutes")
        if isinstance(m, bool) or not isinstance(m, int) or m not in IDLE_CHOICES:
            raise ApiError(400, "invalid")
        sess.idle = m * 60
        self._json(200, {"ok": True, "idle_seconds": sess.idle})

    # -- records -----------------------------------------------------------------
    def h_records_list(self, sess: Session, body: dict) -> None:
        q = self._q()
        kind = None
        if q.get("kind"):
            kind = q["kind"][0]
            if kind not in RECORD_KINDS:
                raise ApiError(400, "invalid")
        limit = _qint(q, "limit", 200, 1, 500)
        with self.server.op_lock:
            rows = sess.runtime.vault.list(kind=kind, limit=limit)
        self._json(200, {"records": [rec_json(r) for r in rows]})

    def h_records_get(self, sess: Session, body: dict, rid: str) -> None:
        with self.server.op_lock:
            rec, text = sess.runtime.vault.get(_id(rid))
        self._json(200, {"record": rec_json(rec), "text": text})

    def h_records_add(self, sess: Session, body: dict) -> None:
        kind = _str(body, "kind", 1, 40)
        if kind not in RECORD_KINDS:
            raise ApiError(400, "invalid")
        title = _str(body, "title", 1, 200)
        text = _str(body, "text", 1, 200_000, strip=False)
        occurred = _date(body.get("occurred_on"))
        source = _str(body, "source", 0, 200, required=False) or ""
        with self.server.op_lock:
            rec = sess.runtime.vault.add(kind, title, text, occurred, source)
            self._receipt(sess, "vault.write", record_refs=(rec.content_sha256,), detail="1 record added")
        self._json(201, {"record": rec_json(rec)})

    def h_records_delete(self, sess: Session, body: dict, rid: str) -> None:
        with self.server.op_lock:
            rec, _ = sess.runtime.vault.get(_id(rid))
            sess.runtime.vault.delete(rec.id)
            self._receipt(sess, "vault.write", record_refs=(rec.content_sha256,), detail="1 record deleted")
        self._json(200, {"ok": True})

    def h_import_(self, sess: Session, body: dict) -> None:
        name = _str(body, "filename", 1, 255)
        name = os.path.basename(name.replace("\\", "/")) or "file"
        if name.rsplit(".", 1)[-1].lower() not in IMPORT_EXTS or "." not in name:
            raise ApiError(415, "unsupported_media_type")
        b64 = body.get("content_base64")
        preview = body.get("preview", False)
        if not isinstance(b64, str) or not isinstance(preview, bool):
            raise ApiError(400, "invalid")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "invalid") from None
        if not data:
            raise ApiError(400, "invalid")
        try:
            docs = self.server.services.parse_import(name, data)
        except ValueError:
            raise ApiError(422, "import_failed") from None
        if not docs or len(docs) > 50:
            raise ApiError(422, "import_failed")
        clean = []
        for d in docs:
            if d.kind not in RECORD_KINDS or not d.title.strip() or not d.text.strip():
                raise ApiError(422, "import_failed")
            clean.append(dataclasses.replace(d, title=d.title.strip()[:200], text=d.text[:200_000],
                                             occurred_on=_date(d.occurred_on), source=(d.source or "")[:200]))
        if preview:
            self._json(200, {"preview": [{"kind": d.kind, "title": d.title, "occurred_on": d.occurred_on,
                                          "source": d.source, "size": len(d.text), "excerpt": _excerpt(d.text)}
                                         for d in clean]})
            return
        added: list[Record] = []
        with self.server.op_lock:
            for d in clean:
                added.append(sess.runtime.vault.add(d.kind, d.title, d.text, d.occurred_on, d.source))
            self._receipt(sess, "vault.write", record_refs=tuple(r.content_sha256 for r in added),
                          detail=f"{len(added)} records imported")
        self._json(201, {"added": [rec_json(r) for r in added]})

    # -- items ------------------------------------------------------------------
    def h_items_list(self, sess: Session, body: dict) -> None:
        q = self._q()
        kind = q["kind"][0] if q.get("kind") else None
        if kind is not None and kind not in ITEM_KINDS:
            raise ApiError(400, "invalid")
        with self.server.op_lock:
            rows = sess.runtime.vault.list_items(kind)
        self._json(200, {"items": [item_json(i) for i in rows]})

    def h_items_add(self, sess: Session, body: dict) -> None:
        kind = _str(body, "kind", 1, 40)
        if kind not in ITEM_KINDS:
            raise ApiError(400, "invalid")
        data = _item_data(body.get("data"))
        with self.server.op_lock:
            item = sess.runtime.vault.add_item(kind, data)
            self._receipt(sess, "items.write", detail=f"1 {kind} added")
        self._json(201, {"item": item_json(item)})

    def h_items_patch(self, sess: Session, body: dict, iid: str) -> None:
        data = _item_data(body.get("data"))
        with self.server.op_lock:
            item = sess.runtime.vault.update_item(_id(iid), data)
            self._receipt(sess, "items.write", detail=f"1 {item.kind} changed")
        self._json(200, {"item": item_json(item)})

    def h_items_delete(self, sess: Session, body: dict, iid: str) -> None:
        with self.server.op_lock:
            sess.runtime.vault.delete_item(_id(iid))
            self._receipt(sess, "items.write", detail="1 item deleted")
        self._json(200, {"ok": True})

    # -- chat ------------------------------------------------------------------
    def h_chat(self, sess: Session, body: dict) -> None:
        text = _str(body, "text", 1, 4000)
        hist_in = body.get("history", [])
        if not isinstance(hist_in, list) or len(hist_in) > 20:
            raise ApiError(400, "invalid")
        history = []
        for h in hist_in:
            if not isinstance(h, dict) or h.get("role") not in ("user", "assistant"):
                raise ApiError(400, "invalid")
            c = h.get("text")
            if not isinstance(c, str) or len(c) > 8000:
                raise ApiError(400, "invalid")
            history.append({"role": h["role"], "content": c})
        with self.server.op_lock:
            reply = sess.runtime.bot.chat(text, history)
            titles = {}
            if reply.cited_records:
                try:
                    titles = {r.id: r for r in sess.runtime.vault.list(limit=500)}
                except Exception:
                    titles = {}
        cited = []
        for rid in reply.cited_records[:20]:
            r = titles.get(rid)
            cited.append({"id": rid, "title": r.title if r else None, "kind": r.kind if r else None,
                          "occurred_on": r.occurred_on if r else None})
        self._json(200, {
            "text": str(reply.text)[:20000],
            "cited": cited,
            "proposed": [{"id": p.id, "kind": p.kind, "summary": p.summary} for p in reply.proposed[:10]],
            "safety": reply.safety if reply.safety in ("urgent", "declined_medical_advice", "no_model") else None,
            "receipts": [int(x) for x in reply.receipts[:50]],
            "route": reply.route,
        })

    def h_change(self, sess: Session, body: dict, cid: str, act: str) -> None:
        _id(cid)
        try:
            with self.server.op_lock:
                result = getattr(sess.runtime.bot, act)(cid)
        except PermissionError:
            raise ApiError(403, "not_allowed") from None
        out: dict[str, Any] = {"ok": True}
        if isinstance(result, BotReply):  # the real bot answers in words, e.g. "saved" or "expired"
            out["text"] = str(result.text)[:2000]
            out["receipts"] = [int(x) for x in result.receipts[:50]]
        elif isinstance(result, Record):
            out["record"] = rec_json(result)
        elif isinstance(result, Item):
            out["item"] = item_json(result)
        self._json(200, out)

    # -- mandates / receipts -----------------------------------------------------
    def h_mandates_list(self, sess: Session, body: dict) -> None:
        now = _now_iso()
        with self.server.op_lock:
            rows = sess.runtime.mandates.all()
        self._json(200, {"mandates": [mandate_json(m, now) for m in rows]})

    def h_mandates_revoke(self, sess: Session, body: dict, mid: str) -> None:
        with self.server.op_lock:
            sess.runtime.mandates.revoke(_id(mid))
            self._receipt(sess, "mandate.revoke", mandate_id=mid, detail="mandate revoked")
        self._json(200, {"ok": True})

    def h_mandates_default(self, sess: Session, body: dict) -> None:
        with self.server.op_lock:
            m = self.server.services.grant_default_mandate(sess.runtime.mandates)
            self._receipt(sess, "mandate.grant", mandate_id=m.id, detail="default assistant mandate")
        self._json(201, {"mandate": mandate_json(m, _now_iso())})

    def h_receipts_list(self, sess: Session, body: dict) -> None:
        q = self._q()
        limit = _qint(q, "limit", 50, 1, 200)
        before = _qint(q, "before", 0, 1, 2**62) if q.get("before") else None
        with self.server.op_lock:
            rows = sess.runtime.receipts.list(limit, before)
        self._json(200, {"receipts": [receipt_json(r) for r in rows]})

    def h_receipts_verify(self, sess: Session, body: dict) -> None:
        with self.server.op_lock:
            v = sess.runtime.receipts.verify()
        self._json(200, {"ok": bool(v.ok), "count": int(v.count),
                         "bad_seq": v.bad_seq, "reason": str(v.reason)[:200]})

    # -- export / passphrase / delete ----------------------------------------------
    def h_export(self, sess: Session, body: dict) -> None:
        tmp = tempfile.mkdtemp(prefix="muse-export-")
        try:
            os.chmod(tmp, 0o700)
            dest = os.path.join(tmp, "archive")
            with self.server.op_lock:
                sess.runtime.vault.export_archive(dest)
                data = Path(dest).read_bytes()
                self._receipt(sess, "vault.export", detail="encrypted archive exported")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        name = "muse-health-" + datetime.now().strftime("%Y%m%d") + ".musevault"
        self._send(200, data, "application/octet-stream",
                   [("Content-Disposition", f'attachment; filename="{name}"')])

    def _throttle(self) -> None:
        wait = self.server.guard.check()
        if wait > 0:
            n = max(1, int(wait + 0.999))
            raise ApiError(429, "rate_limited", headers={"Retry-After": str(n)}, extra={"retry_after": n})

    def h_passphrase(self, sess: Session, body: dict) -> None:
        old = _str(body, "old", 1, MAX_PASSPHRASE, strip=False)
        new = _str(body, "new", 1, MAX_PASSPHRASE, strip=False)
        if len(new) < MIN_PASSPHRASE:
            raise ApiError(400, "weak_passphrase")
        self._throttle()
        with self.server.op_lock:
            try:
                self.server.services.change_passphrase(sess.runtime.vault, old, new)
            except NotSupported:
                raise
            except VaultLocked:
                raise
            except Exception as exc:
                if type(exc).__name__ != "WrongPassphrase":
                    sys.stderr.write(f"muse-app passphrase change failed: {type(exc).__name__}\n")
                self.server.guard.fail()
                raise ApiError(401, "unlock_failed") from None
            self._receipt(sess, "vault.passphrase", detail="passphrase changed")
        self.server.guard.success()
        self._json(200, {"ok": True})

    def h_vault_delete(self, sess: Session, body: dict) -> None:
        pw = _str(body, "passphrase", 1, MAX_PASSPHRASE, strip=False)
        if body.get("confirm") is not True:
            raise ApiError(400, "invalid")
        self._throttle()
        srv = self.server
        with srv.op_lock:
            try:
                check = srv.services.open_vault()
                check.unlock(pw)
            except Exception:
                srv.guard.fail()
                raise ApiError(401, "unlock_failed") from None
            srv.guard.success()
            srv.end_session()
            srv.services.destroy_vault(check)
        self._json(200, {"ok": True}, [srv.clear_cookie()])


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------
class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, addr: tuple[str, int], services: Services, *, remote: bool, idle_seconds: int,
                 clock: Callable[[], float], guard: UnlockGuard, extra_hosts: set[str] | None, quiet: bool):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)
        port = self.server_address[1]
        self.services = services
        self.remote = remote
        self.clock = clock
        self.guard = guard
        self.quiet = quiet
        self.default_idle = idle_seconds
        self.op_lock = threading.RLock()       # serialises calls into vault / bot
        self._sess_lock = threading.Lock()
        self.session: Session | None = None
        if remote:
            self.allowed_hosts: set[str] | None = extra_hosts or None
        else:
            self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self._stop = threading.Event()
        threading.Thread(target=self._reap_loop, daemon=True).start()

    # -- sessions -------------------------------------------------------------
    @staticmethod
    def new_sid() -> str:
        return secrets.token_urlsafe(32)  # 256 bits

    @staticmethod
    def _hash(sid: str) -> str:
        return hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()

    def begin_session(self, runtime: Runtime, sid: str) -> tuple[str, int]:
        """Replace any existing session (one per vault) and return (csrf, idle seconds)."""
        csrf = secrets.token_urlsafe(32)
        with self._sess_lock:
            old = self.session
            self.session = Session(self._hash(sid), csrf, runtime, self.clock(), self.default_idle)
        if old is not None and old.runtime.vault is not runtime.vault:
            self._lock_vault(old)
        return csrf, self.default_idle

    def find_session(self, cookie_value: str, touch: bool = True) -> Session | None:
        with self._sess_lock:
            s = self.session
            if s is None:
                return None
            if not hmac.compare_digest(self._hash(cookie_value).encode(), s.sid_hash.encode()):
                return None
            if self.clock() - s.last_seen >= s.idle:
                expired = s
                self.session = None
            else:
                if touch:
                    s.last_seen = self.clock()
                return s
        self._lock_vault(expired)
        return None

    def end_session(self) -> None:
        with self._sess_lock:
            s, self.session = self.session, None
        if s is not None:
            self._lock_vault(s)

    @staticmethod
    def _lock_vault(s: Session) -> None:
        try:
            s.runtime.vault.lock()
        except Exception:
            pass

    def reap(self) -> None:
        with self._sess_lock:
            s = self.session
            if s is None or self.clock() - s.last_seen < s.idle:
                return
            self.session = None
        self._lock_vault(s)

    def _reap_loop(self) -> None:
        while not self._stop.wait(1.0):
            self.reap()

    def make_cookie(self, sid: str) -> tuple[str, str]:
        flags = "; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self.remote else "")
        return ("Set-Cookie", f"{COOKIE}={sid}{flags}")

    def clear_cookie(self) -> tuple[str, str]:
        flags = "; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if self.remote else "")
        return ("Set-Cookie", f"{COOKIE}={flags}")

    def server_close(self) -> None:
        self._stop.set()
        self.end_session()
        super().server_close()


def make_server(services: Services, host: str = "127.0.0.1", port: int = 4190, *,
                idle_seconds: int | None = None, clock: Callable[[], float] = time.monotonic,
                guard: UnlockGuard | None = None, environ: Mapping[str, str] | None = None,
                quiet: bool = False) -> AppServer:
    """Build (but do not start) the server. port=0 picks a free port."""
    env = os.environ if environ is None else environ
    remote = False
    if not is_loopback(host):
        if env.get("MUSE_APP_ALLOW_REMOTE") != "1":
            raise ValueError(
                f"refusing to bind {host!r}: this app handles a health vault passphrase over plain HTTP. "
                "Set MUSE_APP_ALLOW_REMOTE=1 only behind a TLS-terminating reverse proxy."
            )
        remote = True
    extra = {h.strip().lower() for h in env.get("MUSE_APP_ALLOWED_HOSTS", "").split(",") if h.strip()} or None
    if idle_seconds is None:
        try:
            idle_seconds = max(30, int(env.get("MUSE_APP_IDLE_SECONDS", DEFAULT_IDLE)))
        except ValueError:
            idle_seconds = DEFAULT_IDLE
    srv = AppServer((host, port), services, remote=remote, idle_seconds=idle_seconds, clock=clock,
                    guard=guard or UnlockGuard(clock), extra_hosts=extra, quiet=quiet)
    if remote:
        print(
            f"WARNING: health app bound to {host}. Passphrases and health records travel over this "
            "connection: put a TLS-terminating reverse proxy in front and never expose it directly. "
            + ("" if extra else "Set MUSE_APP_ALLOWED_HOSTS to enable the Host check."),
            file=sys.stderr,
        )
    return srv


def serve(services: Services, host: str = "127.0.0.1", port: int = 4190, open_browser: bool = False, **kw: Any) -> None:
    try:
        srv = make_server(services, host, port, **kw)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{srv.server_address[1]}/"
    print(f"Muse health app: {url}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
