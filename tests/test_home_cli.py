"""`muse home serve|token|call` and `python -m muse.home` through their entry points."""

from __future__ import annotations

import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from home_fakes import ROOT, running, short_dir
from muse import cli
from muse.home import api, auth
from muse.home import cli as home_cli
from muse.home.client import HomeClient

KEY_CANARY = "gsk_CANARYkey0123456789abcdefABCDEF"


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv(home_cli.TOKEN_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)


def run(argv, monkeypatch, tty=False, stdin=None):
    out, err = (TTY() if tty else io.StringIO()), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def token_in(text: str) -> str:
    found = re.findall(r"sk-muse-dev-[A-Za-z0-9_-]+", text)
    assert len(found) == 1, text
    return found[0]


# ---------------------------------------------------------------- tokens


def test_token_add_shows_the_token_once_in_a_terminal(monkeypatch):
    code, out, err = run(["home", "token", "add", "mei-phone"], monkeypatch, tty=True)
    assert code == 0 and "Added device 'mei-phone'" in out and "shown once" in out and "Authorization: Bearer" in out
    token = token_in(out)
    assert auth.TokenFileVerifier().verify(token).name == "mei-phone"
    code, out, err = run(["home", "token", "list"], monkeypatch, tty=True)
    assert code == 0 and "mei-phone" in out and "sk-muse-dev-" not in out and auth.hash_token(token) not in out
    code, out, err = run(["home", "token", "list", "--json"], monkeypatch)
    assert [d["name"] for d in json.loads(out)] == ["mei-phone"] and set(json.loads(out)[0]) == {"name", "role", "created_at", "rotated_at"}
    assert json.loads(out)[0]["role"] == "owner" and "with the role owner" in run(["home", "token", "add", "desk"], monkeypatch, tty=True)[1]


@pytest.mark.parametrize("verb", ["add", "rotate"])
def test_a_token_is_not_printed_into_a_pipe_unless_asked(monkeypatch, verb):
    if verb == "rotate":
        auth.add_token("mei-phone")
    before = auth.tokens_path().read_text() if auth.tokens_path().exists() else None
    code, out, err = run(["home", "token", verb, "mei-phone"], monkeypatch)
    assert code == 1 and out == "" and "Refusing to print a new device token" in err and "Nothing was created or changed" in err
    assert (auth.tokens_path().read_text() if auth.tokens_path().exists() else None) == before  # checked before anything changed
    code, out, err = run(["home", "token", verb, "mei-phone", "--show-token"], monkeypatch)
    assert code == 0 and auth.TokenFileVerifier().verify(token_in(out)).name == "mei-phone"


def test_token_rotate_and_rm(monkeypatch):
    _, old = auth.add_token("mei-phone")
    code, out, _ = run(["home", "token", "rotate", "mei-phone"], monkeypatch, tty=True)
    new = token_in(out)
    assert code == 0 and new != old and "old one no longer works" in out
    v = auth.TokenFileVerifier()
    assert v.verify(old) is None and v.verify(new) is not None
    code, out, _ = run(["home", "token", "rm", "mei-phone"], monkeypatch)
    assert code == 0 and "no longer works" in out and v.verify(new) is None
    code, out, _ = run(["home", "token", "list"], monkeypatch)
    assert "No devices" in out


@pytest.mark.parametrize("argv, words", [
    (["home", "token", "add", "Bad Name"], "device name must be"),
    (["home", "token", "rotate", "ghost"], "no device named 'ghost'"),
    (["home", "token", "rm", "ghost"], "no device named 'ghost'"),
])
def test_token_errors(monkeypatch, argv, words):
    code, out, err = run(argv, monkeypatch, tty=True)
    assert code == 1 and words in err and "sk-muse-dev-" not in out + err


def test_adding_the_same_device_twice_is_refused(monkeypatch):
    run(["home", "token", "add", "one"], monkeypatch, tty=True)
    code, out, err = run(["home", "token", "add", "one"], monkeypatch, tty=True)
    assert code == 1 and "already exists" in err and "sk-muse-dev-" not in out


def test_home_without_a_subcommand_prints_help(monkeypatch):
    code, out, err = run(["home"], monkeypatch)
    assert code == 2 and "serve" in err and "token" in err and "call" in err
    code, out, err = run(["home", "token"], monkeypatch)
    assert code == 2


