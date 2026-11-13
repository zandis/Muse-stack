"""Host bots: what to paste into a host, and a read-only look at a host's configuration.

    setup(...)          create the host's app (its own key, interactive, not allowed to pay) and a
                        device token for the tools server, written to a 0600 file and never printed
    snippet(...)        the text to paste into the host; holds no secret
    check_config(...)   read a host's configuration file and say, in plain words, whether an engine
                        points at Muse and which other engines Muse is not part of

Two rules from docs/spec/05-host-bots.md:
  * Muse connects to a host only through its OpenAI-compatible engine setting, never through a
    vendor's command-line login.
  * Muse never writes into a host's files. `setup` prints; the person pastes.

`check_config` parses defensively. It prints nothing it read from the file except short names
that look like names, and addresses with any sign-in part and query removed. A key found in the
file is counted, never shown. A file that is not in the form the host's docs describe gets
"could not read this format", not a guess.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from muse import presets

MAX_CONFIG_BYTES = 2 * 1024 * 1024
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VENDOR_WORDS = ("claude", "codex", "grok", "gemini", "cursor", "copilot", "chatgpt")

NOT_INVOLVED = "those use your own subscription login; Muse is not involved in them and cannot ration or record them"
UNREADABLE = "could not read this format"


def device_name(kind: str) -> str:
    return f"{kind}-tools"


def token_file_for(kind: str, environ: Mapping[str, str] | None = None) -> Path:
    from muse import paths

    return paths.home(environ) / f"host-{kind}.token"


def snippet(host: presets.HostPreset, base_url: str, token_file: Path | str, python: str | None = None) -> str:
    python = python or sys.executable
    token_file = str(token_file)
    if host.check_format == "openmausbot":  # the text goes inside JSON strings
        python, token_file = json.dumps(python)[1:-1], json.dumps(token_file)[1:-1]
    return presets.host_snippet(host, base_url=base_url, python=python, token_file=token_file)


# ---------------------------------------------------------------- setup


class SetupError(Exception):
    """Why setup did nothing. Safe to print."""


@dataclass(slots=True)
class SetupResult:
    app_name: str | None
    app_token: str | None  # shown once by the caller; None for a tools-only host
    device: str
    token_file: Path


def setup(host: presets.HostPreset, *, app_name: str | None = None, app_environ: Mapping[str, str] | None = None,
          home_environ: Mapping[str, str] | None = None) -> SetupResult:
    """Create the app and the tools device for a host. Checks everything first, so a refusal changes nothing."""
    from muse import apps
    from muse.home import auth

    name = app_name or host.kind
    device = device_name(name)
    path = token_file_for(name, home_environ)
    try:
        auth.validate_name(device)
    except auth.TokenError:
        raise SetupError("that name is too long or has characters a name cannot have") from None
    if host.engine:
        if not apps.NAME_RE.fullmatch(name) or name in apps.RESERVED_NAMES:
            raise SetupError("that app name is not allowed; use lower-case letters, digits and '-'")
        try:
            existing = apps.load_apps(app_environ)
        except apps.AppError:
            raise SetupError("the app list could not be read; run `muse doctor`") from None
        if name in existing:
            raise SetupError(f"an app named '{name}' already exists. `muse hosts show {host.kind}` prints the settings again; "
                             f"`muse apps rotate {name}` issues a new key")
    if any(d.name == device for d in auth.list_devices(home_environ)):
        raise SetupError(f"a device named '{device}' already exists. Remove it with `muse home token rm {device}` to start over")
    if path.exists() or path.is_symlink():
        raise SetupError(f"{path} already exists. Delete it to start over")
    app_token = None
    if host.engine:
        try:
            _, app_token = apps.add_app(name, host.priority, allow_paid=host.allow_paid,
                                        note=f"host bot: {host.title}", environ=app_environ)
        except apps.AppError:
            raise SetupError("the app could not be created; run `muse doctor`") from None
    try:
        _, device_token = auth.add_token(device, home_environ)
    except auth.TokenError:
        raise SetupError("the tools device could not be created; `muse home token list` shows what exists") from None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from its first byte, never a link
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(device_token + "\n")
    except OSError:
        auth.remove_token(device, home_environ)  # a token nobody holds must not keep working
        raise SetupError(f"could not write {path}; the tools device was removed again") from None
    return SetupResult(name if host.engine else None, app_token, device, path)


# ---------------------------------------------------------------- the read-only check


@dataclass(slots=True)
class Report:
    host: str
    readable: bool = True
    muse_engine: bool | None = None  # None: this host has no engine entry to look for
    tools_server: bool = False
    good: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    secrets_seen: int = 0

    @property
    def exit_code(self) -> int:
        if not self.readable:
            return 2
        return 1 if self.warnings or self.muse_engine is False else 0

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "readable": self.readable, "muse_engine": self.muse_engine, "tools_server": self.tools_server,
                "good": self.good, "warnings": self.warnings, "notes": self.notes, "secrets_seen": self.secrets_seen,
                "ok": self.exit_code == 0}

    def text(self) -> str:
        if not self.readable:
            return "\n".join([f"{self.host}: {UNREADABLE}."] + [f"  {n}" for n in self.notes])
        out = [f"{self.host}:"]
        out += [f"  ok       {line}" for line in self.good]
        out += [f"  WARNING  {line}" for line in self.warnings]
        out += [f"  note     {line}" for line in self.notes]
        return "\n".join(out)


def _secretlike(value: Any) -> bool:
    from muse.catalog import looks_secret

    return isinstance(value, str) and looks_secret(value)


def _name(value: Any) -> str:
    """A name from the file, only if it looks like a name. Never a value that could be a secret."""
    if isinstance(value, str) and SAFE_NAME.fullmatch(value) and not _secretlike(value):
        return f"'{value}'"
    return "(a name not shown)"


def _address(value: Any) -> tuple[str, str, int | None, str] | None:
    """(scheme, host, port, path) of an http(s) address, or None. Sign-in parts and queries are dropped."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port, parts.path.rstrip("/")


