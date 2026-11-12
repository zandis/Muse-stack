"""muse.cli: rendering against stub snapshots, key commands, doctor, ask, up."""

from __future__ import annotations

import copy
import io
import json
import socket
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from muse import cli, keys

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
SECRET = "gsk_CLISECRETVALUE0987654321"


def iso(delta: timedelta) -> str:
    return (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in list(keys.KNOWN_KEYS) + ["OLLAMA_API_BASE", "LM_STUDIO_API_BASE", "MUSE_LOCAL_OPENAI_BASE", "MUSE_BIND", "NO_COLOR", "MUSE_DEFAULT_PRIVACY"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MUSE_LOCAL_CATALOG", str(tmp_path / "local.yaml"))
    monkeypatch.setenv("MUSE_PROXY_URL", f"http://127.0.0.1:{free_port()}")
    monkeypatch.setitem(sys.modules, "keyring", None)
    return tmp_path


POLICY_TRAINS = {"trains_on_inputs": "yes", "human_review": "yes", "region": "us", "commercial_use": "allowed", "terms_url": "", "note": ""}
POLICY_NO = {**POLICY_TRAINS, "trains_on_inputs": "no", "human_review": "no"}
POLICY_UNKNOWN = {**POLICY_TRAINS, "trains_on_inputs": "unknown", "human_review": "unknown"}


def provider(pid, *, state="ok", configured=True, kind="cloud", tier="free", policy=POLICY_TRAINS, limits=None, usage=None, blocked_until=None, block_reason=None, deployments=None, key_env="X_API_KEY"):
    return {
        "id": pid,
        "kind": kind,
        "tier": tier,
        "key_env": None if kind == "local" else key_env,
        "configured": configured,
        "enabled": True,
        "policy": policy,
        "state": state,
        "blocked_until": blocked_until,
        "block_reason": block_reason,
        "usage": usage or {"minute": {"requests": 0, "tokens": 0}, "day": {"requests": 0, "tokens": 0}, "month": {"requests": 0, "tokens": 0}},
        "limits": limits or [],
        "deployments": deployments or [],
    }


def dep(did, aliases, state="ok", blocked_until=None):
    return {"id": did, "model": "m", "mode": "chat", "aliases": aliases, "state": state, "blocked_until": blocked_until, "last_status": 200, "last_used_at": None, "success_rate_24h": 1.0}


def snapshot(providers, aliases, recent=None, reachable=True):
    return {
        "generated_at": iso(timedelta(0)),
        "home": "/home/u/.muse",
        "proxy": {"url": "http://127.0.0.1:4000", "reachable": reachable},
        "default_privacy": "standard",
        "providers": providers,
        "aliases": aliases,
        "recent": recent or [],
        "totals": {"requests_today": sum(p["usage"]["day"]["requests"] for p in providers), "tokens_today": 0},
    }


def empty_snapshot():
    provs = [provider("gemini", state="no_key", configured=False, key_env="GEMINI_API_KEY"), provider("groq", state="no_key", configured=False, key_env="GROQ_API_KEY"), provider("ollama", state="no_key", configured=False, kind="local", tier="local", policy=POLICY_NO)]
    return snapshot(provs, [{"name": "fast", "mode": "chat", "available": 0, "total": 0, "next": None}], reachable=None)


def typical_snapshot():
    day = {"requests": 42, "tokens": 123456}
    gem = provider(
        "gemini",
        usage={"minute": {"requests": 1, "tokens": 10}, "day": day, "month": {"requests": 300, "tokens": 5_000_000}},
        limits=[
            {"window": "day", "metric": "requests", "limit": 500, "remaining": 458, "reset_at": iso(timedelta(hours=3, minutes=12, seconds=20)), "source": "estimate"},
            {"window": "minute", "metric": "tokens", "limit": 250000, "remaining": 240000, "reset_at": iso(timedelta(seconds=40)), "source": "header"},
        ],
        deployments=[dep("gemini-3.8-flash", ["smart", "fast"])],
        key_env="GEMINI_API_KEY",
    )
    groq = provider(
        "groq",
        state="cooling",
        blocked_until=iso(timedelta(minutes=4, seconds=5)),
        block_reason="429 rate limit",
        policy=POLICY_UNKNOWN,
        limits=[{"window": "day", "metric": "requests", "limit": None, "remaining": None, "reset_at": None, "source": "catalog"}],
        deployments=[dep("groq-gpt-oss-20b", ["fast", "cheap"], "cooling", iso(timedelta(minutes=4, seconds=5)))],
        key_env="GROQ_API_KEY",
    )
    ollama = provider("ollama", kind="local", tier="local", policy=POLICY_NO, deployments=[dep("ollama-qwen3-8b", ["fast", "cheap"])])
    cohere = provider("cohere", state="no_key", configured=False, tier="trial", policy=POLICY_NO, key_env="COHERE_API_KEY")
    recent = [
        {"ts": iso(timedelta(minutes=-1)), "alias": "fast", "deployment": "groq-gpt-oss-20b", "provider": "groq", "status": 200, "ok": True, "latency_ms": 412, "tokens_in": 1200, "tokens_out": 340, "privacy": "standard"},
        {"ts": iso(timedelta(minutes=-2)), "alias": None, "deployment": "gemini-3.8-flash-with-a-very-long-deployment-name", "provider": "gemini", "status": 429, "ok": False, "latency_ms": None, "tokens_in": None, "tokens_out": None, "privacy": "private"},
    ]
    aliases = [
        {"name": "fast", "mode": "chat", "available": 4, "total": 6, "next": "groq-gpt-oss-20b"},
        {"name": "smart", "mode": "chat", "available": 1, "total": 1, "next": "gemini-3.8-flash"},
        {"name": "rerank", "mode": "rerank", "available": 0, "total": 0, "next": None},
    ]
    return snapshot([gem, groq, ollama, cohere], aliases, recent)


def blocked_snapshot():
    soon = iso(timedelta(minutes=9, seconds=30))
    later = iso(timedelta(hours=26))
    gem = provider("gemini", state="exhausted", blocked_until=later, block_reason="daily quota", deployments=[dep("gemini-3.8-flash", ["fast"], "exhausted", later)], limits=[{"window": "day", "metric": "requests", "limit": 500, "remaining": 0, "reset_at": later, "source": "header"}], key_env="GEMINI_API_KEY")
    groq = provider("groq", state="cooling", blocked_until=soon, block_reason="429", deployments=[dep("groq-gpt-oss-20b", ["fast"], "cooling", soon)], key_env="GROQ_API_KEY")
    return snapshot([gem, groq], [{"name": "fast", "mode": "chat", "available": 0, "total": 2, "next": None}])


def plain(text: str) -> str:
    return cli.strip_ansi(text)


@pytest.mark.parametrize("make", [empty_snapshot, typical_snapshot, blocked_snapshot])
def test_status_fits_80_columns_and_has_no_escape_when_plain(make):
    text = cli.render_status(make(), color=False, now=NOW)
    assert "\x1b" not in text
    assert max(len(line) for line in text.splitlines()) <= 80
    colored = cli.render_status(make(), color=True, now=NOW)
    assert "\x1b[" in colored
    assert max(len(line) for line in plain(colored).splitlines()) <= 80
    assert [l.rstrip() for l in plain(colored).splitlines()] == [l.rstrip() for l in text.splitlines()]


def test_status_ascii_fallback_fits_too():
    text = cli.render_status(typical_snapshot(), color=False, now=NOW, unicode_ok=False)
    assert "█" not in text and "#" in text


def test_status_empty_prints_getting_started():
    text = cli.render_status(empty_snapshot(), now=NOW)
    assert "muse keys set GEMINI_API_KEY" in text and "muse up" in text
    assert "(no calls recorded yet)" in text
    assert "not checked" in text


def test_status_typical_content():
    text = cli.render_status(typical_snapshot(), now=NOW)
    assert "muse keys set" not in text  # configured: no onboarding
    gem = next(line for line in text.splitlines() if line.startswith("gemini"))
    assert "trains" in gem and "42 req 123k tok" in gem and "free" in gem
    assert "resets in 3h 12m" in text
    assert "resets in 40s" in text
    assert "~458/500 left" in text and "240k/250k left" in text  # ~ marks estimates only
    assert "~ = estimate" in text
    groq = next(line for line in text.splitlines() if line.startswith("groq"))
    assert "cooling" in groq and "unknown" in groq
    assert "unblocks in 4m 05s" in text
    assert "limit unknown" in text
    ollama = next(line for line in text.splitlines() if line.startswith("ollama"))
    assert "local" in ollama
    assert "no-train" in next(line for line in text.splitlines() if line.startswith("cohere"))
    assert "fast          4/6 available  next: groq-gpt-oss-20b" in text
    assert "smart" in text and "rerank" not in text.split("ALIASES")[1]
    assert "200" in text and "412ms" in text and "1.2k>340" in text


def test_status_all_blocked_shows_soonest():
    text = cli.render_status(blocked_snapshot(), now=NOW)
    assert "Everything is blocked right now. Soonest: groq-gpt-oss-20b in 9m 30s" in text.replace("  ", " ") or "Soonest: groq" in text
    assert "0/2 available  soonest: groq-gpt-oss-20b in 9m" in text
    assert "unblocks in 1d 2h" in text


@pytest.mark.parametrize(
    "delta,expected",
    [(timedelta(seconds=-5), "now"), (timedelta(seconds=30), "30s"), (timedelta(minutes=5, seconds=7), "5m 07s"), (timedelta(minutes=45), "45m"), (timedelta(hours=3, minutes=12), "3h 12m"), (timedelta(days=2, hours=5), "2d 5h")],
)
def test_countdown(delta, expected):
    assert cli.countdown(iso(delta), NOW) == expected
    assert cli.countdown(None, NOW) is None


def test_privacy_badges():
    assert cli.privacy_badge({"kind": "local", "policy": POLICY_TRAINS}) == "local"
    assert cli.privacy_badge({"kind": "cloud", "policy": POLICY_TRAINS}) == "trains"
    assert cli.privacy_badge({"kind": "cloud", "policy": POLICY_NO}) == "no-train"
    assert cli.privacy_badge({"kind": "cloud", "policy": POLICY_UNKNOWN}) == "unknown"
    assert cli.privacy_badge({"kind": "cloud"}) == "unknown"


def test_no_color_and_tty(monkeypatch):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    assert cli._color_enabled(TTY()) is True
    assert cli._color_enabled(io.StringIO()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli._color_enabled(TTY()) is False


def stub_status(monkeypatch, snap):
    calls = []
    mod = types.ModuleType("muse.status")

    def build_snapshot(now=None, environ=None, check_proxy=False):
        calls.append({"check_proxy": check_proxy})
        return copy.deepcopy(snap)

    mod.build_snapshot = build_snapshot
    monkeypatch.setitem(sys.modules, "muse.status", mod)
    return calls


def test_status_command_plain_and_json(monkeypatch, capsys):
    snap = typical_snapshot()
    calls = stub_status(monkeypatch, snap)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "PROVIDER" in out and "\x1b" not in out
    assert max(len(line) for line in out.splitlines()) <= 80
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == snap
    assert calls


def test_status_when_snapshot_missing_or_broken(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "muse.status", None)  # import fails
    assert cli.main(["status"]) == 1
    assert "status.py" in capsys.readouterr().err
    mod = types.ModuleType("muse.status")
    mod.build_snapshot = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setitem(sys.modules, "muse.status", mod)
    assert cli.main(["status"]) == 1


def test_usage_errors_exit_2(capsys):
    assert cli.main([]) == 2
    assert cli.main(["nonsense"]) == 2
    assert cli.main(["ask"]) == 2
    assert cli.main(["--help"]) == 0


# ------------------------------------------------------------------- keys


def test_keys_set_refuses_argv_value(tmp_path, capsys):
    assert cli.main(["keys", "set", "GROQ_API_KEY", SECRET]) == 2
    assert cli.main(["keys", "set", f"GROQ_API_KEY={SECRET}"]) == 2
    assert not (tmp_path / ".env").exists()
    assert "KEY=value" in capsys.readouterr().err


def test_keys_set_stdin_and_list_never_show_value(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"  {SECRET}\n"))
    assert cli.main(["keys", "set", "GROQ_API_KEY", "--stdin"]) == 0
    assert "Saved GROQ_API_KEY" in capsys.readouterr().out
    assert cli.main(["keys", "list"]) == 0
    out = capsys.readouterr().out
    assert "GROQ_API_KEY" in out and "dotenv" in out
    assert cli.main(["keys", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert next(r for r in rows if r["name"] == "GROQ_API_KEY")["configured"] is True
    assert SECRET not in out + json.dumps(rows)
    assert SECRET in (tmp_path / ".env").read_text()


def test_keys_set_prompts_with_getpass_and_hints(monkeypatch, capsys):
    class TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", TTY())
    prompts = []
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": (prompts.append(prompt), "wrong-shape-key-0123456789")[1])
    assert cli.main(["keys", "set", "GROQ_API_KEY"]) == 0
    cap = capsys.readouterr()
    assert prompts and "wrong-shape" not in cap.out + cap.err + prompts[0]
    assert "gsk_" in cap.err  # shape warning, not blocking


def test_keys_set_without_tty_or_stdin_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(SECRET))
    assert cli.main(["keys", "set", "GROQ_API_KEY"]) == 2
    assert "--stdin" in capsys.readouterr().err


def test_keys_unknown_name_rm_open(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("abcdefghijklmnop"))
    assert cli.main(["keys", "set", "MYSTERY", "--stdin"]) == 1
    assert "--force" in capsys.readouterr().err
    assert cli.main(["keys", "open", "GROQ_API_KEY"]) == 0
    assert capsys.readouterr().out.strip() == "https://console.groq.com/keys"
    assert cli.main(["keys", "rm", "GROQ_API_KEY"]) == 0
    assert "not stored" in capsys.readouterr().out


# ----------------------------------------------------------------- doctor


def healthy_env(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", keys.generate_master_key())
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)


def test_doctor_fresh_machine_fails_with_hints(capsys):
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "✗ No master key set" in out and "fix:" in out
    assert "✗ No provider keys" in out
    assert "Python" in out


def test_doctor_healthy_has_exit_0_and_json(monkeypatch, capsys):
    healthy_env(monkeypatch)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ Master key set (env)" in out and "! Proxy not reachable" in out and "! Docker not found" in out
    assert SECRET not in out
    assert cli.main(["doctor", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and {c["status"] for c in data["checks"]} <= {"ok", "warn", "fail"}
    assert any(c["id"] == "unverified" and c["status"] == "warn" for c in data["checks"])


def test_doctor_weak_master_key_and_bind_and_perms(tmp_path, monkeypatch, capsys):
    healthy_env(monkeypatch)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-1234")
    monkeypatch.setenv("MUSE_BIND", "0.0.0.0")
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    env.chmod(0o644)
    assert cli.main(["doctor", "--json"]) == 1
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["master_key"]["status"] == "fail"
    assert checks["bind"]["status"] == "warn" and "127.0.0.1" in checks["bind"]["fix"]
    assert checks["dotenv"]["status"] == "warn" and "chmod 600" in checks["dotenv"]["fix"]


def test_doctor_duplicate_account_warning(monkeypatch, capsys):
    healthy_env(monkeypatch)
    monkeypatch.setenv("JINA_API_KEY", SECRET)  # same value under two providers
    monkeypatch.setenv("GROQ_API_KEY_2", "other-account")
    assert cli.main(["doctor", "--json"]) == 0
    checks = {c["id"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    acc = checks["accounts"]
    assert acc["status"] == "warn" and "one account per provider" in acc["detail"]
    assert "GROQ_API_KEY = JINA_API_KEY" in acc["detail"] and "GROQ_API_KEY_2" in acc["detail"]
    assert SECRET not in json.dumps(acc)


class FakeProxy:
    """Loopback server impersonating the proxy. `handler(method, path, headers, body) -> (status, headers, body)`."""

    def __init__(self, handler):
        outer = self
        self.requests: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def _do(self, method):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n) if n else b""
                outer.requests.append({"method": method, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, headers, payload = handler(method, self.path, self.headers, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._do("GET")

            def do_POST(self):  # noqa: N802
                self._do("POST")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxy(monkeypatch):
    made = []

    def make(handler):
        p = FakeProxy(handler)
        made.append(p)
        monkeypatch.setenv("MUSE_PROXY_URL", p.url)
        return p

    yield make
    for p in made:
        p.close()


def test_doctor_proxy_requires_auth_vs_open(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)

    def locked(method, path, headers, body):
        return (200, {}, {"ok": 1}) if path.startswith("/health") else (401, {}, {"error": "no key"})

    proxy(locked)
    assert cli.main(["doctor", "--json"]) == 0
    check = next(c for c in json.loads(capsys.readouterr().out)["checks"] if c["id"] == "proxy")
    assert check["status"] == "ok" and "requires auth" in check["title"]

    def open_(method, path, headers, body):
        return (200, {}, {"data": []})

    proxy(open_)
    cli.main(["doctor", "--json"])
    check = next(c for c in json.loads(capsys.readouterr().out)["checks"] if c["id"] == "proxy")
    assert check["status"] == "warn" and "unauthenticated" in check["title"]


# -------------------------------------------------------------------- ask


def chat_ok(method, path, headers, body):
    return 200, {"x-muse-provider": "groq", "x-muse-deployment": "groq-gpt-oss-20b", "X-LiteLLM-Response-Cost": "0", "x-other": "no"}, {"choices": [{"message": {"content": "hello there"}}]}


def test_ask_success_sends_headers_and_shows_route(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    p = proxy(chat_ok)
    assert cli.main(["ask", "hi", "--alias", "smart", "--privacy", "private", "--show-route"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "hello there"
    assert "x-muse-provider: groq" in cap.err and "x-muse-deployment: groq-gpt-oss-20b" in cap.err and "x-litellm-response-cost" in cap.err
    assert "x-other" not in cap.err
    req = p.requests[0]
    assert req["headers"]["x-muse-privacy"] == "private"
    assert req["headers"]["authorization"].startswith("Bearer sk-muse-")
    body = json.loads(req["body"])
    assert body["model"] == "smart" and body["messages"][0]["content"] == "hi"
    assert SECRET not in cap.out + cap.err


def test_ask_model_overrides_alias(monkeypatch, proxy):
    healthy_env(monkeypatch)
    p = proxy(chat_ok)
    assert cli.main(["ask", "hi", "--model", "groq-gpt-oss-20b"]) == 0
    assert json.loads(p.requests[0]["body"])["model"] == "groq-gpt-oss-20b"
    assert "x-muse-privacy" not in p.requests[0]["headers"]


def test_ask_401_names_master_key(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    proxy(lambda m, p, h, b: (401, {}, {"error": {"message": "Authentication Error"}}))
    assert cli.main(["ask", "hi"]) == 1
    err = capsys.readouterr().err
    assert "master key" in err and "LITELLM_MASTER_KEY" in err


def test_ask_429_shows_soonest_reset(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    proxy(lambda m, p, h, b: (429, {}, {"error": {"message": "rate limited"}}))
    snap = blocked_snapshot()
    now = datetime.now(timezone.utc)
    snap["providers"][1]["blocked_until"] = (now + timedelta(minutes=7, seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap["providers"][1]["deployments"][0]["blocked_until"] = snap["providers"][1]["blocked_until"]
    stub_status(monkeypatch, snap)
    assert cli.main(["ask", "hi"]) == 1
    err = capsys.readouterr().err
    assert "rate-limited or blocked" in err and "Soonest reset: groq-gpt-oss-20b in 7m" in err


def test_ask_429_without_snapshot_still_clear(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    proxy(lambda m, p, h, b: (429, {}, {}))
    monkeypatch.setitem(sys.modules, "muse.status", None)
    assert cli.main(["ask", "hi"]) == 1
    assert "muse status" in capsys.readouterr().err


def test_ask_policy_refusal_names_class(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    proxy(lambda m, p, h, b: (403, {}, {"error": {"message": "privacy policy: no provider is allowed for class 'private'"}}))
    assert cli.main(["ask", "hi", "--privacy", "private"]) == 1
    err = capsys.readouterr().err
    assert "privacy policy" in err and "'private'" in err and "local model" in err


def test_ask_other_errors_and_unreachable(monkeypatch, proxy, capsys):
    healthy_env(monkeypatch)
    proxy(lambda m, p, h, b: (500, {}, b"<html>oops</html>"))
    assert cli.main(["ask", "hi"]) == 1
    assert "HTTP 500" in capsys.readouterr().err
    proxy(lambda m, p, h, b: (200, {}, {"weird": True}))
    assert cli.main(["ask", "hi"]) == 1
    monkeypatch.setenv("MUSE_PROXY_URL", f"http://127.0.0.1:{free_port()}")
    assert cli.main(["ask", "hi"]) == 1
    assert "muse up" in capsys.readouterr().err


# --------------------------------------------------------------- up / env


def test_up_generates_master_key_without_printing_and_forwards_keychain_keys(tmp_path, monkeypatch, capsys):
    store = {}
    kr = types.ModuleType("keyring")
    kr.get_keyring = lambda: types.SimpleNamespace(priority=5)
    kr.get_password = lambda s, n: store.get((s, n))
    kr.set_password = lambda s, n, v: store.__setitem__((s, n), v)
    kr.delete_password = lambda s, n: store.pop((s, n))
    monkeypatch.setitem(sys.modules, "keyring", kr)
    store[(keys.SERVICE, "GROQ_API_KEY")] = SECRET
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    ran = {}

    def fake_run(cmd, cwd=None, env=None, check=False):
        ran.update(cmd=cmd, env=env)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["up"]) == 0
    cap = capsys.readouterr()
    assert ran["cmd"] == ["docker", "compose", "up", "-d"]
    master = store[(keys.SERVICE, "LITELLM_MASTER_KEY")]
    assert master.startswith("sk-muse-")
    assert master not in cap.out + cap.err and SECRET not in cap.out + cap.err
    assert "stored it in OS keychain" in cap.out
    assert ran["env"]["GROQ_API_KEY"] == SECRET and ran["env"]["LITELLM_MASTER_KEY"] == master
    assert ran["env"]["OLLAMA_API_BASE"] == "http://host.docker.internal:11434"
    assert not (tmp_path / ".env").exists()  # nothing written to disk
    # docker-compose.yml passes every catalogue key through, so there is nothing to warn about
    assert cli.compose_gaps(ran["env"]) == [] and "GROQ_API_KEY:" not in cap.err


def test_up_show_master_key_prints_once_and_second_run_does_not_regenerate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert cli.main(["up", "--show-master-key"]) == 0
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("LITELLM_MASTER_KEY=sk-muse-"))
    first = line.split("=", 1)[1]
    assert first in (tmp_path / ".env").read_text()
    assert cli.main(["up"]) == 0
    assert "Generated" not in capsys.readouterr().out
    assert cli.main(["up", "--show-master-key"]) == 0
    assert first in capsys.readouterr().out


def test_up_native_needs_litellm(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda n: None)
    assert cli.main(["up", "--native"]) == 1
    assert "litellm" in capsys.readouterr().err


def test_up_docker_missing(monkeypatch, capsys):
    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli.main(["up"]) == 1
    assert "Docker" in capsys.readouterr().err


def test_env_never_prints_the_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-muse-ENVKEYSHOULDNOTAPPEAR")
    assert cli.main(["env"]) == 0
    out = capsys.readouterr().out
    assert "ENVKEYSHOULDNOTAPPEAR" not in out
    assert "export OPENAI_BASE_URL=http://127.0.0.1:" in out and "${LITELLM_MASTER_KEY" in out
    assert cli.main(["env", "--show-master-key"]) == 0
    assert "OPENAI_API_KEY=sk-muse-ENVKEYSHOULDNOTAPPEAR" in capsys.readouterr().out


def test_dash_calls_serve(monkeypatch):
    seen = {}
    mod = types.ModuleType("muse.dashboard.server")
    mod.serve = lambda host="127.0.0.1", port=4180: seen.update(host=host, port=port)
    monkeypatch.setitem(sys.modules, "muse.dashboard.server", mod)
    assert cli.main(["dash", "--port", "5000", "--no-open"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 5000}


def test_local_discover_and_list_cli(tmp_path, monkeypatch, capsys):
    assert cli.main(["local", "list"]) == 0
    assert "No local models" in capsys.readouterr().out
    assert cli.main(["local", "discover", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["found"] == {} and len(data["runtimes"]) == 2
    from muse import local

    monkeypatch.setattr(local, "discover", lambda env=None: {"ollama": [{"id": "ollama-a", "model": "ollama_chat/a", "mode": "chat", "aliases": ["fast"], "notes": ""}]})
    assert cli.main(["local", "discover", "--write"]) == 0
    out = capsys.readouterr().out
    assert "OLLAMA_API_BASE" not in out or "docker" in out
    assert (tmp_path / "local.yaml").exists()
    assert cli.main(["local", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ollama"][0]["id"] == "ollama-a"


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out.startswith("muse 0.")