def test_the_other_commands_are_still_there(monkeypatch):
    parser = cli.build_parser()
    for argv in (["dash", "--no-open", "--port", "4180"], ["pause"], ["resume"], ["status"], ["apps", "list"], ["connect"]):
        assert parser.parse_args(argv).func is not None
    assert parser.parse_args(["dash"]).port == 4180  # `muse dash` is unchanged


# ---------------------------------------------------------------- call


@pytest.fixture()
def served(monkeypatch):
    """A real home on a short socket path, and a device token in the environment."""
    _, token = auth.add_token("laptop")
    home = api.Home(proxy_running=lambda: False)
    with short_dir() as folder, running(home, auth.TokenFileVerifier(), socket_path=folder / "home.sock") as srv:
        monkeypatch.setenv(home_cli.TOKEN_ENV, token)
        yield srv, token


def test_call_sends_one_request_and_prints_the_reply(monkeypatch, served):
    srv, token = served
    sock = ["--socket", str(srv.socket_path)]
    code, out, err = run(["home", "call", "GET", "/v1/status", *sock], monkeypatch)
    assert code == 0 and json.loads(out)["api_version"] == "1" and token not in out + err
    code, out, err = run(["home", "call", "post", "/v1/pause", *sock], monkeypatch)
    assert code == 0 and json.loads(out) == {"paused": True, "recorded": True}
    code, out, err = run(["home", "call", "POST", "/v1/apps", "--data", '{"name": "one", "priority": "normal"}', *sock], monkeypatch)
    assert code == 0 and json.loads(out)["app"]["name"] == "one"
    code, out, err = run(["home", "call", "GET", "/v1/nope", *sock], monkeypatch)
    assert code == 1 and json.loads(out)["error"]["code"] == "not_found" and "HTTP 404" in err


def test_call_reads_a_body_from_stdin_or_a_file_and_keeps_keys_off_the_command_line(monkeypatch, served, tmp_path):
    srv, _ = served
    sock = ["--socket", str(srv.socket_path)]
    code, out, err = run(["home", "call", "POST", "/v1/keys/groq", "--data", json.dumps({"key": KEY_CANARY}), *sock], monkeypatch)
    assert code == 2 and "must not be typed on the command line" in err and KEY_CANARY not in out + err
    assert not Path(os.environ["MUSE_ENV_FILE"]).exists()
    code, out, err = run(["home", "call", "POST", "/v1/keys/groq", "--data", "-", *sock], monkeypatch, stdin=json.dumps({"key": KEY_CANARY}))
    assert code == 0 and json.loads(out)["stored"] == ["GROQ_API_KEY"] and KEY_CANARY not in out + err
    assert KEY_CANARY in Path(os.environ["MUSE_ENV_FILE"]).read_text()
    body = tmp_path / "job.json"
    body.write_text(json.dumps({"prompt": "hello"}))
    code, out, err = run(["home", "call", "POST", "/v1/jobs", "--data", f"@{body}", *sock], monkeypatch)
    assert code == 0 and json.loads(out)["job"]["status"] == "queued"
    code, out, err = run(["home", "call", "POST", "/v1/jobs", "--data", "{not json", *sock], monkeypatch)
    assert code == 2 and "not valid JSON" in err


def test_call_without_a_token_or_a_home_says_what_to_do(monkeypatch, served):
    srv, token = served
    monkeypatch.delenv(home_cli.TOKEN_ENV)
    code, out, err = run(["home", "call", "GET", "/v1/status", "--socket", str(srv.socket_path)], monkeypatch, stdin="")
    assert code == 2 and "no device token" in err and home_cli.TOKEN_ENV in err
    monkeypatch.setenv(home_cli.TOKEN_ENV, "wrong")
    code, out, err = run(["home", "call", "GET", "/v1/status", "--socket", str(srv.socket_path)], monkeypatch)
    assert code == 1 and json.loads(out)["error"]["code"] == "unauthorized"
    monkeypatch.setenv(home_cli.TOKEN_ENV, token)
    code, out, err = run(["home", "call", "GET", "/v1/status", "--socket", str(srv.socket_path) + ".gone"], monkeypatch)
    assert code == 1 and "cannot reach the home" in err and "muse home serve" in err


def test_call_asks_for_the_token_hidden_in_a_terminal(monkeypatch, served):
    import getpass

    srv, token = served
    monkeypatch.delenv(home_cli.TOKEN_ENV)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": token)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", TTY())
    assert cli.main(["home", "call", "GET", "/v1/cards", "--socket", str(srv.socket_path)]) == 0
    assert json.loads(out.getvalue())["cards"] == [] and token not in out.getvalue() + err.getvalue()


