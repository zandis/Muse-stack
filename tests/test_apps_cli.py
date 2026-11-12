"""`muse apps ...`, presets, `muse pause|resume`, and `muse queue ...` through cli.main."""

from __future__ import annotations

import io
import json
import socket
import sys
from pathlib import Path

import pytest

from muse import apps, cli, keys, presets, queue
from muse.ledger import Ledger
from queue_fakes import CANARY_PROMPT, CANARY_REPLY, FakeProxy


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "NO_COLOR"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setenv("MUSE_PROXY_URL", "http://127.0.0.1:4000")
    monkeypatch.setitem(sys.modules, "keyring", None)


def run(argv, monkeypatch, tty=False, stdin=None):
    out, err = (TTY() if tty else io.StringIO()), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def home_bytes() -> bytes:
    return b"".join(p.read_bytes() for p in Path(cli.paths.home({})).rglob("*") if p.is_file())


# ---------------------------------------------------------------- apps add


def test_add_prints_the_token_once_with_settings_on_a_terminal(monkeypatch):
    code, out, err = run(["apps", "add", "notes", "--priority", "background", "--daily-requests", "300", "--note", "nightly"], monkeypatch, tty=True)
    assert code == 0, err
    token = next(w for w in out.split() if w.startswith(apps.TOKEN_PREFIX))
    assert out.count(token) == 2  # once as the key, once in the environment line: the same single issue
    assert "COPY THE KEY NOW" in out and "http://127.0.0.1:4000/v1" in out and "OPENAI_API_KEY" in out
    app = apps.load_apps()["notes"]
    assert app.priority == "background" and app.daily_requests == 300 and app.allow_paid is False and app.note == "nightly"
    assert apps.find_by_token(token).name == "notes"
    assert token.encode() not in home_bytes()  # only the hash is stored
    assert token not in err


def test_add_refuses_when_stdout_is_not_a_terminal_and_creates_nothing(monkeypatch):
    code, out, err = run(["apps", "add", "notes"], monkeypatch)
    assert code == 2 and out == "" and "Nothing was created" in err and "--show-token" in err
    assert apps.load_apps() == {}


def test_add_with_show_token_works_when_piped_and_json(monkeypatch):
    code, out, err = run(["apps", "add", "bot", "--show-token", "--json", "--daily-tokens", "1000"], monkeypatch)
    data = json.loads(out)
    assert code == 0 and data["token"].startswith(apps.TOKEN_PREFIX) and data["base_url"] == "http://127.0.0.1:4000/v1"
    assert data["daily_tokens"] == 1000 and apps.find_by_token(data["token"]).name == "bot"
    code, out, err = run(["apps", "add", "bot", "--show-token"], monkeypatch)
    assert code == 1 and "already exists" in err and apps.apps_path().read_text().count("name: bot") == 1


def test_add_bad_input(monkeypatch):
    for argv in (["apps", "add", "Bad Name", "--show-token"], ["apps", "add", "owner", "--show-token"],
                 ["apps", "add", "x", "--show-token", "--daily-requests", "-5"]):
        code, out, err = run(argv, monkeypatch)
        assert code == 1 and "Error" in err and apps.TOKEN_PREFIX not in out
    assert apps.load_apps() == {}


def test_allow_paid_flag_both_ways(monkeypatch):
    run(["apps", "add", "a", "--priority", "background", "--allow-paid", "--show-token"], monkeypatch)
    run(["apps", "add", "b", "--no-paid", "--show-token"], monkeypatch)
    assert apps.load_apps()["a"].allow_paid is True and apps.load_apps()["b"].allow_paid is False


# ---------------------------------------------------------------- list / set / rm / rotate


def test_list_json_has_usage_caps_and_deferrals(monkeypatch):
    run(["apps", "add", "notes", "--priority", "background", "--daily-requests", "10", "--show-token"], monkeypatch)
    ledger = Ledger()
    for _ in range(3):
        ledger.record_ration(app="notes", priority="background", alias="cheap", kind="defer", code="yield")
    ledger.record_ration(app="notes", priority="background", alias="cheap", kind="refuse", code="app-requests")
    ledger.record_call(deployment_id="a", provider="freea", upstream="freea:a", alias="cheap", status=200, ok=True, tokens_in=5, tokens_out=5, app="notes", priority="background")
    code, out, _ = run(["apps", "list", "--json"], monkeypatch)
    rows = {r["name"]: r for r in json.loads(out)}
    assert code == 0 and list(rows)[:2] == ["owner", "anonymous"] and rows["owner"]["builtin"] and rows["anonymous"]["builtin"]
    n = rows["notes"]
    assert n["today"]["requests"] == 1 and n["today"]["tokens"] == 10 and n["daily_requests"] == 10 and n["allow_paid"] is False
    assert n["deferrals"] == {"defer:yield": 3, "refuse:app-requests": 1}
    code, text, _ = run(["apps", "list"], monkeypatch)
    assert "(built in)" in text and "3x waited for an app in use" in text and "1x hit its request cap" in text
    assert all(len(line) <= 80 for line in text.splitlines())


