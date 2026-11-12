"""Health web app: server security, endpoints, page hygiene, XSS (Playwright)."""

from __future__ import annotations

import base64
import http.client
import json
import re
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from health_app_fakes import FakeReceiptLog, FakeVault, make_fake_services  # noqa: E402
from muse.health.app import server as appsrv  # noqa: E402
from muse.health.contracts import BotReply  # noqa: E402

PASS = "SECRET-PASS-xyz-123"
INDEX = (Path(appsrv.__file__).parent / "index.html").read_text(encoding="utf-8")
SECRET_TEXT = "PLAINTEXT-MARKER-7731"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Client:
    def __init__(self, srv, host: str | None = None):
        self.port = srv.server_address[1]
        self.host = host or f"127.0.0.1:{self.port}"
        self.cookie = None
        self.csrf = None

    def req(self, method, path, body=None, *, headers=None, raw=None, csrf=True, cookie=True, ctype="application/json"):
        h = {"Host": self.host}
        if ctype:
            h["Content-Type"] = ctype
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        if csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        sc = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        c.close()
        return r.status, hdrs, payload, sc

    def js(self, *a, **k):
        s, h, p, sc = self.req(*a, **k)
        return s, (json.loads(p) if p and h.get("content-type", "").startswith("application/json") else None), h, sc

    def unlock(self, pw=PASS, path="/api/unlock", extra=None):
        s, d, h, sc = self.js("POST", path, {"passphrase": pw, **(extra or {})}, csrf=False, cookie=False)
        if s == 200:
            self.cookie = sc[0].split(";")[0]
            self.csrf = d["csrf"]
        return s, d, h, sc


@pytest.fixture()
def app():
    clock = Clock()
    svc, store = make_fake_services(passphrase=PASS, seed=True)
    srv = appsrv.make_server(svc, port=0, clock=clock, idle_seconds=600, environ={})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(srv)
    yield type("App", (), {"srv": srv, "c": c, "store": store, "svc": svc, "clock": clock})
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def fresh():
    svc, store = make_fake_services()
    srv = appsrv.make_server(svc, port=0, environ={})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("App", (), {"srv": srv, "c": Client(srv), "store": store, "svc": svc})
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def ok(app):
    s, d, h, sc = app.c.unlock()
    assert s == 200
    return app


# ---------------------------------------------------------------- state / setup
def test_state_while_locked(app):
    s, d, h, _ = app.c.js("GET", "/api/state")
    assert s == 200 and d["unlocked"] is False and d["vault_exists"] is True
    assert d["tenant"]["consent_version"] == "demo-0.1" and "csrf" not in d
    assert d["tenant"]["display_name"] and "emergency_text" in d["tenant"]


def test_setup_requires_current_consent_and_strong_passphrase(fresh):
    c = fresh.c
    assert c.js("POST", "/api/setup", {"passphrase": "longenough1", "consent_version": "old"}, csrf=False)[0] == 400
    assert c.js("POST", "/api/setup", {"passphrase": "longenough1"}, csrf=False)[0] == 400
    s, d, *_ = c.js("POST", "/api/setup", {"passphrase": "short", "consent_version": "demo-0.1"}, csrf=False)
    assert s == 400 and d["error"] == "weak_passphrase"
    assert not fresh.store.exists


def test_setup_creates_vault_mandate_and_receipts(fresh):
    c = fresh.c
    s, d, h, sc = c.unlock("longenough1", "/api/setup", {"consent_version": "demo-0.1"})
    assert s == 200 and d["csrf"]
    acts = [r.action for r in fresh.store.receipts]
    assert acts == ["consent.accept", "mandate.grant"]
    assert len(fresh.store.mandates) == 1 and fresh.store.mandates[0].revoked_at is None
    assert c.js("POST", "/api/setup", {"passphrase": "longenough1", "consent_version": "demo-0.1"}, csrf=False)[0] == 409


# ---------------------------------------------------------------- cookies, headers
def test_cookie_flags_and_session_entropy(app):
    s, d, h, sc = app.c.unlock()
    assert s == 200 and len(sc) == 1
    flags = sc[0]
    assert "HttpOnly" in flags and "SameSite=Strict" in flags and "Path=/" in flags and "Secure" not in flags
    sid = sc[0].split(";")[0].split("=", 1)[1]
    assert len(sid) >= 43 and len(d["csrf"]) >= 43
    assert sid != d["csrf"]


