"""`muse permissions grant | stop | status | sweep`, `muse hosts setup --permission`, and the tools server's
`check_permission` answering from a host's own permission. Needs biscuit-python for everything that signs;
skips cleanly without it."""

from __future__ import annotations

import io
import json
import sys

import pytest

from home_fakes import client_for, running
from wardmuse import apps, auth as proxy_auth, cli, keys
from wardmuse import permissions as P
from wardmuse.home import api
from wardmuse.home.auth import StaticVerifier
from wardmuse.mcp import tools

needs_library = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "NO_COLOR", "MUSE_APPS_FILE", "MUSE_PERMISSIONS"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MUSE_PROXY_URL", "http://127.0.0.1:4000")
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setitem(sys.modules, "keyring", None)


def run(argv, monkeypatch, tty=False):
    out, err = (TTY() if tty else io.StringIO()), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def refused(token: str) -> str | None:
    try:
        proxy_auth.authenticate(token, None, master_key="sk-master-for-tests")
    except proxy_auth.AuthError as exc:
        return exc.message if hasattr(exc, "message") else str(exc)
    return None


# ---------------------------------------------------------------- grant / status / stop / sweep


@needs_library
def test_grant_status_stop_and_sweep_from_the_command_line(monkeypatch):
    grant = ["permissions", "grant", "--holder", "app:reader", "--action", "llm.call", "--resource", "provider:router",
             "--limit", "requests:day:50", "--days", "3", "--yes"]
    code, out, err = run(grant, monkeypatch)
    assert code == 2 and "stdout is not a terminal" in err and "Nothing was issued" in err and apps.load_apps() == {}
    code, out, err = run(grant, monkeypatch, tty=True)
    assert code == 0, err
    token = next(w for w in out.split() if w.startswith(apps.TOKEN_PREFIX))
    assert out.count(token) == 1 and "COPY THE KEY NOW" in out and "Router app ✓, Records ✓" in out
    name = out.split("Issued ")[1].split()[0]
    app = apps.find_by_token(token)
    assert app.mandate.startswith("m1_" + name) and refused(token) is None

    code, out, err = run(["permissions", "status", "--json"], monkeypatch)
    status = json.loads(out)
    assert code == 0 and status["orphans"] == [] and status["skills"] == "advise"
    assert status["permissions"][0]["targets"]["router"]["state"] == "active" and token not in out
    code, out, _ = run(["permissions", "status"], monkeypatch)
    assert "Not built: payments, connectors, health" in out and "'advise' mode" in out

    code, out, err = run(["permissions", "stop", name, "--yes"], monkeypatch)
    assert code == 0 and f"Stopped {name}" in out and "Router app off" in out
    assert "permission stopped" in refused(token)
    code, out, err = run(["permissions", "sweep", "--json"], monkeypatch)
    assert code == 0 and json.loads(out)["failed"] == 0
    assert run(["permissions", "stop", "nosuch", "--yes"], monkeypatch)[0] == 1


@needs_library
def test_a_plain_revoke_by_hand_stops_the_router_app_at_once(monkeypatch):
    code, out, _ = run(["permissions", "grant", "--holder", "app:reader", "--action", "llm.call", "--resource", "provider:router",
                        "--yes", "--show-token"], monkeypatch)
    token = next(w for w in out.split() if w.startswith(apps.TOKEN_PREFIX))
    name = out.split("Issued ")[1].split()[0]
    assert refused(token) is None
    assert run(["permissions", "revoke", name, "--yes"], monkeypatch)[0] == 0  # no sweep in between
    assert "permission stopped" in refused(token)


def test_status_without_the_library_or_a_key_says_so(monkeypatch):
    if P.available():
        code, out, _ = run(["permissions", "status"], monkeypatch)
        assert code == 0 and "No permissions yet" in out
    else:
        code, out, _ = run(["permissions", "status"], monkeypatch)
        assert code == 0 and "biscuit-python" in out and "nothing is checked" in out


# ---------------------------------------------------------------- hosts setup offers a permission


@needs_library
def test_hosts_setup_with_a_permission_ties_the_hosts_key_to_it(monkeypatch):
    code, out, err = run(["hosts", "setup", "openmausbot", "--permission"], monkeypatch, tty=True)
    assert code == 0, err
    token = next(w for w in out.split() if w.startswith(apps.TOKEN_PREFIX))
    assert "Gave this host the permission" in out
    app = apps.load_apps()["openmausbot"]
    assert app.mandate and app.allow_paid is False and refused(token) is None  # the same app, adopted, not a second one
    from wardmuse.compiler.core import Compiler

    m = Compiler().find(app.mandate)
    assert m.draft.holder == "device:openmausbot-tools" and "llm.paid" not in m.draft.actions
    assert not any(a.startswith("files.") for a in m.draft.actions) and "skills.run" not in m.draft.routine
    name = m.id[3:15]
    assert f"muse permissions stop {name}" in out
    assert run(["permissions", "stop", name, "--yes"], monkeypatch)[0] == 0
    assert "permission stopped" in refused(token)


def test_hosts_setup_without_a_permission_says_how_to_add_one(monkeypatch):
    code, out, err = run(["hosts", "setup", "openmausbot", "--no-permission"], monkeypatch, tty=True)
    assert code == 0 and "This host has NO permission yet" in out and apps.load_apps()["openmausbot"].mandate is None


# ---------------------------------------------------------------- check_permission from the host's own permission


@needs_library
def test_check_permission_answers_from_the_hosts_permission_and_falls_back_to_the_labelled_interim_rule(monkeypatch):
    from wardmuse.compiler import hosts as host_permission
    from wardmuse.compiler.core import Compiler

    home = api.Home(proxy_running=lambda: False)
    verifier = StaticVerifier({"bot-tools": "tok-host", "phone": "tok-phone"}, roles={"bot-tools": "host"})
    with running(home, verifier) as srv:
        ctx = tools.Context(home=tools.HomeLink(client_for(srv, "tok-host")))
        ctx.allowed_read = ("/home/me/notes",)
        before, err = tools.call(ctx, "check_permission", {"action": "read", "resource": "/home/me/notes/a.md"})
        assert not err and before["basis"] == "interim rule" and before["decision"] == "allow"
        c = Compiler()
        draft = host_permission.default_draft(c.ensure_root(), "bot-tools", now=c.now())
        mandate, _ = c.issue(draft)
        got, err = tools.call(ctx, "check_permission", {"action": "read", "resource": "/home/me/notes/a.md"})
        assert not err and got["basis"] == "permission" and got["decision"] == "ask" and got["enforced"] is False
        got, _ = tools.call(ctx, "check_permission", {"action": "run"})
        assert got["basis"] == "permission" and got["decision"] == "ask"  # skills.run is asked about each time
        c.revoke(mandate.id)
        got, _ = tools.call(ctx, "check_permission", {"action": "read", "resource": "/home/me/notes/a.md"})
        assert got["basis"] == "permission" and got["decision"] == "deny" and "Do not do this" in got["what_to_do"]
        ctx.stop.set()