def test_set_changes_and_clears(monkeypatch):
    run(["apps", "add", "a", "--daily-requests", "5", "--daily-tokens", "9", "--privacy", "private", "--show-token"], monkeypatch)
    code, out, err = run(["apps", "set", "a", "--priority", "background", "--no-daily-requests", "--daily-tokens", "100", "--privacy", "none", "--note", "hi"], monkeypatch)
    app = apps.load_apps()["a"]
    assert code == 0 and (app.priority, app.daily_requests, app.daily_tokens, app.default_privacy, app.note) == ("background", None, 100, None, "hi")
    assert run(["apps", "set", "a"], monkeypatch)[0] == 2
    assert run(["apps", "set", "ghost", "--note", "x"], monkeypatch)[0] == 1


def test_rm_and_rotate(monkeypatch):
    _, out, _ = run(["apps", "add", "a", "--show-token", "--json"], monkeypatch)
    old = json.loads(out)["token"]
    code, out, err = run(["apps", "rotate", "a"], monkeypatch)
    assert code == 2 and apps.find_by_token(old) is not None  # refused before anything changed
    code, out, _ = run(["apps", "rotate", "a", "--show-token", "--json"], monkeypatch)
    new = json.loads(out)["token"]
    assert code == 0 and new != old and apps.find_by_token(old) is None and apps.find_by_token(new).name == "a"
    code, out, _ = run(["apps", "rotate", "a"], monkeypatch, tty=True)
    assert code == 0 and "old key no longer works" in out
    assert run(["apps", "rm", "a"], monkeypatch)[0] == 0
    assert run(["apps", "rm", "a"], monkeypatch)[0] == 1 and apps.load_apps() == {}
    assert run(["apps", "rotate", "a", "--show-token"], monkeypatch)[0] == 1


# ---------------------------------------------------------------- presets


@pytest.mark.parametrize("kind", list(presets.PRESETS))
def test_every_preset_creates_an_app_and_prints_exact_settings(kind, monkeypatch):
    p = presets.PRESETS[kind]
    code, out, err = run(["apps", "preset", "--for", kind], monkeypatch, tty=True)
    assert code == 0, err
    token = next(w for w in out.replace("'", " ").replace("}", " ").split() if w.startswith(apps.TOKEN_PREFIX))
    app = apps.load_apps()[kind]
    assert (app.priority, app.daily_requests, app.daily_tokens) == (p.priority, p.daily_requests, p.daily_tokens)
    assert "http://127.0.0.1:4000/v1" in out and p.alias in out and token in out
    assert ("UNVERIFIED" in out) == (not p.verified) and ("Checked against" in out) == p.verified
    assert "{" not in out.split("Settings for")[1].replace("${MUSE_APP_KEY}", "")


def test_preset_defaults_follow_the_kind_of_tool(monkeypatch):
    assert presets.PRESETS["cline"].priority == presets.PRESETS["continue"].priority == presets.PRESETS["aider"].priority == "normal"
    assert all(presets.PRESETS[k].daily_tokens for k in ("cline", "continue", "aider"))
    assert presets.PRESETS["n8n"].priority == presets.PRESETS["script"].priority == "background"
    assert presets.PRESETS["open-webui"].priority == "interactive"
    assert presets.PRESETS["n8n"].verified is False
    assert "${MUSE_APP_KEY}" in presets.hint(presets.PRESETS["librechat"], "http://x/v1", "sk-k")


def test_preset_overrides_name_and_caps(monkeypatch):
    code, out, _ = run(["apps", "preset", "my-agent", "--for", "aider", "--daily-tokens", "50000", "--show-token", "--json"], monkeypatch)
    assert code == 0 and json.loads(out)["model"] == "smart"
    assert apps.load_apps()["my-agent"].daily_tokens == 50000
    assert run(["apps", "preset", "--for", "nonsense"], monkeypatch)[0] == 2
    code, _, err = run(["apps", "preset", "--for", "script"], monkeypatch)
    assert code == 2 and "Nothing was created" in err and "script" not in apps.load_apps()


# ---------------------------------------------------------------- pause / resume / queue CLI