def _show_address(addr: tuple[str, str, int | None, str]) -> str:
    scheme, host, port, path = addr
    shown = f"{scheme}://{host}{':' + str(port) if port else ''}{path}"
    return "(an address not shown)" if _secretlike(shown) or len(shown) > 200 else shown


_LOCAL = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}


def _same_place(a: tuple[str, str, int | None, str], b: tuple[str, str, int | None, str]) -> bool:
    def port(x: tuple[str, str, int | None, str]) -> int:
        return x[2] or (443 if x[0] == "https" else 80)

    same_host = a[1] == b[1] or (a[1] in _LOCAL and b[1] in _LOCAL)
    return a[0] == b[0] and same_host and port(a) == port(b)


def _count_secrets(obj: Any) -> int:
    from muse.catalog import find_secretlike

    try:
        return len(find_secretlike(obj))
    except RecursionError:
        return 0


def _load(path: Path, report: Report) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            report.readable = False
            report.notes.append("The file is larger than a configuration file should be.")
            return None
        raw = path.read_bytes()
    except OSError:
        report.readable = False
        report.notes.append("The file could not be opened. Check the path.")
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        report.readable = False
        report.notes.append("It is not the JSON this host's documentation describes. Nothing was guessed.")
        return None
    if not isinstance(data, dict):
        report.readable = False
        report.notes.append("It is JSON, but not the object this host's documentation describes. Nothing was guessed.")
        return None
    report.secrets_seen = _count_secrets(data)
    if report.secrets_seen:
        report.notes.append(f"The file holds {report.secrets_seen} value(s) that look like keys or tokens. None is shown here.")
    return data


def _check_tools(servers: Any, report: Report) -> None:
    """Find the Muse tools server among `mcpServers` entries."""
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        report.notes.append('"mcpServers" is not in the form the documentation describes; it was not read.')
        return
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args") if isinstance(entry.get("args"), list) else []
        words = [a for a in args if isinstance(a, str)]
        command = entry.get("command") if isinstance(entry.get("command"), str) else ""
        if "muse.mcp" not in words and not command.endswith("muse-mcp"):
            continue
        report.tools_server = True
        parked = entry.get("enabled") is False
        report.good.append(f"The Muse tools server is registered as {_name(name)}" + (", but switched off." if parked else "."))
        if "--token-file" not in words:
            report.notes.append("The tools server entry has no --token-file. It then needs MUSE_HOME_TOKEN in its environment.")
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        if "MUSE_HOME_TOKEN" in env:
            report.warnings.append("The device token is written in this file (MUSE_HOME_TOKEN). Keep it in a 0600 file instead and "
                                   "pass --token-file.")
    if not report.tools_server:
        report.notes.append("The Muse tools server is not registered in this file (it may have been added in the host's own "
                            "settings screen instead).")