def test_secure_cookie_when_remote(app):
    svc, _ = make_fake_services(passphrase=PASS)
    srv = appsrv.make_server(svc, host="0.0.0.0", port=0, environ={"MUSE_APP_ALLOW_REMOTE": "1", "MUSE_APP_ALLOWED_HOSTS": "health.example"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv, host="health.example")
        s, d, h, sc = c.unlock()
        assert s == 200 and "Secure" in sc[0]
        assert Client(srv, host="evil.example").js("GET", "/api/state")[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


REQUIRED = {
    "content-security-policy": appsrv.CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


def _check_headers(h):
    for k, v in REQUIRED.items():
        assert h.get(k) == v, k
    assert "camera=()" in h["permissions-policy"] and "microphone=()" in h["permissions-policy"]
    assert "geolocation=()" in h["permissions-policy"]
    assert "access-control-allow-origin" not in h


def test_headers_on_every_status(ok):
    c = ok.c
    seen = set()
    cases = [
        c.js("GET", "/api/state"), c.js("GET", "/healthz"), c.js("GET", "/nope"),
        c.js("PUT", "/api/records"), c.js("OPTIONS", "/api/records"),
        c.js("POST", "/api/records", {"x": 1}, ctype="text/plain"),
        c.js("POST", "/api/records", raw=b"x" * (appsrv.MAX_BODY + 1)),
        c.js("POST", "/api/records", {"x": 1}, csrf=False),
        c.js("GET", "/api/records", cookie=False),
        c.js("GET", "/api/state", headers={"Host": "evil.test"}),
        c.js("GET", "/api/records/..%2f..%2fetc"),
        c.js("POST", "/api/records", raw=b"{not json"),
    ]
    for s, d, h, _ in cases:
        seen.add(s)
        _check_headers(h)
    s, h, p, _ = c.req("GET", "/")
    _check_headers(h)
    assert {200, 401, 403, 404, 405, 413, 415, 400} <= seen
    # malformed request line handled by http.server itself
    import socket
    sk = socket.create_connection(("127.0.0.1", c.port))
    sk.sendall(b"GARBAGE\r\n\r\n")
    data = sk.recv(4096).decode("latin-1")
    sk.close()
    assert "Content-Security-Policy" in data


def test_host_allowlist(app):
    for host in ("evil.example", f"evil.example:{app.c.port}", "127.0.0.1", "", f"localhost:{app.c.port + 1}"):
        s, d, *_ = app.c.js("GET", "/api/state", headers={"Host": host})
        assert s == 403 and d["error"] == "forbidden_host"
    assert Client(app.srv, host=f"localhost:{app.c.port}").js("GET", "/api/state")[0] == 200


# ---------------------------------------------------------------- auth / csrf
PROTECTED = [
    ("GET", "/api/records"), ("GET", "/api/records/rec_0001"), ("POST", "/api/records"), ("DELETE", "/api/records/rec_0001"),
    ("POST", "/api/import"), ("GET", "/api/items"), ("POST", "/api/items"), ("PATCH", "/api/items/item_1"),
    ("DELETE", "/api/items/item_1"), ("POST", "/api/chat"), ("POST", "/api/changes/c/confirm"),
    ("POST", "/api/changes/c/reject"), ("GET", "/api/mandates"), ("POST", "/api/mandates/grant-default"),
    ("POST", "/api/mandates/m/revoke"), ("GET", "/api/receipts"), ("GET", "/api/receipts/verify"),
    ("GET", "/api/export"), ("POST", "/api/passphrase"), ("POST", "/api/lock"), ("POST", "/api/vault/delete"),
    ("POST", "/api/session/touch"), ("POST", "/api/session/idle"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_401_when_locked(app, method, path):
    s, d, h, _ = app.c.js(method, path, {} if method != "GET" else None, cookie=False, csrf=False)
    assert s == 401 and d["error"] == "locked"
    _check_headers(h)


@pytest.mark.parametrize("token", [None, "wrong", "x" * 43])
def test_csrf_missing_or_wrong(ok, token):
    c = ok.c
    c.csrf = token
    s, d, *_ = c.js("POST", "/api/records", {"kind": "note", "title": "t", "text": "x"})
    assert s == 403 and d["error"] == "csrf"
    assert c.js("GET", "/api/export")[0] == 403
    assert c.js("POST", "/api/lock")[0] == 403
    assert ok.srv.session is not None


def test_cross_site_origin_and_fetch_metadata(ok):
    c = ok.c
    body = {"kind": "note", "title": "t", "text": "x"}
    for hdr in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": f"http://127.0.0.1:{c.port + 1}"},
                {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
        s, d, *_ = c.js("POST", "/api/records", body, headers=hdr)
        assert s == 403 and d["error"] == "cross_site", hdr
        assert c.js("POST", "/api/unlock", {"passphrase": PASS}, headers=hdr, csrf=False, cookie=False)[0] == 403
    good = {"Origin": f"http://127.0.0.1:{c.port}", "Sec-Fetch-Site": "same-origin"}
    assert c.js("POST", "/api/records", body, headers=good)[0] == 201


def test_wrong_content_type_and_body_limits(ok):
    c = ok.c
    for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        s, d, *_ = c.js("POST", "/api/records", raw=b'{"a":1}', ctype=ct)
        assert s == 415, ct
    s, d, *_ = c.js("POST", "/api/records", raw=b"{" + b" " * appsrv.MAX_BODY + b"}")
    assert s == 413 and d["error"] == "too_large"
    big = base64.b64encode(b"a" * 1_000_000).decode()
    assert c.js("POST", "/api/import", {"filename": "a.txt", "content_base64": big, "preview": True})[0] == 200
    huge = json.dumps({"filename": "a.txt", "content_base64": "A" * (appsrv.MAX_IMPORT_BODY + 10)}).encode()
    assert c.js("POST", "/api/import", raw=huge)[0] == 413
    assert c.js("POST", "/api/records", raw=b"[1,2]")[0] == 400
    assert c.js("POST", "/api/records", raw=b"[" * 5000)[0] == 400


def test_slow_body_times_out(ok, monkeypatch):
    monkeypatch.setattr(appsrv, "BODY_TIMEOUT", 0.5)
    import socket
    sk = socket.create_connection(("127.0.0.1", ok.c.port))
    sk.settimeout(5)
    head = (f"POST /api/records HTTP/1.1\r\nHost: {ok.c.host}\r\nContent-Type: application/json\r\nCookie: {ok.c.cookie}\r\n"
            f"X-CSRF-Token: {ok.c.csrf}\r\nContent-Length: 100\r\n\r\n{{").encode()
    sk.sendall(head)
    data = sk.recv(4096)
    sk.close()
    assert b" 408 " in data.split(b"\r\n")[0] or data == b""


# ---------------------------------------------------------------- unlock
def test_unlock_backoff_and_uniform_errors(app):
    c, clock = app.c, app.clock
    bodies = []
    for _ in range(3):
        s, d, h, _ = c.unlock("wrong-1")
        assert s == 401
        bodies.append(d)
    assert bodies[0] == bodies[1] == bodies[2] == {"error": "unlock_failed", "message": "Could not unlock."}
    s, d, h, _ = c.unlock(PASS)               # correct, but inside the back-off window
    assert s == 429 and d["error"] == "rate_limited" and int(h["retry-after"]) >= 1
    assert "wrong" not in json.dumps(d)
    clock.t += 1.1
    assert c.unlock("wrong-2")[0] == 401      # fourth failure -> wait doubles
    clock.t += 1.5
    assert c.unlock(PASS)[0] == 429
    clock.t += 2.5
    assert c.unlock(PASS)[0] == 200           # success clears the back-off
    clock.t += 1
    assert c.unlock("nope")[0] == 401


def test_unlock_no_vault_looks_the_same(fresh):
    s, d, *_ = fresh.c.js("POST", "/api/unlock", {"passphrase": "whatever1"}, csrf=False, cookie=False)
    assert s == 401 and d == {"error": "unlock_failed", "message": "Could not unlock."}


def test_unlock_per_minute_cap(app):
    c, clock = app.c, app.clock
    app.srv.guard.per_minute = 5
    app.srv.guard.base = 0.0
    codes = []
    for _ in range(7):
        codes.append(c.unlock("bad")[0])
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    clock.t += 61
    assert c.unlock(PASS)[0] == 200


def test_unlock_rejects_bad_types(app):
    for body in ({"passphrase": 123}, {"passphrase": ""}, {"passphrase": None}, {}, {"passphrase": ["a"]}):
        assert app.c.js("POST", "/api/unlock", body, csrf=False, cookie=False)[0] == 400


# ---------------------------------------------------------------- happy paths
def test_records_crud_and_import(ok):
    c = ok.c
    s, d, *_ = c.js("GET", "/api/records?limit=3")
    assert s == 200 and len(d["records"]) == 3 and "text" not in d["records"][0]
    s, d, *_ = c.js("GET", "/api/records?kind=lab")
    assert {r["kind"] for r in d["records"]} == {"lab"}
    assert c.js("GET", "/api/records?kind=bogus")[0] == 400
    assert c.js("GET", "/api/records?limit=0")[0] == 400
    s, d, *_ = c.js("POST", "/api/records", {"kind": "note", "title": "新筆記", "text": SECRET_TEXT, "occurred_on": "2026-11-01", "source": "自己", "extra": 1})
    assert s == 201
    rid = d["record"]["id"]
    s, d, *_ = c.js("GET", f"/api/records/{rid}")
    assert d["text"] == SECRET_TEXT and d["record"]["title"] == "新筆記"
    for bad in ({"kind": "x", "title": "t", "text": "x"}, {"kind": "note", "title": "", "text": "x"}, {"kind": "note", "title": "t", "text": 5},
                {"kind": "note", "title": "t", "text": "x", "occurred_on": "2026-13-45"}, {"kind": "note", "title": "t" * 201, "text": "x"}):
        assert c.js("POST", "/api/records", bad)[0] == 400
    assert c.js("DELETE", f"/api/records/{rid}")[0] == 200
    assert c.js("GET", f"/api/records/{rid}")[0] == 404
    assert c.js("DELETE", f"/api/records/{rid}")[0] == 404
    assert c.js("GET", "/api/records/a%20b%3Cscript%3E")[0] == 404
    # import: preview does not save, commit does
    n = len(ok.store.records)
    payload = {"filename": "報告.txt", "content_base64": base64.b64encode("血壓報告\n130/80".encode()).decode()}
    s, d, *_ = c.js("POST", "/api/import", {**payload, "preview": True})
    assert s == 200 and d["preview"][0]["title"] == "血壓報告" and len(ok.store.records) == n
    s, d, *_ = c.js("POST", "/api/import", payload)
    assert s == 201 and len(d["added"]) == 1 and len(ok.store.records) == n + 1
    assert c.js("POST", "/api/import", {**payload, "filename": "x.exe"})[0] == 415
    assert c.js("POST", "/api/import", {**payload, "content_base64": "!!!"})[0] == 400
    bad = {"filename": "b.txt", "content_base64": base64.b64encode(b"BAD-FILE").decode()}
    s, d, *_ = c.js("POST", "/api/import", bad)
    assert s == 422 and d["error"] == "import_failed"
    assert ok.store.receipts[-1].action == "vault.write" and ok.store.receipts[-1].record_refs


def test_items_crud(ok):
    c = ok.c
    s, d, *_ = c.js("GET", "/api/items")
    assert s == 200 and {i["kind"] for i in d["items"]} >= {"medication", "appointment", "reminder", "question_list"}
    s, d, *_ = c.js("GET", "/api/items?kind=medication")
    assert all(i["kind"] == "medication" for i in d["items"])
    s, d, *_ = c.js("POST", "/api/items", {"kind": "medication", "data": {"name": "維他命 D", "dose": "每天 1 顆", "schedule": "09:00"}})
    assert s == 201
    iid = d["item"]["id"]
    s, d, *_ = c.js("PATCH", f"/api/items/{iid}", {"data": {"name": "維他命 D3", "dose": "每天 1 顆", "schedule": "09:00"}})
    assert s == 200 and d["item"]["data"]["name"] == "維他命 D3"
    for bad in ({"kind": "x", "data": {"a": "b"}}, {"kind": "reminder", "data": {}}, {"kind": "reminder", "data": "s"},
                {"kind": "reminder", "data": {"Bad Key": "x"}}, {"kind": "reminder", "data": {"a": {"nested": 1}}}):
        assert c.js("POST", "/api/items", bad)[0] == 400
    assert c.js("DELETE", f"/api/items/{iid}")[0] == 200
    assert c.js("PATCH", f"/api/items/{iid}", {"data": {"a": "b"}})[0] == 404


def test_chat_variants_and_changes(ok):
    c = ok.c
    s, d, *_ = c.js("POST", "/api/chat", {"text": "解釋抽血報告", "history": [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]})
    assert s == 200 and d["cited"][0]["title"] and d["route"].endswith("(local)") and d["receipts"] and d["safety"] is None
    assert ok.store.chat_calls[-1][1] == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert c.js("POST", "/api/chat", {"text": "我胸痛"})[1]["safety"] == "urgent"
    assert c.js("POST", "/api/chat", {"text": "可以停藥嗎"})[1]["safety"] == "declined_medical_advice"
    assert c.js("POST", "/api/chat", {"text": "nomodel"})[1]["safety"] == "no_model"
    for bad in ({"text": ""}, {"text": 5}, {"text": "x" * 4001}, {"text": "x", "history": "no"}, {"text": "x", "history": [{"role": "system", "text": "x"}]},
                {"text": "x", "history": [{"role": "user"}]}, {"text": "x", "history": [{"role": "user", "text": "a"}] * 21}):
        assert c.js("POST", "/api/chat", bad)[0] == 400
    s, d, *_ = c.js("POST", "/api/chat", {"text": "提醒我領藥"})
    ch = d["proposed"][0]
    assert "payload" not in ch and ch["summary"]
    n = len(ok.store.items)
    s, d, *_ = c.js("POST", f"/api/changes/{ch['id']}/confirm")
    assert s == 200 and d["text"].startswith("已儲存") and len(ok.store.items) == n + 1
    assert c.js("POST", f"/api/changes/{ch['id']}/confirm")[0] == 404
    ch2 = c.js("POST", "/api/chat", {"text": "提醒我領藥"})[1]["proposed"][0]
    assert c.js("POST", f"/api/changes/{ch2['id']}/reject")[0] == 200
    assert len(ok.store.items) == n + 1


def test_mandates_receipts_verify(ok):
    c = ok.c
    s, d, *_ = c.js("GET", "/api/mandates")
    m = d["mandates"][0]
    assert s == 200 and m["active"] and "signature" not in m and "vault.read" in m["scopes"]
    assert c.js("POST", f"/api/mandates/{m['id']}/revoke")[0] == 200
    assert c.js("GET", "/api/mandates")[1]["mandates"][0]["active"] is False
    assert c.js("POST", "/api/mandates/nope/revoke")[0] == 404
    s, d, *_ = c.js("POST", "/api/mandates/grant-default")
    assert s == 201 and d["mandate"]["active"]
    s, d, *_ = c.js("GET", "/api/receipts?limit=2")
    assert len(d["receipts"]) == 2 and "record_refs" not in d["receipts"][0] and d["receipts"][0]["seq"] > d["receipts"][1]["seq"]
    s, d2, *_ = c.js("GET", f"/api/receipts?limit=2&before={d['receipts'][-1]['seq']}")
    assert d2["receipts"][0]["seq"] < d["receipts"][-1]["seq"]
    assert c.js("GET", "/api/receipts?limit=abc")[0] == 400
    s, d, *_ = c.js("GET", "/api/receipts/verify")
    assert d["ok"] is True and d["count"] >= 3
    FakeReceiptLog(ok.store).tamper(2)
    d = c.js("GET", "/api/receipts/verify")[1]
    assert d["ok"] is False and d["bad_seq"] == 2


def test_export_streams_vault_output_as_attachment(ok):
    s, h, p, _ = ok.c.req("GET", "/api/export")
    assert s == 200 and h["content-disposition"].startswith("attachment;") and h["content-type"] == "application/octet-stream"
    assert p == ok.store.last_archive and p.startswith(b"MUSE-FAKE-ARCHIVE")
    for rec, text in ok.store.records.values():
        assert text.encode() not in p and rec.title.encode() not in p
    assert ok.store.receipts[-1].action == "vault.export"


def test_passphrase_change_and_delete_everything(ok):
    c = ok.c
    s, d, *_ = c.js("POST", "/api/passphrase", {"old": "wrong-old", "new": "brand new phrase"})
    assert s == 401 and d["error"] == "unlock_failed"
    assert c.js("POST", "/api/passphrase", {"old": PASS, "new": "short"})[0] == 400
    ok.clock.t += 5
    assert c.js("POST", "/api/passphrase", {"old": PASS, "new": "brand new phrase"})[0] == 200
    assert c.js("POST", "/api/lock")[0] == 200
    assert c.unlock(PASS)[0] == 401
    ok.clock.t += 5
    assert c.unlock("brand new phrase")[0] == 200
    assert c.js("POST", "/api/vault/delete", {"passphrase": "brand new phrase"})[0] == 400   # confirm flag missing
    assert c.js("POST", "/api/vault/delete", {"passphrase": "nope nope nope", "confirm": True})[0] == 401
    ok.clock.t += 5
    assert c.js("POST", "/api/vault/delete", {"passphrase": "brand new phrase", "confirm": True})[0] == 200
    s, d, *_ = c.js("GET", "/api/state")
    assert d["vault_exists"] is False and d["unlocked"] is False and not ok.store.records


# ---------------------------------------------------------------- sessions
def test_lock_invalidates_session_and_locks_vault(ok):
    c = ok.c
    old_cookie, old_csrf = c.cookie, c.csrf
    s, d, h, sc = c.js("POST", "/api/lock")
    assert s == 200 and "Max-Age=0" in sc[0]
    assert not any(v.is_unlocked() for v in ok.store.vaults)
    assert c.js("GET", "/api/records")[0] == 401
    assert c.js("GET", "/api/state")[1]["unlocked"] is False
    c.unlock()
    c.cookie, c.csrf = old_cookie, old_csrf
    assert c.js("GET", "/api/records")[0] == 401        # old session id is dead even after a new unlock


def test_new_unlock_replaces_old_session(ok):
    other = Client(ok.srv)
    assert other.unlock()[0] == 200
    assert ok.c.js("GET", "/api/records")[0] == 401
    assert other.js("GET", "/api/records")[0] == 200
    assert sum(v.is_unlocked() for v in ok.store.vaults) == 1


def test_idle_auto_lock_enforced_server_side(ok):
    c, clock = ok.c, ok.clock
    clock.t += 590
    assert c.js("GET", "/api/records")[0] == 200           # activity resets the timer
    clock.t += 590
    assert c.js("POST", "/api/session/touch")[0] == 200
    s, d, *_ = c.js("GET", "/api/state")
    assert d["unlocked"] and 0 < d["idle_remaining"] <= 600
    assert c.js("POST", "/api/session/idle", {"minutes": 1})[0] == 200
    assert c.js("POST", "/api/session/idle", {"minutes": 7})[0] == 400
    clock.t += 61
    ok.srv.reap()
    assert not any(v.is_unlocked() for v in ok.store.vaults)
    s, d, *_ = c.js("GET", "/api/records")
    assert s == 401 and d["error"] == "locked"


def test_state_poll_does_not_extend_session(ok):
    ok.clock.t += 400
    ok.c.js("GET", "/api/state")
    ok.clock.t += 250
    assert ok.c.js("GET", "/api/records")[0] == 401


# ---------------------------------------------------------------- leaks
def test_no_secrets_in_errors_or_logs(app, capfd):
    c = app.c
    c.unlock("hunter2-WRONG-PASS")
    c.unlock(PASS)
    app.srv.guard.success()
    bodies = []

    def boom(text, history):
        raise RuntimeError(f"boom {SECRET_TEXT} /home/secret/path {PASS}")

    c.js("POST", "/api/records", {"kind": "note", "title": "t", "text": SECRET_TEXT})
    app.srv.session.runtime.bot.chat = boom
    s, d, h, _ = c.js("POST", "/api/chat", {"text": f"question {SECRET_TEXT}"})
    bodies.append((s, d))
    assert s == 500 and d == {"error": "internal", "message": "Something went wrong."}
    bodies.append(c.js("POST", "/api/records", {"kind": "bad", "title": SECRET_TEXT, "text": "x"})[:2])
    bodies.append(c.js("POST", "/api/passphrase", {"old": "zzz-old-secret", "new": "zzz-new-secret"})[:2])
    app.svc.change_passphrase = lambda *a: (_ for _ in ()).throw(ValueError(f"leak {PASS}"))
    app.clock.t += 5
    bodies.append(c.js("POST", "/api/passphrase", {"old": PASS, "new": "zzz-new-secret"})[:2])
    bodies.append(c.js("GET", f"/api/records/missing?token={SECRET_TEXT}")[:2])
    blob = json.dumps(bodies)
    for needle in (SECRET_TEXT, PASS, "hunter2", "zzz-", "/home/", "Traceback", "RuntimeError", "boom"):
        assert needle not in blob, needle
    err = capfd.readouterr().err
    for needle in (SECRET_TEXT, PASS, "hunter2", "zzz-", "/home/secret", "token="):
        assert needle not in err, needle
    assert "muse-app" in err


def test_nonloopback_bind_refused_without_flag():
    svc, _ = make_fake_services()
    with pytest.raises(ValueError):
        appsrv.make_server(svc, host="0.0.0.0", port=0, environ={})
    with pytest.raises(ValueError):
        appsrv.make_server(svc, host="192.0.2.1", port=0, environ={"MUSE_APP_ALLOW_REMOTE": "0"})
    assert appsrv.is_loopback("::1") and appsrv.is_loopback("127.0.0.1") and not appsrv.is_loopback("10.0.0.2")


def test_serve_exits_on_refused_bind(capsys):
    svc, _ = make_fake_services()
    with pytest.raises(SystemExit):
        appsrv.serve(svc, host="0.0.0.0", port=0, environ={})
    assert "MUSE_APP_ALLOW_REMOTE" in capsys.readouterr().err


def _real_available() -> bool:
    try:
        import muse.health.vault, muse.health.bot, muse.health.importers, muse.health.llm  # noqa: F401, E401
        return True
    except ImportError:
        return False


def test_build_services_fails_clearly_without_real_modules(tmp_path, monkeypatch):
    from muse.health.app.services import ServicesUnavailable, build_services
    from muse.health.tenant import load_tenant
    monkeypatch.setitem(sys.modules, "muse.health.vault", None)   # simulate a missing module
    with pytest.raises(ServicesUnavailable) as e:
        build_services(load_tenant("demo-hospital"), tmp_path / "v", {})
    assert "muse.health.vault" in str(e.value)


@pytest.mark.skipif(not _real_available(), reason="real health modules not installed")
def test_real_services_end_to_end(tmp_path):
    from muse.health.app.services import build_services
    from muse.health.tenant import load_tenant
    svc = build_services(load_tenant("demo-hospital"), tmp_path / "vault.db", {})
    srv = appsrv.make_server(svc, port=0, environ={}, quiet=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv)
        assert c.js("GET", "/api/state")[1]["vault_exists"] is False
        s, d, *_ = c.unlock("a long real passphrase", "/api/setup", {"consent_version": "demo-0.1"})
        assert s == 200
        s, d, *_ = c.js("POST", "/api/records", {"kind": "lab", "title": "血糖報告", "text": "空腹血糖 105 mg/dL", "occurred_on": "2026-10-01"})
        assert s == 201
        rid = d["record"]["id"]
        assert c.js("GET", f"/api/records/{rid}")[1]["text"].startswith("空腹血糖")
        assert c.js("GET", "/api/records/does-not-exist")[0] == 404
        s, d, *_ = c.js("POST", "/api/items", {"kind": "reminder", "data": {"title": "量血壓", "time": "07:30"}})
        assert s == 201
        assert c.js("GET", "/api/mandates")[1]["mandates"][0]["active"]
        s, d, *_ = c.js("POST", "/api/chat", {"text": "我現在胸痛喘不過氣"})
        assert s == 200 and d["safety"] == "urgent"
        s, d, *_ = c.js("GET", "/api/receipts/verify")
        assert d["ok"] is True
        b64 = base64.b64encode("胸部 X 光\n無明顯異常".encode()).decode()
        s, d, *_ = c.js("POST", "/api/import", {"filename": "xray.txt", "content_base64": b64})
        assert s == 201
        s, h, p, _ = c.req("GET", "/api/export")
        assert s == 200 and "血糖".encode() not in p and "空腹血糖".encode() not in p
        assert c.js("POST", "/api/passphrase", {"old": "a long real passphrase", "new": "another long passphrase"})[0] == 200
        assert c.js("POST", "/api/lock")[0] == 200
        srv.guard.success()
        assert c.unlock("a long real passphrase")[0] == 401
        srv.guard.success()
        assert c.unlock("another long passphrase")[0] == 200
        assert c.js("POST", "/api/vault/delete", {"passphrase": "another long passphrase", "confirm": True})[0] == 200
        assert c.js("GET", "/api/state")[1]["vault_exists"] is False
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- page hygiene
def test_index_has_no_external_references_or_innerhtml():
    assert not re.search(r"""(?:src|href|action|poster|data)\s*=\s*["']\s*(?:https?:)?//""", INDEX, re.I)
    assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", INDEX, re.I)
    assert "@import" not in INDEX and "<link rel=\"stylesheet\"" not in INDEX
    assert not re.search(r"<script[^>]+src=", INDEX, re.I)
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage.setItem('muse.csrf", "sessionStorage"):
        assert bad not in INDEX, bad
    for use in re.findall(r"localStorage\.\w+\(([^)]*)\)", INDEX):
        assert use.startswith("'muse.lang'"), use


def test_index_served_with_headers(app):
    s, h, p, _ = app.c.req("GET", "/")
    assert s == 200 and h["content-type"].startswith("text/html") and p.decode() == INDEX


# ---------------------------------------------------------------- Playwright
NODE_OK = shutil.which("node") is not None and subprocess.run(
    ["node", "-e", "require('playwright')"], capture_output=True).returncode == 0
needs_pw = pytest.mark.skipif(not NODE_OK, reason="node/playwright not available")


def _run_node(tmp_path, script: str, *args) -> dict:
    f = tmp_path / "t.js"
    f.write_text(script)
    r = subprocess.run(["node", str(f), *args], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:] + r.stdout[-1000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture()
def live():
    svc, store = make_fake_services(passphrase=PASS, seed=True)
    srv = appsrv.make_server(svc, port=0, environ={}, quiet=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("L", (), {"srv": srv, "store": store, "url": f"http://127.0.0.1:{srv.server_address[1]}/"})
    srv.shutdown()
    srv.server_close()


@needs_pw
def test_xss_payloads_never_execute(live, tmp_path):
    payload = "<img src=x onerror=window.__pwn=1>"
    js_url = "javascript:window.__pwn=2"
    v = FakeVault(live.store)
    v.unlock(PASS)
    rec = v.add("note", f"{payload} {js_url}", f"{payload}\n{js_url}", "2026-11-01", payload)
    v.add_item("reminder", {"title": payload, "date": "2026-11-30", "note": js_url})
    FakeReceiptLog(live.store).append("bot", "vault.read", record_refs=("a" * 64,), egress="cloud", model=payload, detail=f"{payload} {js_url}")
    FakeReceiptLog(live.store).append("bot", payload, egress="cloud", detail=payload)
    live.store.queue.append(BotReply(f"{payload}\n{js_url}\n<script>window.__pwn=3</script>", cited_records=(rec.id,),
                                     route=f"{payload} (local)", safety=None))
    live.store.queue.append(BotReply(payload, safety="urgent"))
    script = textwrap.dedent("""
    const { chromium } = require('playwright');
    (async () => {
      const [url, pass] = process.argv.slice(2);
      const b = await chromium.launch(); const p = await b.newPage();
      const dialogs = []; p.on('dialog', d => { dialogs.push(d.message()); d.dismiss(); });
      const consoleErr = []; p.on('pageerror', e => consoleErr.push(e.message));
      await p.goto(url); await p.fill('#pw', pass); await p.click('button[type=submit]'); await p.waitForSelector('.starters');
      await p.fill('#chat-in', 'hello'); await p.keyboard.press('Enter'); await p.waitForSelector('.bot .answer');
      await p.fill('#chat-in', 'again'); await p.keyboard.press('Enter'); await p.waitForSelector('.urgent');
      const chatText = await p.textContent('.msgs');
      const seen = {};
      for (const tab of ['records', 'reminders', 'trust']) {
        await p.click(`#tabs a[data-tab=${tab}]`); await p.waitForTimeout(500); seen[tab] = await p.textContent('main');
      }
      await p.goto(url + '#/records'); await p.waitForSelector('.row'); await p.click('.row >> nth=0'); await p.waitForSelector('.recbody');
      seen.record = await p.textContent('main');
      const out = await p.evaluate(() => ({
        pwn: window.__pwn === undefined, imgs: document.querySelectorAll('img').length,
        badX: document.querySelectorAll('[src="x"],[onerror]').length,
        jsLinks: [...document.querySelectorAll('[href]')].filter(a => /^\\s*javascript:/i.test(a.getAttribute('href'))).length,
        scripts: document.querySelectorAll('main script').length }));
      console.log(JSON.stringify({ ...out, dialogs, consoleErr, chatText, seen }));
      await b.close();
    })().catch(e => { console.error(e); process.exit(1); });
    """)
    out = _run_node(tmp_path, script, live.url, PASS)
    assert out["pwn"] and out["imgs"] == 0 and out["badX"] == 0 and out["jsLinks"] == 0 and out["scripts"] == 0
    assert out["dialogs"] == [] and out["consoleErr"] == []
    assert payload in out["chatText"] and "window.__pwn=3" in out["chatText"]      # shown as text, not run
    assert payload in out["seen"]["records"] and payload in out["seen"]["trust"] and payload in out["seen"]["reminders"]
    assert js_url in out["seen"]["record"]


@needs_pw
def test_ui_flow_i18n_and_no_overflow(live, tmp_path):
    script = textwrap.dedent("""
    const { chromium } = require('playwright');
    (async () => {
      const [url, pass] = process.argv.slice(2);
      const b = await chromium.launch(); const ctx = await b.newContext({ viewport: { width: 360, height: 740 }, locale: 'zh-TW' });
      const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
      const reqs = []; p.on('request', r => reqs.push(r.url()));
      const res = {};
      await p.goto(url); await p.waitForSelector('#pw');
      res.keys = await p.evaluate(() => { const a = Object.keys(T.zh), b = Object.keys(T.en); return { miss: a.filter(k => !(k in T.en)).concat(b.filter(k => !(k in T.zh))) }; });
      res.title = await p.title();
      await p.fill('#pw', pass); await p.click('button[type=submit]'); await p.waitForSelector('.starters');
      res.overflow = [];
      const check = async n => { res.overflow.push([n, await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)]); };
      for (const tab of ['chat', 'records', 'reminders', 'trust', 'settings']) { await p.click(`#tabs a[data-tab=${tab}]`); await p.waitForTimeout(400); await check(tab); }
      // tap targets
      res.small = await p.evaluate(() => [...document.querySelectorAll('button, a.btn, #tabs a, select, input:not([type=checkbox])')].filter(e => e.offsetParent && (e.getBoundingClientRect().height < 43.5)).map(e => e.textContent.trim().slice(0, 20)));
      // hash routing: back/forward
      await p.click('#tabs a[data-tab=records]'); await p.waitForSelector('.row'); await p.click('.row >> nth=0'); await p.waitForSelector('.recbody');
      await p.goBack(); await p.waitForSelector('.row'); res.back = p.url().endsWith('#/records');
      await p.goForward(); await p.waitForSelector('.recbody'); res.fwd = /#\\/records\\//.test(p.url());
      // csrf not persisted
      res.storage = await p.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
      res.csrfInDom = await p.evaluate(() => document.documentElement.outerHTML.includes(document.cookie || 'zzzz'));
      // language toggle
      await p.click('.lang-btn'); await p.waitForTimeout(300); res.enNav = await p.textContent('#tabs');
      res.noCjkInNav = !/[\\u4e00-\\u9fff]/.test(res.enNav);
      // lock via button
      await p.click('#top .btn'); await p.waitForSelector('#pw'); res.locked = true;
      res.external = reqs.filter(u => !u.startsWith(url) && !u.startsWith('data:'));
      res.errs = errs; console.log(JSON.stringify(res)); await b.close();
    })().catch(e => { console.error(e); process.exit(1); });
    """)
    out = _run_node(tmp_path, script, live.url, PASS)
    assert out["keys"]["miss"] == [] and out["errs"] == [] and out["external"] == []
    assert all(w <= 0 for _, w in out["overflow"]), out["overflow"]
    assert out["small"] == [], out["small"]
    assert out["back"] and out["fwd"] and out["locked"] and out["noCjkInNav"]
    assert "csrf" not in out["storage"].lower() and out["storage"].count(PASS) == 0