def test_pause_and_resume(monkeypatch):
    code, out, _ = run(["pause"], monkeypatch)
    assert code == 0 and "proxy keeps answering" in out and queue.is_paused()
    assert "PAUSED" in run(["status"], monkeypatch)[1]
    code, out, _ = run(["resume"], monkeypatch)
    assert code == 0 and not queue.is_paused() and "not paused" in run(["resume"], monkeypatch)[1]


def test_queue_commands_end_to_end(tmp_path, monkeypatch):
    out_file = tmp_path / "r.txt"
    code, out, err = run(["queue", "add", CANARY_PROMPT, "--alias", "fast", "--privacy", "private", "--out", str(out_file), "--by", "2h"], monkeypatch)
    assert code == 0, err
    jid = out.split()[1]
    assert CANARY_PROMPT not in out  # the prompt is not echoed back
    code, out, _ = run(["queue", "list", "--json"], monkeypatch)
    rows = json.loads(out)
    assert rows[0]["id"] == jid and rows[0]["status"] == "queued" and rows[0]["deadline"] and "payload" not in rows[0]
    assert CANARY_PROMPT not in out
    code, out, _ = run(["queue", "show", jid], monkeypatch)
    assert "queued" in out and CANARY_PROMPT not in out and run(["queue", "show", "ffffff"], monkeypatch)[0] == 1
    proxy = FakeProxy()
    try:
        monkeypatch.setenv("MUSE_PROXY_URL", proxy.url)
        code, out, err = run(["queue", "run", "--once"], monkeypatch)
        assert code == 0 and "1 done" in err and proxy.requests[0]["model"] == "fast"
        assert CANARY_PROMPT not in err and CANARY_REPLY not in err and CANARY_REPLY not in out
    finally:
        proxy.stop()
    code, out, _ = run(["queue", "show", jid, "--output"], monkeypatch)
    assert out.strip() == CANARY_REPLY
    assert run(["queue", "clear"], monkeypatch)[0] == 2
    assert "Cleared 1" in run(["queue", "clear", "--done"], monkeypatch)[1] and out_file.exists()


def test_queue_add_from_stdin_cancel_and_errors(monkeypatch, tmp_path):
    code, out, _ = run(["queue", "add", "-"], monkeypatch, stdin="from stdin")
    jid = out.split()[1]
    assert code == 0 and run(["queue", "cancel", jid], monkeypatch)[0] == 0
    assert run(["queue", "cancel", jid], monkeypatch)[0] == 1
    assert run(["queue", "add", "   "], monkeypatch)[0] == 1
    assert run(["queue", "add", "x", "--by", "whenever"], monkeypatch)[0] == 1
    assert run(["queue", "add", "x", "--privacy", "secret"], monkeypatch)[0] == 2


def test_queue_add_file_and_directory(tmp_path, monkeypatch):
    d = tmp_path / "docs"
    d.mkdir()
    for i in range(3):
        (d / f"n{i}.txt").write_text(f"note {i}")
    (d / "pic.bin").write_bytes(b"\x00\x01")
    outd = tmp_path / "out"
    code, out, err = run(["queue", "add-file", str(d), "--do", "summarise", "--out", str(outd)], monkeypatch)
    assert code == 0 and "Queued 3 jobs" in out and "skipped" in out
    assert len(json.loads(run(["queue", "list", "--json"], monkeypatch)[1])) == 3
    code, _, err = run(["queue", "add-file", str(d), "--do", "x", "--max-files", "2"], monkeypatch)
    assert code == 1 and "limit is 2" in err
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * 200_000)
    assert run(["queue", "add-file", str(big), "--do", "x"], monkeypatch)[0] == 1
    f = tmp_path / "one.txt"
    f.write_text("hello")
    assert run(["queue", "add-file", str(f), "--do", "translate to French"], monkeypatch)[0] == 0
    assert run(["queue", "add-file", str(tmp_path / "nope.txt"), "--do", "x"], monkeypatch)[0] == 1


def test_queue_add_batch(tmp_path, monkeypatch):
    f = tmp_path / "p.txt"
    f.write_text("one\n---\ntwo")
    code, out, _ = run(["queue", "add-batch", str(f)], monkeypatch)
    assert code == 0 and "2 prompts" in out
    assert json.loads(run(["queue", "list", "--json"], monkeypatch)[1])[0]["kind"] == "batch"


def test_queue_run_refuses_a_second_runner_and_reports_pause(monkeypatch):
    with queue.runner_lock(None) as got:
        assert got
        code, _, err = run(["queue", "run", "--once"], monkeypatch)
    assert code == 1 and "already running" in err
    queue.set_paused()
    code, _, err = run(["queue", "run", "--once"], monkeypatch)
    assert code == 0 and "paused" in err