def _check_openmausbot(data: dict[str, Any], base_url: str, report: Report) -> None:
    from muse import apps

    known = {"instances", "mcpServers", "features"}
    if data and not known & set(data):
        report.readable = False
        report.notes.append('It has none of "instances", "mcpServers" or "features". Nothing was guessed.')
        return
    instances = data.get("instances", {})
    if not isinstance(instances, dict) or any(not isinstance(v, dict) for v in instances.values()):
        report.readable = False
        report.notes.append('"instances" is not in the form the documentation describes. Nothing was guessed.')
        return
    muse = _address(base_url)
    report.muse_engine = False
    for name, entry in instances.items():
        driver = entry.get("driver")
        shown = _name(name)
        if driver == "openai-compat":
            config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
            addr = _address(config.get("url"))
            if addr is None:
                report.notes.append(f"Engine {shown} is OpenAI-compatible but its address could not be read.")
                continue
            if muse is None or not _same_place(addr, muse):
                report.warnings.append(f"Engine {shown} goes straight to {_show_address(addr)}, not through Muse. Muse cannot ration "
                                       "or record what it uses.")
                continue
            report.muse_engine = True
            report.good.append(f"Engine {shown} points at Muse ({_show_address(addr)}).")
            if addr[3] != muse[3]:
                report.warnings.append(f"Engine {shown}: the address should end in {muse[3] or '/'} (Muse's address is {base_url}).")
            key_env = config.get("apiKeyEnv")
            env = entry.get("environment") if isinstance(entry.get("environment"), dict) else {}
            if not isinstance(key_env, str) or not ENV_NAME.fullmatch(key_env):
                report.warnings.append(f'Engine {shown} has no usable "apiKeyEnv", so it sends no Muse app key.')
            elif not isinstance(env.get(key_env), str) or not env.get(key_env):
                report.warnings.append(f'Engine {shown}: "environment" has no value for {key_env}, so it sends no Muse app key.')
            elif not env[key_env].startswith(apps.TOKEN_PREFIX):
                report.warnings.append(f"Engine {shown}: the key in {key_env} is not a Muse app key (those start with "
                                       f"{apps.TOKEN_PREFIX}). It is not shown here.")
            else:
                report.good.append(f"Engine {shown} sends a Muse app key from {key_env} (not shown).")
        elif driver == "customAcp":
            report.warnings.append(f"Engine {shown} runs a command-line agent with its own sign-in. If that is a vendor's tool: "
                                   f"{NOT_INVOLVED}.")
        else:
            label = " ".join(str(x).lower() for x in (name, driver) if isinstance(x, str))
            if any(word in label for word in VENDOR_WORDS):
                report.warnings.append(f"Engine {shown} looks like a vendor's command-line engine: {NOT_INVOLVED}.")
            else:
                report.warnings.append(f"Engine {shown} uses a driver this check does not know. It does not go through Muse.")
    if not report.muse_engine:
        report.warnings.append(f"No engine in this file points at Muse ({base_url}). `muse hosts show openmausbot` prints the entry to add.")
    report.notes.append("OpenMausBot also offers the claude, codex and grok command-line tools by themselves when they are installed "
                        f"and signed in. They do not appear in this file, so this check cannot see them: {NOT_INVOLVED}. "
                        "In each bot's model picker, choose the Muse engine.")
    _check_tools(data.get("mcpServers"), report)


def _check_mcp_json(data: dict[str, Any], host: presets.HostPreset, report: Report) -> None:
    if "mcpServers" not in data:
        report.readable = False
        report.notes.append('It has no "mcpServers". Nothing was guessed.')
        return
    report.muse_engine = None
    _check_tools(data["mcpServers"], report)
    if not report.tools_server:
        report.warnings.append(f"The Muse tools server is not in this file. `muse hosts show {host.kind}` prints how to add it.")
    report.notes.append(f"Muse is not part of {host.vendor_engines}: {NOT_INVOLVED}. Only the Muse tools are connected.")


def check_config(host: presets.HostPreset, path: Path | str, base_url: str) -> Report:
    """Read a host's configuration file. Read-only: the file is opened for reading and nothing is written anywhere."""
    report = Report(host.title)
    data = _load(Path(path).expanduser(), report)
    if data is None:
        return report
    if host.check_format == "openmausbot":
        _check_openmausbot(data, base_url, report)
    elif host.check_format == "mcp-json":
        _check_mcp_json(data, host, report)
    else:
        report.readable = False
        report.notes.append("Muse has no reader for this host's configuration yet.")
    return report
