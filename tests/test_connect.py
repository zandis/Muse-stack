"""`muse connect`: the one-tap OAuth flow against a fake provider, and the guided paste-key flow."""

from __future__ import annotations

import hashlib
import base64
import http.client
import http.server
import inspect
import io
import json
import socket
import ssl
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from muse import catalog, cli, connect, keys

CANARY = "sk-or-v1-CANARYKEY0123456789abcdefABCDEF"
CODE = "authcode-CANARY-9f8e7d6c"


# ----------------------------------------------------------------- a fake provider


class FakeProvider:
    """Plays OpenRouter's exchange endpoint on 127.0.0.1 over plain http (the fake is not https)."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.challenges: dict[str, str] = {}  # code -> S256 challenge the browser step registered
        self.used: set[str] = set()
        self.exchanges: list[dict] = []
        self.paths: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_POST(self):  # noqa: N802
                outer.paths.append(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.exchanges.append({"body": body, "ua": self.headers.get("User-Agent"), "ct": self.headers.get("Content-Type")})
                if self.path == "/leak":  # a redirect target that must never be requested
                    return self._send(200, json.dumps({"key": CANARY}).encode())
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/leak")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.mode == "error":
                    return self._send(403, json.dumps({"error": f"Invalid code {CANARY}"}).encode())
                if outer.mode == "oversize":
                    return self._send(200, json.dumps({"key": CANARY, "pad": "x" * 70000}).encode())
                if outer.mode == "badjson":
                    return self._send(200, b"<html>not json</html>")
                if outer.mode == "nokey":
                    return self._send(200, json.dumps({"key": ""}).encode())
                code, verifier = body.get("code"), body.get("code_verifier", "")
                challenge = outer.challenges.get(code)
                ok = (
                    challenge is not None
                    and code not in outer.used
                    and body.get("code_challenge_method") == "S256"
                    and base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == challenge
                )
                if not ok:
                    return self._send(403, b'{"error":"Invalid code or code_verifier"}')
                outer.used.add(code)
                self._send(200, json.dumps({"key": CANARY}).encode())

            def _send(self, status: int, data: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def endpoints(self) -> connect.Endpoints:
        return connect.Endpoints(f"http://127.0.0.1:{self.port}/auth", f"http://127.0.0.1:{self.port}/exchange", allow_insecure=True)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def browse(callback: str, **query: str) -> int:
    """What the person's browser does after approving: GET the callback URL (Host as a browser would send it)."""
    parts = urllib.parse.urlsplit(callback)
    c = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
    c.putrequest("GET", parts.path + ("?" + urllib.parse.urlencode(query) if query else ""), skip_host=True)
    c.putheader("Host", f"localhost:{parts.port}")
    c.endheaders()
    status = c.getresponse().status
    c.close()
    return status


