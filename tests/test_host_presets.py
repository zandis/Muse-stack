"""Host presets, `muse hosts setup|show|check`, and the guides in docs/hosts/.

The sample configuration files below are written from the format the host's own
documentation describes (see muse/presets.py for the sources). No host was
installed: nothing here shows that a host works with Muse end to end.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import sys
from datetime import date
from pathlib import Path

import pytest

from muse import apps, cli, keys, presets
from muse.home import auth
from muse.mcp import hosts, server

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:4000/v1"
KEY_CANARY = "sk-CANARYhostkey0123456789abcdefABCDEF"
OTHER_CANARY = "gsk_CANARYother0123456789abcdefABCDEF"
MUSE_KEY = apps.TOKEN_PREFIX + "CANARYmuseAppKey0123456789abcdefXYZ"


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "NO_COLOR", "MUSE_APPS_FILE"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MUSE_PROXY_URL", "http://127.0.0.1:4000")
    monkeypatch.setitem(sys.modules, "keyring", None)


def run(argv, monkeypatch, tty=False):
    out, err = (TTY() if tty else io.StringIO()), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def write(tmp_path: Path, data, name: str = "config.json") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8")
    return path


def muse_instance(**over):
    entry = {"driver": "openai-compat", "displayName": "Muse", "environment": {"MUSE_APP_KEY": MUSE_KEY},
             "config": {"url": BASE, "apiKeyEnv": "MUSE_APP_KEY", "model": "auto"}}
    entry.update(over)
    return entry


TOOLS_ENTRY = {"command": "/usr/bin/python3", "args": ["-m", "muse.mcp", "--token-file", "/home/me/.muse/host-openmausbot.token"]}


# ---------------------------------------------------------------- the presets are well-formed and dated


def test_host_presets_are_well_formed_and_dated():
    assert list(presets.HOSTS)[0] == "openmausbot"
    checked = date(2026, 11, 13)
    assert presets.HOSTS_CHECKED == "13 November 2026"
    for kind, host in presets.HOSTS.items():
        assert host.kind == kind and apps.NAME_RE.fullmatch(kind) and auth.NAME_RE.fullmatch(hosts.device_name(kind))
        assert host.priority == "interactive" and host.priority in apps.PRIORITIES and host.allow_paid is False
        assert host.tools and host.after and host.facts and host.vendor_engines and host.config_file
        assert host.check_format in ("openmausbot", "mcp-json")
        assert host.unverified, "every host says what was not run or not confirmed"
        assert any("not" in line and ("run" in line or "installed" in line) for line in host.unverified)
        if host.verified is not None:
            assert date.fromisoformat(host.verified) == checked and host.sources
            assert all(url.startswith("https://") for url in host.sources)
        assert any("muse.mcp" in line and "--token-file" in line for line in host.tools)
        assert not any("pip install" in line for line in (*host.engine, *host.tools, *host.after))


def test_the_openmausbot_preset_uses_the_field_names_its_docs_give():
    host = presets.HOSTS["openmausbot"]
    engine, tools_block = "\n".join(host.engine), "\n".join(host.tools)
    for field in ('"instances"', '"driver": "openai-compat"', '"displayName"', '"environment"', '"config"', '"url"', '"apiKeyEnv"', '"model"'):
        assert field in engine, field
    for field in ('"mcpServers"', '"command"', '"args"'):
        assert field in tools_block, field
    assert host.config_file == "~/.openmausbot/config.json" and host.verified == "2026-11-13"
    assert {u.rsplit("/", 1)[1] for u in host.sources} == {"custom-engines.md", "custom-mcp-servers.md", "README.md"}
    assert "claude, codex and grok" in host.vendor_engines
    code = presets.HOSTS["claude-code"]
    assert code.engine == () and "claude mcp add --transport stdio" in code.tools[0] and " -- " in code.tools[0]


def test_host_presets_do_not_change_the_app_presets():
    assert not set(presets.HOSTS) & set(presets.PRESETS)
    assert set(presets.HOST_MODELS) == {"auto", "fast", "cheap", "smart", "long-context"}
    with pytest.raises(KeyError):
        presets.get_host("nonsense")


# ---------------------------------------------------------------- the snippet


@pytest.mark.parametrize("kind", list(presets.HOSTS))
def test_the_printed_snippet_holds_no_secret(kind):
    from muse.catalog import find_secretlike

    host = presets.HOSTS[kind]
    text = hosts.snippet(host, BASE, "/home/me/.muse/host-x.token", python="/usr/bin/python3")
    assert find_secretlike(text.split()) == [] and apps.TOKEN_PREFIX not in text and auth.TOKEN_PREFIX not in text
    assert "/usr/bin/python3" in text and "-m" in text and "muse.mcp" in text and "--token-file" in text
    assert "/home/me/.muse/host-x.token" in text
    assert ("Field names checked against the host's own pages" in text) == bool(host.verified)
    assert ("UNVERIFIED" in text) == (not host.verified)
    assert "Not run or not verified here" in text
    if host.engine:
        assert BASE in text and presets.HOST_APP_KEY_ENV in text and presets.KEY_PLACEHOLDER in text
        assert "auto (Muse picks by task), or fast, cheap, smart, long-context" in text
    else:
        assert BASE not in text and "No engine entry" in text


def test_the_openmausbot_snippet_is_json_the_checker_itself_accepts(tmp_path):
    host = presets.HOSTS["openmausbot"]
    text = hosts.snippet(host, BASE, r"C:\Users\me\.muse\host.token", python=r"C:\Python\python.exe")
    blocks = []
    for label in ("The engine", "The tools"):
        body = text.split(label)[1].split("\n\n")[0].split("\n", 1)[1]
        body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("Model names"))
        blocks.append(json.loads("{" + body + "}"))  # each block is one member of the config object
    merged = {**blocks[0], **blocks[1]}
    assert merged["instances"]["muse"]["config"] == {"url": BASE, "apiKeyEnv": "MUSE_APP_KEY", "model": "auto"}
    assert merged["mcpServers"]["muse-tools"] == {"command": r"C:\Python\python.exe",
                                                  "args": ["-m", "muse.mcp", "--token-file", r"C:\Users\me\.muse\host.token"]}
    merged["instances"]["muse"]["environment"]["MUSE_APP_KEY"] = MUSE_KEY  # the one thing the person pastes in
    report = hosts.check_config(host, write(tmp_path, merged), BASE)
    assert report.exit_code == 0 and report.muse_engine is True and report.tools_server is True


# ---------------------------------------------------------------- muse hosts setup / show / list


def test_setup_creates_the_app_and_the_tools_token_and_prints_what_to_paste(monkeypatch, tmp_path):
    code, out, err = run(["hosts", "setup", "openmausbot"], monkeypatch, tty=True)
    assert code == 0, err
    app = apps.load_apps()["openmausbot"]
    assert (app.priority, app.allow_paid, app.daily_requests, app.daily_tokens) == ("interactive", False, None, None)
    token = next(w for w in out.split() if w.startswith(apps.TOKEN_PREFIX))
    assert out.count(token) == 1 and apps.find_by_token(token).name == "openmausbot" and "COPY THE KEY NOW" in out
    # The tools device exists; its token is in a private file and was never printed.
    assert [d.name for d in auth.list_devices()] == ["openmausbot-tools"]
    token_file = Path(os.environ["MUSE_HOME"]) / "host-openmausbot.token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    device_token = token_file.read_text(encoding="utf-8").strip()
    assert device_token.startswith(auth.TOKEN_PREFIX) and device_token not in out + err and auth.TOKEN_PREFIX not in out + err
    assert auth.TokenFileVerifier().verify(device_token).name == "openmausbot-tools"
    assert server.read_token_file(token_file) == device_token  # exactly what the tools server will accept
    # The settings: Muse's address, the env var name, the model names, and the tools server command.
    settings = out.split("Settings for OpenMausBot")[1]
    assert token not in settings and presets.KEY_PLACEHOLDER in settings
    for want in (BASE, '"apiKeyEnv": "MUSE_APP_KEY"', '"model": "auto"', "fast, cheap, smart, long-context", sys.executable,
                 '"-m", "muse.mcp", "--token-file"', str(token_file), "~/.openmausbot/config.json"):
        assert want in settings, want
    assert "Muse did not touch the host's files" in out and "docs/hosts/README.md" in out and "muse hosts check openmausbot" in out
    assert not (tmp_path / ".openmausbot").exists() and not (Path.home() / ".openmausbot" / "config.json.muse").exists()


def test_setup_refuses_to_print_a_key_into_a_pipe_and_creates_nothing(monkeypatch):
    code, out, err = run(["hosts", "setup", "openmausbot"], monkeypatch)
    assert code == 2 and "Nothing was created" in err and apps.TOKEN_PREFIX not in out + err
    assert "openmausbot" not in apps.load_apps() and auth.list_devices() == []
    assert not (Path(os.environ["MUSE_HOME"]) / "host-openmausbot.token").exists()
    code, out, err = run(["hosts", "setup", "openmausbot", "--show-token"], monkeypatch)
    assert code == 0 and "openmausbot" in apps.load_apps()


def test_setup_twice_changes_nothing_the_second_time(monkeypatch):
    assert run(["hosts", "setup", "openmausbot"], monkeypatch, tty=True)[0] == 0
    token_file = Path(os.environ["MUSE_HOME"]) / "host-openmausbot.token"
    before = (token_file.read_bytes(), apps.load_apps()["openmausbot"].token_sha256)
    code, out, err = run(["hosts", "setup", "openmausbot"], monkeypatch, tty=True)
    assert code == 1 and "already exists" in err and "Nothing was created or changed" in err and apps.TOKEN_PREFIX not in out
    assert (token_file.read_bytes(), apps.load_apps()["openmausbot"].token_sha256) == before
    # A second host of the same kind under another name is fine.
    code, out, err = run(["hosts", "setup", "openmausbot", "--app-name", "omb-vm"], monkeypatch, tty=True)
    assert code == 0 and "omb-vm" in apps.load_apps() and sorted(d.name for d in auth.list_devices()) == ["omb-vm-tools", "openmausbot-tools"]
    for bad in ("Bad Name", "muse", "x" * 39):
        code, _, err = run(["hosts", "setup", "openmausbot", "--app-name", bad], monkeypatch, tty=True)
        assert code == 1 and "Nothing was created" in err


def test_setup_leaves_no_working_token_behind_when_the_file_cannot_be_written(monkeypatch):
    blocker = Path(os.environ["MUSE_HOME"])
    blocker.mkdir(parents=True, exist_ok=True)
    (blocker / "host-openmausbot.token").symlink_to(blocker / "elsewhere")  # a dangling link: never written through
    code, out, err = run(["hosts", "setup", "openmausbot"], monkeypatch, tty=True)
    assert code == 1 and "already exists" in err and auth.list_devices() == [] and "openmausbot" not in apps.load_apps()
    assert not (blocker / "elsewhere").exists()


def test_a_tools_only_host_gets_no_app_and_needs_no_terminal(monkeypatch):
    code, out, err = run(["hosts", "setup", "claude-code"], monkeypatch)
    assert code == 0, err
    assert apps.load_apps().keys().isdisjoint({"claude-code"}) and [d.name for d in auth.list_devices()] == ["claude-code-tools"]
    assert "claude mcp add --transport stdio muse-tools -- " in out and "host-claude-code.token" in out
    assert apps.TOKEN_PREFIX not in out and auth.TOKEN_PREFIX not in out and "No engine entry" in out


def test_show_prints_the_settings_again_and_creates_nothing(monkeypatch):
    code, out, err = run(["hosts", "show", "openmausbot"], monkeypatch)
    assert code == 0 and BASE in out and presets.KEY_PLACEHOLDER in out and "host-openmausbot.token" in out
    assert apps.load_apps() == {} or "openmausbot" not in apps.load_apps()
    assert auth.list_devices() == [] and not Path(os.environ["MUSE_HOME"], "host-openmausbot.token").exists()


def test_list_and_usage(monkeypatch):
    code, out, _ = run(["hosts", "list"], monkeypatch)
    assert code == 0 and "openmausbot" in out and "claude-code" in out and "not run here" in out and "2026-11-13" in out
    code, out, _ = run(["hosts", "list", "--json"], monkeypatch)
    listed = json.loads(out)
    assert [h["name"] for h in listed] == list(presets.HOSTS) and all(h["not_verified"] for h in listed)
    assert run(["hosts"], monkeypatch)[0] == 2 and run(["mcp"], monkeypatch)[0] == 2
    assert run(["hosts", "setup", "nonsense"], monkeypatch)[0] == 2


# ---------------------------------------------------------------- muse hosts check (read-only)


def check(tmp_path, data, kind="openmausbot"):
    path = write(tmp_path, data)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    report = hosts.check_config(presets.HOSTS[kind], path, BASE)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before  # read-only
    text = report.text() + json.dumps(report.to_dict())
    assert "CANARY" not in text  # nothing key-like from the file is ever shown
    return report


def test_check_good_config(tmp_path):
    report = check(tmp_path, {"instances": {"muse": muse_instance()}, "mcpServers": {"muse-tools": TOOLS_ENTRY}})
    assert report.exit_code == 0 and report.muse_engine is True and report.tools_server is True and report.warnings == []
    text = report.text()
    assert "Engine 'muse' points at Muse (http://127.0.0.1:4000/v1)." in text
    assert "sends a Muse app key from MUSE_APP_KEY (not shown)" in text
    assert "The Muse tools server is registered as 'muse-tools'." in text
    # Said every time, because this file cannot show it:
    assert "claude, codex and grok command-line tools" in text and hosts.NOT_INVOLVED in text
    assert report.secrets_seen >= 1 and "look like keys or tokens. None is shown here" in text


@pytest.mark.parametrize("url", ["http://localhost:4000/v1", "http://127.0.0.1:4000/v1/", "http://host.docker.internal:4000/v1",
                                 "HTTP://127.0.0.1:4000/v1"])
def test_check_accepts_the_other_spellings_of_muses_address(tmp_path, url):
    cfg = muse_instance()
    cfg["config"]["url"] = url
    assert check(tmp_path, {"instances": {"muse": cfg}}).muse_engine is True


def test_check_missing_muse_engine(tmp_path):
    for data in ({"instances": {}}, {"mcpServers": {"muse-tools": TOOLS_ENTRY}}, {"features": {"browser": True}}, {}):
        report = check(tmp_path, data)
        assert report.readable and report.muse_engine is False and report.exit_code == 1
        assert f"No engine in this file points at Muse ({BASE})" in report.text()
        assert "muse hosts show openmausbot" in report.text()


def test_check_an_engine_that_goes_somewhere_else(tmp_path):
    other = muse_instance(environment={"K": OTHER_CANARY})
    other["config"] = {"url": "https://user:" + OTHER_CANARY + "@api.example.com/v1?key=" + OTHER_CANARY, "apiKeyEnv": "K", "model": "m"}
    report = check(tmp_path, {"instances": {"direct": other}})
    assert report.muse_engine is False and report.exit_code == 1
    assert "Engine 'direct' goes straight to https://api.example.com/v1, not through Muse" in report.text()
    wrong_port = muse_instance()
    wrong_port["config"]["url"] = "http://127.0.0.1:1234/v1"
    assert check(tmp_path, {"instances": {"lm": wrong_port}}).muse_engine is False


def test_check_vendor_engines_enabled(tmp_path):
    data = {"instances": {
        "muse": muse_instance(),
        "mistral-vibe": {"driver": "customAcp", "displayName": "Mistral Vibe", "config": {"cli": "/home/you/.local/bin/vibe-acp"}},
        "my-agent": {"driver": "customAcp", "environment": {"MY_AGENT_TOKEN": KEY_CANARY}, "config": {"cli": f"agent --token {KEY_CANARY}"}},
        "claude-work": {"driver": "claude", "config": {}},
        "mystery": {"driver": "somethingNew"},
    }}
    report = check(tmp_path, data)
    assert report.muse_engine is True and report.exit_code == 1 and len(report.warnings) == 4
    text = report.text()
    assert text.count(hosts.NOT_INVOLVED) >= 3
    assert "those use your own subscription login; Muse is not involved in them and cannot ration or record them" in text
    assert "Engine 'mistral-vibe' runs a command-line agent with its own sign-in" in text
    assert "Engine 'claude-work' looks like a vendor's command-line engine" in text
    assert "Engine 'mystery' uses a driver this check does not know. It does not go through Muse." in text
    assert "vibe-acp" not in text and "agent --token" not in text  # commands are never echoed


def test_check_a_muse_engine_that_would_not_authenticate(tmp_path):
    cases = {
        "no value": (muse_instance(environment={}), 'has no value for MUSE_APP_KEY'),
        "not a muse key": (muse_instance(environment={"MUSE_APP_KEY": KEY_CANARY}), "is not a Muse app key"),
        "no env name": (muse_instance(config={"url": BASE, "model": "auto"}), 'has no usable "apiKeyEnv"'),
        "placeholder": (muse_instance(environment={"MUSE_APP_KEY": presets.KEY_PLACEHOLDER}), "is not a Muse app key"),
    }
    for name, (entry, want) in cases.items():
        report = check(tmp_path, {"instances": {"muse": entry}})
        assert report.muse_engine is True and report.exit_code == 1 and want in report.text(), name
    no_v1 = muse_instance()
    no_v1["config"]["url"] = "http://127.0.0.1:4000"
    assert "should end in /v1" in check(tmp_path, {"instances": {"muse": no_v1}}).text()


def test_check_never_prints_a_key_found_in_the_file(tmp_path):
    data = {"instances": {
        "muse": muse_instance(),
        KEY_CANARY: {"driver": "openai-compat", "environment": {"A": OTHER_CANARY}, "config": {"url": "https://x.example/v1", "apiKeyEnv": "A"}},
        "bad name with spaces": {"driver": "customAcp"},
    }, "mcpServers": {
        "notes": {"command": "npx", "args": ["-y", "notes"], "env": {"NOTES_TOKEN": OTHER_CANARY}},
        "docs": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": f"Bearer {OTHER_CANARY}"}},
        "muse-tools": {**TOOLS_ENTRY, "env": {"MUSE_HOME_TOKEN": "sk-muse-dev-CANARYdevtoken0123456789abcdefXY"}},
    }}
    report = check(tmp_path, data)
    text = report.text()
    assert "(a name not shown)" in text and report.secrets_seen >= 4
    assert "The device token is written in this file (MUSE_HOME_TOKEN)" in text and report.exit_code == 1
    for canary in (KEY_CANARY, OTHER_CANARY, MUSE_KEY, "CANARYdevtoken"):
        assert canary not in text


def test_check_the_tools_server_entry(tmp_path):
    report = check(tmp_path, {"instances": {"muse": muse_instance()}})
    assert report.tools_server is False and report.exit_code == 0 and "not registered in this file" in report.text()
    parked = check(tmp_path, {"instances": {"muse": muse_instance()}, "mcpServers": {"muse-tools": {**TOOLS_ENTRY, "enabled": False}}})
    assert parked.tools_server is True and "switched off" in parked.text()
    bare = check(tmp_path, {"instances": {"muse": muse_instance()}, "mcpServers": {"m": {"command": "python", "args": ["-m", "muse.mcp"]}}})
    assert "has no --token-file" in bare.text()


@pytest.mark.parametrize("content", [
    "", "not json at all", "{ trailing: comma, }", '{"instances": {"muse": {"driver": "openai-compat",}}}', "[1, 2, 3]", '"text"', "42",
    "instances:\n  muse:\n    driver: openai-compat\n",  # YAML: not what its docs describe
    '{"engines": [{"name": "muse"}]}',  # JSON, but another shape entirely
    '{"instances": ["muse"]}', '{"instances": {"muse": "openai-compat"}}', '{"instances": "all"}',
    "{" * 5000,
])
def test_check_an_unreadable_format_says_so_and_guesses_nothing(tmp_path, content):
    path = write(tmp_path, content)
    report = hosts.check_config(presets.HOSTS["openmausbot"], path, BASE)
    assert report.readable is False and report.exit_code == 2 and report.muse_engine is not True
    text = report.text()
    assert text.startswith("OpenMausBot: could not read this format.") and "points at Muse" not in text and "WARNING" not in text


def test_check_a_missing_or_huge_file(tmp_path):
    report = hosts.check_config(presets.HOSTS["openmausbot"], tmp_path / "nope.json", BASE)
    assert report.exit_code == 2 and "could not be opened" in report.text()
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (hosts.MAX_CONFIG_BYTES + 1))
    assert hosts.check_config(presets.HOSTS["openmausbot"], big, BASE).exit_code == 2


def test_check_a_tools_only_host(tmp_path):
    report = check(tmp_path, {"mcpServers": {"muse-tools": TOOLS_ENTRY}}, kind="claude-code")
    assert report.exit_code == 0 and report.muse_engine is None and report.tools_server is True
    assert "Claude Code's own model calls" in report.text() and hosts.NOT_INVOLVED in report.text()
    missing = check(tmp_path, {"mcpServers": {"other": {"command": "npx", "args": ["x"]}}}, kind="claude-code")
    assert missing.exit_code == 1 and "muse hosts show claude-code" in missing.text()
    assert check(tmp_path, {"projects": {}}, kind="claude-code").exit_code == 2


def test_check_through_the_cli(monkeypatch, tmp_path):
    good = write(tmp_path, {"instances": {"muse": muse_instance()}, "mcpServers": {"muse-tools": TOOLS_ENTRY}}, "good.json")
    code, out, err = run(["hosts", "check", "openmausbot", str(good)], monkeypatch)
    assert code == 0 and "points at Muse" in out and "CANARY" not in out + err
    code, out, _ = run(["hosts", "check", "openmausbot", str(good), "--json"], monkeypatch)
    assert code == 0 and json.loads(out)["ok"] is True and "CANARY" not in out
    vendor = write(tmp_path, {"instances": {"muse": muse_instance(), "cc": {"driver": "customAcp", "config": {"cli": "claude acp"}}}}, "v.json")
    code, out, _ = run(["hosts", "check", "openmausbot", str(vendor)], monkeypatch)
    assert code == 1 and "WARNING" in out and hosts.NOT_INVOLVED in out
    code, out, _ = run(["hosts", "check", "openmausbot", str(write(tmp_path, "nope", "bad.json"))], monkeypatch)
    assert code == 2 and "could not read this format" in out
    # MUSE_PROXY_URL moves the address the check looks for.
    monkeypatch.setenv("MUSE_PROXY_URL", "http://127.0.0.1:4999")
    code, out, _ = run(["hosts", "check", "openmausbot", str(good)], monkeypatch)
    assert code == 1 and "No engine in this file points at Muse (http://127.0.0.1:4999/v1)" in out


def test_the_check_opens_the_file_for_reading_only():
    source = Path(hosts.__file__).read_text(encoding="utf-8")
    body = source.split("# ---------------------------------------------------------------- the read-only check")[1]
    assert not re.search(r"\.write|open\(|os\.replace|unlink|mkdir|chmod", body)


# ---------------------------------------------------------------- the guides


def guide(name: str) -> str:
    return (ROOT / "docs" / "hosts" / name).read_text(encoding="utf-8")


def test_the_guides_say_what_is_and_is_not_protected():
    readme = guide("README.md")
    from muse.mcp import SCOPE_NOTE

    assert SCOPE_NOTE in readme
    for want in ("never through a vendor", "Not** what the host does with its own shell and file tools", "`pay`", "planned",
                 "muse hosts check", "advisory"):
        assert want in readme, want
    for tool in ("allowance_status", "suggest_service", "ask_approval", "check_approval", "check_permission", "record_action",
                 "queue_job", "job_status", "list_skills", "run_skill", "pause_status"):
        assert f"`{tool}`" in readme, tool


def test_the_openmausbot_guide_marks_every_step_it_could_not_run():
    text = guide("openmausbot.md")
    steps = re.findall(r"^## Step \d+.*$", text, flags=re.M)
    assert len(steps) >= 5
    for block in re.split(r"^## Step \d+", text, flags=re.M)[1:]:
        block = block.split("\n## ")[0]
        assert "not run here" in block.lower() or "run here:" in block.lower(), block[:80]
    assert "was not installed" in text and "end to end" in text
    for want in ("muse hosts setup openmausbot", "~/.openmausbot/config.json", '"apiKeyEnv"', '"openai-compat"', "muse hosts check openmausbot",
                 "x-muse-", "muse status", "How to undo", "muse apps rm openmausbot", "muse home token rm openmausbot-tools"):
        assert want in text, want
    assert "sk-muse-" not in text  # no key, real or sample, in a guide


def test_no_guide_or_preset_tells_anyone_to_install_unpinned_packages():
    for path in [*(ROOT / "docs" / "hosts").glob("*.md"), ROOT / "muse" / "presets.py"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"pip install (?!-e|'muse-stack\[mcp\]')", text), path.name