# ---------------------------------------------------------------- serve


def wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


@pytest.mark.parametrize("entry", [["-m", "muse", "home", "serve"], ["-m", "muse.home"]])
def test_serve_listens_on_the_private_socket_until_stopped(entry, tmp_path):
    with short_dir() as muse_home:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "MUSE_HOME": str(muse_home)}
        _, token = auth.add_token("laptop", environ=env)
        proc = subprocess.Popen([sys.executable, *entry], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        sock = muse_home / "home.sock"
        try:
            wait_for(sock)
            assert oct(sock.stat().st_mode & 0o777) == "0o600" and oct(muse_home.stat().st_mode & 0o777) == "0o700"
            client = HomeClient(token, socket_path=sock, timeout=10)
            status, snap = client.call("GET", "/v1/status")
            assert status == 200 and snap["api_version"] == "1" and snap["home"] == str(muse_home)
            assert HomeClient("wrong", socket_path=sock, timeout=10).call("GET", "/v1/status")[0] == 401
            second = subprocess.run([sys.executable, *entry], capture_output=True, text=True, env=env, timeout=60)
            assert second.returncode == 1 and "already running" in second.stderr
            assert client.call("GET", "/v1/status")[0] == 200
        finally:
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        assert f"unix:{sock}" in out and token not in out + err
        assert not sock.exists()


def test_serve_says_when_no_device_can_sign_in_and_tcp_is_loopback(tmp_path):
    with short_dir() as muse_home:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "MUSE_HOME": str(muse_home)}
        proc = subprocess.Popen([sys.executable, "-m", "muse", "home", "serve", "--tcp", "0"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            seen = ""
            while "http://127.0.0.1:" not in seen:
                line = proc.stdout.readline()
                assert line, proc.stderr.read()
                seen += line
            port = int(re.search(r"http://127\.0\.0\.1:(\d+)", seen).group(1))
            assert "No device can sign in yet" in seen and "muse home token add" in seen and "any program on this computer" in seen
            assert HomeClient("anything", tcp_port=port, timeout=10).call("GET", "/v1/status")[0] == 401
            assert not (muse_home / "home.sock").exists()
        finally:
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=20)
        assert proc.returncode == 0


def test_serve_reports_a_path_it_cannot_use(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-socket"
    blocker.write_text("x")
    code, out, err = run(["home", "serve", "--socket", str(blocker)], monkeypatch)
    assert code == 1 and "Could not start the home" in err and "not a socket" in err


def test_token_add_takes_a_role_and_list_shows_it(monkeypatch):
    code, out, err = run(["home", "token", "add", "bot-tools", "--role", "host"], monkeypatch, tty=True)
    assert code == 0 and "with the role host" in out and "cannot pause or resume" in out
    assert auth.TokenFileVerifier().verify(token_in(out)).role == "host"
    assert run(["home", "token", "add", "wall", "--role", "viewer"], monkeypatch, tty=True)[0] == 0
    assert run(["home", "token", "add", "mei-phone"], monkeypatch, tty=True)[0] == 0  # no --role: owner, as before roles
    code, out, err = run(["home", "token", "list"], monkeypatch, tty=True)
    lines = {line.split()[0]: line.split()[1] for line in out.splitlines() if line.strip()}
    assert lines == {"bot-tools": "host", "mei-phone": "owner", "wall": "viewer"}
    code, out, err = run(["home", "token", "list", "--json"], monkeypatch)
    assert {d["name"]: d["role"] for d in json.loads(out)} == lines
    assert run(["home", "token", "add", "x", "--role", "admin"], monkeypatch, tty=True)[0] == 2
    assert "x" not in {d.name for d in auth.list_devices()}


@pytest.mark.parametrize("stop, ignore_interrupt", [(signal.SIGTERM, False), (signal.SIGINT, True)])
def test_serve_stops_cleanly_on_terminate_and_when_started_detached(stop, ignore_interrupt, tmp_path):
    """A service manager sends SIGTERM; a detached start (nohup, a background job) inherits SIGINT ignored."""
    with short_dir() as muse_home:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1", "MUSE_HOME": str(muse_home)}
        before = (lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)) if ignore_interrupt else None
        proc = subprocess.Popen([sys.executable, "-m", "muse", "home", "serve"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, preexec_fn=before)
        sock = muse_home / "home.sock"
        try:
            wait_for(sock)
        finally:
            proc.send_signal(stop)
            out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        assert not sock.exists()