class Rig:
    """The fake provider, a fake browser, captured output, and a private key store."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "ok") -> None:
        self.env_file = tmp_path / "dotenv"
        monkeypatch.setenv("MUSE_ENV_FILE", str(self.env_file))
        monkeypatch.setattr(keys, "_keyring", lambda: None)
        self.provider = FakeProvider(mode)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.opened: list[str] = []
        self.behaviour = "good"
        self.callbacks: list[str] = []
        self.statuses: list[int] = []
        self.threads: list[threading.Thread] = []
        self.io = connect.ConnectIO(
            out=self.out, err=self.err, open_url=self.open_url, probe=lambda key: [{"window": "day", "metric": "requests", "limit": 50, "remaining": 50}],
            proxy_running=lambda: False,
        )

    def close(self) -> None:
        self.provider.close()

    @property
    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def open_url(self, url: str) -> bool:
        self.opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        callback = q["callback_url"][0]
        self.callbacks.append(callback)
        self.provider.challenges[CODE] = q["code_challenge"][0]
        state = q["state"][0]
        parts = urllib.parse.urlsplit(callback)

        def run() -> None:
            b = self.behaviour
            if b == "good":
                self.statuses.append(browse(callback, code=CODE, state=state))
            elif b == "wrong_state":
                self.statuses.append(browse(callback, code=CODE, state="not-the-state"))
            elif b == "wrong_path_first":
                self.statuses.append(browse(f"http://localhost:{parts.port}/cb/wrong", code=CODE, state=state))
                self.statuses.append(browse(f"http://localhost:{parts.port}/", code=CODE, state=state))
                self.statuses.append(browse(callback, code=CODE, state=state))
            elif b == "denied":
                self.statuses.append(browse(callback, error="access_denied"))
            elif b == "no_state":
                self.statuses.append(browse(callback, code=CODE))
            elif b == "dup_code":
                self.statuses.append(browse(callback + f"?code={CODE}&code=other&state={state}"))
            elif b == "nothing":
                pass

        th = threading.Thread(target=run, daemon=True)
        self.threads.append(th)
        th.start()
        return True

    def run(self, provider: str = "openrouter", **kw):
        kw.setdefault("lang", "en")
        kw.setdefault("endpoints", self.provider.endpoints)
        kw.setdefault("timeout_s", 10.0)
        code = connect.run_connect(self.io, {"MUSE_HOME": str(self.env_file.parent / "home")}, provider=provider, **kw)
        for th in self.threads:
            th.join(5)
        return code

    def stored(self) -> str:
        return self.env_file.read_text() if self.env_file.exists() else ""


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


def no_canary(rig: Rig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture | None = None) -> None:
    seen = rig.text + "".join(capsys.readouterr())
    if caplog is not None:
        seen += caplog.text
    for secret in (CANARY, CODE, "CANARYKEY"):
        assert secret not in seen, "a secret reached the output"
    # nothing but the key store holds the key
    for f in rig.env_file.parent.rglob("*"):
        if f.is_file() and f != rig.env_file:
            assert CANARY not in f.read_text(errors="ignore"), f


def listener_closed(callback: str) -> bool:
    port = urllib.parse.urlsplit(callback).port
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return True
    return False


# ------------------------------------------------------------------- OAuth: happy


def test_one_tap_happy_path(rig, capsys, caplog):
    assert rig.run() == 0
    assert f"OPENROUTER_API_KEY={CANARY}" in rig.stored()
    url = rig.opened[0]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith(f"http://127.0.0.1:{rig.provider.port}/auth?")
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) == 43
    cb = urllib.parse.urlsplit(q["callback_url"][0])
    assert cb.hostname == "localhost" and cb.path.startswith("/cb/") and len(cb.path) > 20 and q["state"][0]
    # exactly one exchange, carrying the verifier that matches the challenge, with an honest user agent
    assert len(rig.provider.exchanges) == 1
    ex = rig.provider.exchanges[0]
    assert ex["ct"] == "application/json" and ex["ua"].startswith("muse/") and ex["body"]["code_challenge_method"] == "S256"
    assert rig.statuses == [200]
    assert listener_closed(rig.callbacks[0]), "the listener must shut down straight after the answer"
    out = rig.text
    assert "Free requests left today: 50 of 50" in out
    assert "50 requests a day" in out and "1,000 a day" in out and "learn from what you send" in out  # both honest caveats, numbers from the catalogue
    assert "muse up" in out  # proxy not running
    assert "Next best helper:" in out and "Cohere" in out  # Cerebras needs a card now, so it is no longer the next free step
    assert "Muse does not click anything for you" in out
    no_canary(rig, capsys, caplog)


def test_one_tap_zh_and_proxy_running(rig, monkeypatch):
    rig.io.proxy_running = lambda: True
    assert rig.run(lang="zh-TW") == 0
    assert "今天還剩 50／50 次免費請求" in rig.text and "muse restart" in rig.text and "老實說之二" in rig.text


def test_restart_command_matches_how_the_proxy_runs(tmp_path):
    env = {"MUSE_HOME": str(tmp_path)}
    assert connect.proxy_command(env, True) == "muse restart" and connect.proxy_command(env, False) == "muse up"
    (tmp_path / "proxy.pid").write_text("123")
    assert connect.proxy_command(env, True) == "muse restart --native --detach"
    assert connect.proxy_command(env, False) == "muse up --native --detach"


# ------------------------------------------------------------- OAuth: rejections


def test_wrong_state_is_refused(rig, capsys):
    rig.behaviour = "wrong_state"
    assert rig.run() == 1
    assert rig.statuses == [400] and "did not expect" in rig.text
    assert rig.stored() == "" and rig.provider.exchanges == []
    assert listener_closed(rig.callbacks[0])
    no_canary(rig, capsys)


@pytest.mark.parametrize("behaviour", ["no_state", "dup_code"])
def test_missing_state_or_repeated_parameters_are_refused(rig, behaviour):
    rig.behaviour = behaviour
    assert rig.run() == 1
    assert rig.stored() == "" and rig.provider.exchanges == []


def test_wrong_paths_are_rejected_and_the_real_one_still_works(rig):
    rig.behaviour = "wrong_path_first"
    assert rig.run() == 0
    assert rig.statuses == [404, 404, 200]
    assert len(rig.provider.exchanges) == 1


def test_wrong_host_header_is_rejected():
    r = connect._Receiver()
    try:
        port = r.port
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("GET", f"/cb/{r.path_token}?code=abc&state={r.state}", skip_host=True)
        c.putheader("Host", "evil.example")
        c.endheaders()
        t = threading.Thread(target=lambda: c.getresponse().read(), daemon=True)
        t.start()
        r.handle_request()
        t.join(5)
        assert r.outcome is None  # rejected, not accepted
    finally:
        r.server_close()


def test_other_methods_and_absolute_targets_are_rejected():
    r = connect._Receiver()
    try:
        results = []

        def poke(method: str, target: str) -> None:
            c = http.client.HTTPConnection("127.0.0.1", r.port, timeout=5)
            c.putrequest(method, target, skip_host=True)
            c.putheader("Host", f"localhost:{r.port}")
            c.endheaders()
            results.append(c.getresponse().status)

        for method, target in (("POST", f"/cb/{r.path_token}"), ("GET", f"http://evil.example/cb/{r.path_token}?code=a&state={r.state}")):
            t = threading.Thread(target=poke, args=(method, target), daemon=True)
            t.start()
            r.handle_request()
            t.join(5)
        assert results == [405, 404] and r.outcome is None
    finally:
        r.server_close()


def test_second_request_after_success_is_refused(rig):
    assert rig.run() == 0
    with pytest.raises(OSError):
        browse(rig.callbacks[0], code=CODE, state="x")  # the listener is gone: connection refused
    assert len(rig.provider.exchanges) == 1


def test_listener_binds_loopback_on_a_random_port_with_a_random_path():
    a, b = connect._Receiver(), connect._Receiver()
    try:
        assert a.server_address[0] == "127.0.0.1" and b.server_address[0] == "127.0.0.1"
        assert a.port != b.port and a.path_token != b.path_token and a.state != b.state
        assert a.callback_url.startswith(f"http://localhost:{a.port}/cb/")
    finally:
        a.server_close()
        b.server_close()


def test_state_is_compared_in_constant_time():
    src = inspect.getsource(connect._Handler.do_GET)
    assert src.count("hmac.compare_digest") >= 2  # the path token and the state


def test_denied_on_the_providers_page(rig):
    rig.behaviour = "denied"
    assert rig.run() == 1
    assert "did not approve" in rig.text and rig.stored() == "" and rig.provider.exchanges == []


def test_timeout_leaves_nothing_and_closes_the_listener(rig):
    rig.behaviour = "nothing"
    assert rig.run(timeout_s=0.6) == 1
    assert "Nobody approved in time" in rig.text and rig.stored() == ""
    assert listener_closed(rig.callbacks[0])


def test_user_cancel_with_ctrl_c(rig):
    rig.behaviour = "nothing"

    def tick() -> None:
        raise KeyboardInterrupt

    rig.io.tick = tick
    assert rig.run() == 1
    assert "Cancelled" in rig.text and rig.stored() == ""
    assert listener_closed(rig.callbacks[0])


def test_flood_of_stray_requests_stops_the_flow(rig, monkeypatch):
    monkeypatch.setattr(connect, "MAX_REJECTED", 3)
    rig.behaviour = "nothing"
    seen = {"n": 0}

    def tick() -> None:
        if seen["n"] < 5 and rig.callbacks:
            seen["n"] += 1
            threading.Thread(target=lambda: browse(rig.callbacks[0].rsplit("/", 1)[0] + "/nope"), daemon=True).start()

    rig.io.tick = tick
    assert rig.run(timeout_s=10) == 1
    assert "Too many unexpected requests" in rig.text and rig.stored() == ""


# ------------------------------------------------------------ OAuth: the exchange


@pytest.mark.parametrize("mode,words", [
    ("error", "HTTP 403"), ("oversize", "too large"), ("badjson", "not what Muse expected"), ("nokey", "not what Muse expected"), ("redirect", "redirect"),
])
def test_exchange_failures_save_nothing_and_never_echo_the_reply(tmp_path, monkeypatch, capsys, caplog, mode, words):
    r = Rig(tmp_path, monkeypatch, mode)
    try:
        assert r.run() == 1
        assert words in r.text and "Nothing was saved" in r.text and "muse keys set OPENROUTER_API_KEY" in r.text
        assert r.stored() == ""
        no_canary(r, capsys, caplog)
        if mode == "redirect":
            assert r.provider.paths == ["/exchange"], "the redirect target must not be requested"
    finally:
        r.close()


def test_exchange_refuses_non_https_endpoints_in_production(rig):
    http_only = connect.Endpoints("https://x.example/auth", "http://x.example/keys")
    with pytest.raises(connect.ConnectError):
        http_only.check()
    with pytest.raises(connect.ConnectError):
        connect.exchange_code(http_only, "c", "v")
    assert rig.run(endpoints=http_only) == 1 and "not https" in rig.text
    assert rig.opened == [] and rig.stored() == ""
    # the catalogue's real values are https
    prov = catalog.load_catalog().provider("openrouter")
    connect.Endpoints(prov.connection.auth_url, prov.connection.exchange_url).check()


def test_exchange_verifies_certificates_and_does_not_follow_redirects():
    opener = connect._opener(False)
    https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert https and https[0]._context.verify_mode == ssl.CERT_REQUIRED and https[0]._context.check_hostname is True
    redirects = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert redirects and all(isinstance(h, connect._NoRedirect) for h in redirects)
    assert connect._NoRedirect().redirect_request(None, None, 302, "", {}, "https://x") is None


def test_certificate_failure_is_reported_without_details(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("secret-hostname-detail"))

    import urllib.error

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    with pytest.raises(connect.ConnectError) as err:
        connect.exchange_code(connect.Endpoints("https://a.example/", "https://a.example/keys"), "c", "v")
    assert "certificate" in str(err.value) and "secret-hostname-detail" not in str(err.value)


def test_pkce_pair_is_s256():
    v, c = connect.pkce_pair()
    assert 43 <= len(v) <= 128 and c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert connect.pkce_pair()[0] != v


# ---------------------------------------------------------- OAuth: other modes


def test_paste_code_mode_has_no_listener_and_no_callback(rig, capsys, caplog):
    rig.provider.challenges[CODE] = ""  # filled in below once the URL is known

    def secret(prompt: str) -> str:
        url = next(line for line in rig.out.getvalue().splitlines() if line.startswith("http"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert "callback_url" not in q and "state" not in q and q["code_challenge_method"] == ["S256"]
        rig.provider.challenges[CODE] = q["code_challenge"][0]
        assert "(input is hidden)" in prompt
        return f"  {CODE}  "

    rig.io.secret = secret
    assert rig.run(paste_code=True) == 0
    assert f"OPENROUTER_API_KEY={CANARY}" in rig.stored() and rig.opened == []
    no_canary(rig, capsys, caplog)


def test_paste_code_needs_a_terminal_and_a_code(rig):
    assert rig.run(paste_code=True) == 1 and "no terminal" in rig.text.lower()
    rig.io.secret = lambda prompt: "  "
    assert rig.run(paste_code=True) == 1 and "Nothing was pasted" in rig.text and rig.stored() == ""


def test_no_browser_prints_the_link_and_does_not_open(rig, capsys, caplog):
    sent = {"done": False}

    def tick() -> None:
        if sent["done"]:
            return
        url = next((line for line in rig.out.getvalue().splitlines() if line.startswith("http://127.0.0.1")), None)
        if url:
            sent["done"] = True
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            rig.provider.challenges[CODE] = q["code_challenge"][0]
            threading.Thread(target=lambda: rig.statuses.append(browse(q["callback_url"][0], code=CODE, state=q["state"][0])), daemon=True).start()

    rig.io.tick = tick
    assert rig.run(no_browser=True) == 0
    assert rig.opened == [] and "open this link yourself" in rig.text
    assert f"OPENROUTER_API_KEY={CANARY}" in rig.stored()
    no_canary(rig, capsys, caplog)


def test_browser_that_cannot_open_falls_back_to_printing(rig):
    rig.io.open_url = lambda url: False
    rig.behaviour = "nothing"
    assert rig.run(timeout_s=0.5) == 1
    assert "open this link yourself" in rig.text


# --------------------------------------------------------- paste-key flow


GEMINI_KEY = "AIzaSyCANARY0123456789abcdefghijklmnopq"


class KeyRig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str | None = GEMINI_KEY) -> None:
        self.env_file = tmp_path / "dotenv"
        monkeypatch.setenv("MUSE_ENV_FILE", str(self.env_file))
        monkeypatch.setattr(keys, "_keyring", lambda: None)
        self.out, self.err = io.StringIO(), io.StringIO()
        self.prompts: list[str] = []
        self.texts: list[str] = []
        self.gets: list[tuple[str, dict]] = []
        self.opened: list[str] = []
        self.answers: dict[str, bool] = {}
        self.status = 200
        self.io = connect.ConnectIO(
            out=self.out, err=self.err, proxy_running=lambda: False, open_url=lambda u: self.opened.append(u) or True,
            secret=(lambda p: self.prompts.append(p) or secret) if secret is not None else None,
            text=lambda p: self.texts.append(p) or "0123456789abcdef0123456789abcdef",
            confirm=lambda p, d: self.answers.get("open" if ("key page" in p or "金鑰頁面" in p) else "shape", d),
            http_get=lambda url, headers, timeout: self.gets.append((url, dict(headers))) or self.status,
        )

    @property
    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def run(self, provider: str = "gemini", **kw) -> int:
        kw.setdefault("lang", "en")
        return connect.run_connect(self.io, {"MUSE_HOME": str(self.env_file.parent / "home")}, provider=provider, **kw)

    def stored(self) -> str:
        return self.env_file.read_text() if self.env_file.exists() else ""


@pytest.fixture()
def kr(tmp_path, monkeypatch):
    return KeyRig(tmp_path, monkeypatch)


def test_paste_key_guides_stores_and_never_prints_the_key(kr):
    assert kr.run() == 0
    t = kr.text
    assert "Muse only guides you. It never fills in or clicks anything on Gemini's website" in t
    assert "1. Sign in with your own Google account" in t and "2. Accept the terms" in t and "3. Copy the key" in t
    assert "Open this page yourself: https://aistudio.google.com/apikey" in t and kr.opened == []
    assert kr.prompts == ["Paste your Gemini key (input is hidden): "] and kr.texts == []  # hidden input; nothing visible asked
    assert f"GEMINI_API_KEY={GEMINI_KEY}" in kr.stored()
    assert GEMINI_KEY not in t and "CANARY" not in t
    assert "allowance not confirmed" in t  # no number is invented
    assert kr.gets == []  # nothing is sent without --test
    assert "add --test" in t.lower() and "muse up" in t and "Next best helper: OpenRouter" in t


def test_paste_key_in_chinese_and_opening_the_page(kr):
    kr.answers["open"] = True
    assert kr.run(lang="zh-TW") == 0
    assert kr.opened == ["https://aistudio.google.com/apikey"]
    assert "用你自己的 Google 帳號登入" in kr.text and "不會在 Gemini 的網站上替你填寫或點擊任何東西" in kr.text and "額度尚未確認" in kr.text


def test_open_flag_skips_the_question(kr):
    kr.io.confirm = None
    assert kr.run(open_page=True) == 0 and kr.opened == ["https://aistudio.google.com/apikey"]


def test_test_flag_sends_exactly_one_request_and_says_what(kr):
    assert kr.run(test=True) == 0
    assert len(kr.gets) == 1
    url, headers = kr.gets[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models" and headers == {"x-goog-api-key": GEMINI_KEY}
    assert "ONE request: GET https://generativelanguage.googleapis.com/v1beta/models" in kr.text
    assert "sends no prompt text" in kr.text and "accepted the key" in kr.text
    assert GEMINI_KEY not in kr.text


@pytest.mark.parametrize("status,words", [(401, "not accepted"), (429, "rate limited"), (500, "does not say")])
def test_test_results_are_explained(kr, status, words):
    kr.status = status
    assert kr.run(test=True) == 0 and words in kr.text and len(kr.gets) == 1
    assert f"GEMINI_API_KEY={GEMINI_KEY}" in kr.stored()  # saving never depends on the test


def test_bearer_test_for_groq_and_a_failed_test(tmp_path, monkeypatch):
    k = KeyRig(tmp_path, monkeypatch, secret="gsk_" + "a" * 40)
    assert k.run("groq", test=True) == 0
    assert k.gets[0][0] == "https://api.groq.com/openai/v1/models" and k.gets[0][1] == {"Authorization": "Bearer gsk_" + "a" * 40}
    (tmp_path / "b").mkdir()
    k2 = KeyRig(tmp_path / "b", monkeypatch, secret="gsk_" + "b" * 40)
    k2.io.http_get = lambda *a: (_ for _ in ()).throw(OSError("secret header echo gsk_bbbb"))
    assert k2.run("groq", test=True) == 0
    assert "could not reach" in k2.text and "secret header echo" not in k2.text and "gsk_bbbb" not in k2.text


def test_provider_without_a_defined_test_sends_nothing(tmp_path, monkeypatch):
    k = KeyRig(tmp_path, monkeypatch)
    assert k.run("cloudflare", test=True) == 0
    assert k.gets == [] and "No safe test is defined for Cloudflare Workers AI" in k.text
    # the account id was asked in the open, the token hidden, and both stored
    assert k.prompts == ["Paste your Cloudflare Workers AI key (input is hidden): "]
    assert k.texts == ["Paste CLOUDFLARE_ACCOUNT_ID (not a secret; shown as you type): "]
    stored = k.stored()
    assert f"CLOUDFLARE_API_KEY={GEMINI_KEY}" in stored and "CLOUDFLARE_ACCOUNT_ID=0123456789abcdef0123456789abcdef" in stored


def test_wrong_shape_asks_before_saving(kr):
    k = kr
    k.io.secret = lambda p: "not-a-google-key"
    assert k.run() == 1 and k.stored() == "" and "Not saved" in k.text
    k.answers["shape"] = True
    assert k.run() == 0 and "GEMINI_API_KEY=not-a-google-key" in k.stored()


def test_empty_paste_and_no_terminal(tmp_path, monkeypatch):
    k = KeyRig(tmp_path, monkeypatch)
    k.io.secret = lambda p: "   "
    assert k.run() == 1 and k.stored() == "" and "Nothing was pasted" in k.text
    (tmp_path / "x").mkdir()
    k2 = KeyRig(tmp_path / "x", monkeypatch, secret=None)
    assert k2.run() == 2 and "no terminal" in k2.text.lower() and k2.stored() == ""


def test_a_multiline_paste_is_refused(kr):
    kr.io.secret = lambda p: "AIzaSyline1\nline2"
    assert kr.run() == 1 and kr.stored() == "" and "Nothing was saved" in kr.text


def test_key_on_the_command_line_is_refused_and_not_echoed(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "dotenv"))
    for argv in (["connect", "gemini", GEMINI_KEY], ["connect", GEMINI_KEY], ["connect", "GEMINI_API_KEY=" + GEMINI_KEY], ["connect", "openrouter", CANARY]):
        assert cli.main(argv) == 2
        seen = capsys.readouterr()
        assert GEMINI_KEY not in seen.out + seen.err and CANARY not in seen.out + seen.err and "never takes a key on the command line" in seen.err
    assert not (tmp_path / "dotenv").exists()
    parser = cli.build_parser()
    dests = {a.dest for a in parser._subparsers._group_actions[0].choices["connect"]._actions}
    assert not ({"key", "token", "api_key", "value"} & dests)


def test_unknown_helper_is_not_echoed(capsys):
    assert cli.main(["connect", "nonsense"]) == 2
    seen = capsys.readouterr()
    assert "nonsense" not in seen.out + seen.err and "does not know that helper" in seen.err


# ------------------------------------------------------------- overview / others


def test_overview_lists_connected_and_ranked_suggestions(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "dotenv"))
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    # A key left in the process environment (OPENROUTER_API_KEY on a dev machine)
    # would mark that provider connected and change the ranking.
    for name in keys.KNOWN_KEYS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "dotenv").write_text("GROQ_API_KEY=gsk_x\n")
    assert cli.main(["connect", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "Connected helpers: Groq" in out and "1. OpenRouter: about 1 min, one tap" in out and "adds roughly 50 requests a day" in out
    assert "Steps: Approve on OpenRouter's own page > Come back here" in out and "Run: muse connect openrouter" in out
    assert "Groq: about" not in out and "Muse only guides you" in out
    assert "gsk_x" not in out
    assert cli.main(["connect", "--lang", "zh-TW"]) == 0
    assert "建議接下來新增的幫手" in capsys.readouterr().out
    assert cli.main(["connect", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["connected"] == ["groq"] and data["suggestions"][0]["id"] == "openrouter"


def test_muse_lang_environment_default(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "dotenv"))
    monkeypatch.setenv("MUSE_LANG", "zh-TW")
    assert cli.main(["connect"]) == 0
    assert "建議接下來新增的幫手" in capsys.readouterr().out


def test_connect_local_wraps_discovery(tmp_path, monkeypatch):
    from muse import local

    class P:
        provider, base, reachable, error, models = "ollama", "http://127.0.0.1:11434", True, None, ["m"]

    found = {"ollama": [{"id": "ollama-m", "model": "ollama_chat/m", "mode": "chat", "aliases": ["fast"]}]}
    monkeypatch.setattr(local, "probe_runtimes", lambda env: [P()])
    monkeypatch.setattr(local, "discover", lambda env: found)
    written = {}
    monkeypatch.setattr(local, "write_catalog", lambda f, path: written.setdefault("p", tmp_path / "local.yaml"))
    monkeypatch.setattr(local, "suggest_env", lambda f, env: {"OLLAMA_API_BASE": {"host": "http://127.0.0.1:11434", "docker": "http://host.docker.internal:11434"}})
    out = io.StringIO()
    cio = connect.ConnectIO(out=out, err=out, proxy_running=lambda: False)
    assert connect.run_connect(cio, {"MUSE_HOME": str(tmp_path)}, provider="local", lang="en") == 0
    assert "Found 1 model(s)" in out.getvalue() and "OLLAMA_API_BASE" in out.getvalue() and "p" in written
    out.truncate(0)
    written.clear()
    assert connect.run_connect(cio, {"MUSE_HOME": str(tmp_path)}, provider="local", lang="en", write_local=False) == 0
    assert not written and "--no-write" in out.getvalue()
    monkeypatch.setattr(local, "discover", lambda env: {})
    assert connect.run_connect(cio, {"MUSE_HOME": str(tmp_path)}, provider="local", lang="en") == 1


def test_resources_command(capsys):
    assert cli.main(["resources", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert "Other allowances (shown, not managed yet)" in out and "Hugging Face ZeroGPU: 5 GPU minutes a day" in out
    assert "Coinbase x402 payment facilitator: 1,000 transactions a month" in out and "Cloudflare R2 storage: 10 GB-month a month" in out
    assert "Your subscriptions (hand-off only)" in out and "Muse never signs in to these" in out and "Opens the plain page; a prefilled question is not confirmed." in out
    assert cli.main(["resources", "--lang", "zh-TW"]) == 0
    assert "你的訂閱（只能交接）" in capsys.readouterr().out
    assert cli.main(["resources", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["kind"] for r in data["resources"]} == {"storage", "compute", "chain", "messaging"} and all(r["managed"] is False for r in data["resources"])
    assert all(s["handoff_only"] for s in data["subscriptions"])
