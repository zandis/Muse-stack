"""Ready-made app settings for the tools people point at an OpenAI-compatible endpoint.

Why this exists: "register an app" is easy; knowing which priority and cap suit a
chat window versus a coding agent versus a nightly script, and where each tool
hides its endpoint setting, is the part people get wrong. A preset is data only:
muse/cli.py turns it into an app (muse.apps.add_app) and prints `hint()`.

Each preset says whether its setup steps were confirmed against the tool's own
documentation (`verified`) and on what date. Anything not confirmed is printed
as UNVERIFIED so nobody trusts a menu path that may have moved.
"""

from __future__ import annotations

from dataclasses import dataclass

CHECKED = "12 November 2026"


@dataclass(frozen=True, slots=True)
class Preset:
    kind: str
    title: str
    priority: str
    daily_requests: int | None
    daily_tokens: int | None
    alias: str  # the model name to choose in the tool
    why: str  # one line on the defaults
    steps: tuple[str, ...]  # {base} {key} {alias} are filled in
    verified: bool
    source: str  # where the steps were checked
    caveat: str = ""


PRESETS: dict[str, Preset] = {
    "open-webui": Preset(
        "open-webui", "Open WebUI", "interactive", None, None, "fast",
        "A person is typing in a chat window, so it is never held back and has no cap.",
        (
            "Admin Settings > Connections > OpenAI > Add Connection.",
            "URL: {base}   (no trailing slash)",
            "API Key: {key}",
            "Leave the model list empty to discover models, then pick: {alias}",
        ),
        True, "docs.openwebui.com, OpenAI-compatible quick start (menu path from a search of that page; the page itself did not load when checked)",
        "Open WebUI in Docker: replace 127.0.0.1 in the URL with host.docker.internal.",
    ),
    "librechat": Preset(
        "librechat", "LibreChat", "interactive", None, None, "fast",
        "A person is typing in a chat window, so it is never held back and has no cap.",
        (
            "Add this under endpoints.custom in librechat.yaml:",
            "  - name: 'Muse'",
            "    apiKey: '${{MUSE_APP_KEY}}'      # and in LibreChat's .env:  MUSE_APP_KEY={key}",
            "    baseURL: '{base}'",
            "    models:",
            "      default: ['{alias}']",
            "      fetch: true",
            "Restart LibreChat. The endpoint is then called 'Muse' in its selector.",
        ),
        True, "librechat.ai/docs/quick_start/custom_endpoints",
        "LibreChat in Docker: replace 127.0.0.1 in baseURL with host.docker.internal.",
    ),
    "cline": Preset(
        "cline", "Cline", "normal", None, 1_500_000, "smart",
        "Agent loops send large contexts. Normal priority, 1.5M tokens a day so one runaway task cannot eat the day.",
        (
            "Cline settings (gear icon) > API Provider: OpenAI Compatible.",
            "Base URL: {base}",
            "API Key: {key}",
            "Model ID: {alias}",
            "Click Verify.",
        ),
        True, "docs.cline.bot/provider-config/openai-compatible",
        "Cline sends tool-use prompts; small free models may follow them poorly.",
    ),
    "continue": Preset(
        "continue", "Continue", "normal", None, 1_500_000, "smart",
        "Coding assistant: normal priority, 1.5M tokens a day. Use `fast` for tab autocomplete.",
        (
            "In Continue's config.yaml add:",
            "  models:",
            "    - name: Muse",
            "      provider: openai",
            "      model: {alias}",
            "      apiBase: {base}",
            "      apiKey: {key}",
        ),
        True, "docs.continue.dev/customize/model-providers/top-level/openai",
        "The JSON config format is deprecated in Continue; use config.yaml.",
    ),
    "aider": Preset(
        "aider", "aider", "normal", None, 1_500_000, "smart",
        "Coding assistant: normal priority, 1.5M tokens a day.",
        (
            "export OPENAI_API_BASE={base}",
            "export OPENAI_API_KEY={key}",
            "aider --model openai/{alias}",
        ),
        True, "aider.chat/docs/llms/openai-compat.html",
        "aider may warn that it does not know the model name. That is expected for an alias.",
    ),
    "n8n": Preset(
        "n8n", "n8n", "background", 500, None, "cheap",
        "Automations are not urgent: background priority, 500 requests a day, waits whenever you are busy.",
        (
            "Credentials > New > OpenAI API.",
            "API Key: {key}",
            "Base URL: {base}",
            "In the OpenAI Chat Model node choose model: {alias}",
        ),
        False, "n8n community threads and third-party guides; n8n's own credential page does not document Base URL",
        "A Base URL with a trailing slash or a full /chat/completions path fails. Some n8n versions "
        "pass the credential test but 404 at run time; if so, update n8n or try turning streaming off. "
        "A background app is told to wait (HTTP 429, x-muse-ration: defer:*); n8n must retry, "
        "so enable 'Retry On Fail' with a wait of at least the Retry-After header.",
    ),
    "script": Preset(
        "script", "your own script", "background", 500, None, "cheap",
        "A script is usually not urgent: background priority, 500 requests a day. Raise it with `muse apps set`.",
        (
            "Any OpenAI SDK:  OpenAI(base_url='{base}', api_key='{key}')",
            "or the environment:  OPENAI_BASE_URL={base}  OPENAI_API_KEY={key}",
            "model='{alias}'",
        ),
        True, "the OpenAI SDKs' own base_url / api_key parameters (stable, standard)",
        "On HTTP 429 with a header x-muse-ration: defer:*, nothing was sent upstream: wait Retry-After seconds and try again.",
    ),
}


def get(kind: str) -> Preset:
    try:
        return PRESETS[kind]
    except KeyError:
        raise KeyError(f"unknown preset '{kind}'; choose one of {', '.join(PRESETS)}") from None


def hint(preset: Preset, base_url: str, key: str, alias: str | None = None) -> str:
    """The exact settings, as text. `key` is the token (or a placeholder when it is not shown)."""
    fill = {"base": base_url, "key": key, "alias": alias or preset.alias}
    lines = [s.replace("{{", "\0").replace("}}", "\1").format(**fill).replace("\0", "{").replace("\1", "}") for s in preset.steps]
    out = [f"Settings for {preset.title}:"] + [f"  {line}" for line in lines]
    if preset.caveat:
        out.append(f"  Note: {preset.caveat}")
    if preset.verified:
        out.append(f"  Checked against: {preset.source} ({CHECKED}). Menus move; trust the tool's own docs if they differ.")
    else:
        out.append(f"  UNVERIFIED: {preset.source}. Checked {CHECKED}; treat these steps as a starting point.")
    return "\n".join(out)


# ---------------------------------------------------------------- host bots (docs/spec/05-host-bots.md, docs/hosts/)
#
# A host bot is a bot that already exists and gets Muse underneath it through two doors:
# the engine (Muse's OpenAI-compatible address, with an app token of the host's own) and the
# tools (the Muse tools server, `python -m muse.mcp`, registered in the host as an MCP server).
#
# Same rule as above: data only, and every fact about a host's configuration says where it was
# read and when. `verified` is the date the host's OWN pages were read for exactly these facts;
# None prints as UNVERIFIED. Nothing here was run: no host was installed when these were written.
# `muse hosts setup|show|check` (muse/mcp/hosts.py) turn a preset into text; nothing in Muse ever
# writes into a host's configuration files.

HOSTS_CHECKED = "13 November 2026"
HOST_APP_KEY_ENV = "MUSE_APP_KEY"  # the name the host's configuration uses for the Muse app token
HOST_MODELS = ("auto", "fast", "cheap", "smart", "long-context")  # `auto`: Muse picks by task
KEY_PLACEHOLDER = "<paste the Muse app key here>"


@dataclass(frozen=True, slots=True)
class HostPreset:
    kind: str
    title: str
    priority: str  # the host's app: a person is waiting on a bot's answer
    allow_paid: bool  # never on by default; the person turns it on with `muse apps set`
    config_file: str  # where the host's own docs say its configuration lives
    engine: tuple[str, ...]  # the engine entry to paste; () when the host has no custom-engine setting to use
    tools: tuple[str, ...]  # how to register the Muse tools server
    after: tuple[str, ...]  # what the host's docs say to do after editing
    facts: tuple[str, ...]  # the exact field names and commands read on the host's pages
    verified: str | None  # ISO date the facts were read on the host's own pages, or None
    sources: tuple[str, ...]
    vendor_engines: str  # in plain words: which of the host's engines Muse is NOT part of
    check_format: str  # which reader `muse hosts check` uses: "openmausbot" or "mcp-json"
    unverified: tuple[str, ...] = ()  # what could not be confirmed, said plainly


HOSTS: dict[str, HostPreset] = {
    "openmausbot": HostPreset(
        kind="openmausbot",
        title="OpenMausBot",
        priority="interactive",
        allow_paid=False,
        config_file="~/.openmausbot/config.json",
        engine=(
            '"instances": {{',
            '  "muse": {{',
            '    "driver": "openai-compat",',
            '    "displayName": "Muse",',
            '    "environment": {{ "{key_env}": "{key}" }},',
            '    "config": {{',
            '      "url": "{base}",',
            '      "apiKeyEnv": "{key_env}",',
            '      "model": "{model}"',
            "    }}",
            "  }}",
            "}}",
        ),
        tools=(
            '"mcpServers": {{',
            '  "muse-tools": {{',
            '    "command": "{python}",',
            '    "args": ["-m", "wardmuse.mcp", "--token-file", "{token_file}"]',
            "  }}",
            "}}",
        ),
        after=(
            "Merge both blocks into the one JSON object in the file (keep what is already there), then restart OpenMausBot.",
            "Or add the tools server in the app instead: Plugins > MCP servers > Add server > Run a command.",
            "In each bot's model picker choose the engine called Muse.",
        ),
        facts=(
            'engine entries live under "instances" in ~/.openmausbot/config.json; the app is restarted after editing',
            '"driver": "openai-compat" with "displayName", "environment" and "config"',
            '"config" takes "url" (the base address), "apiKeyEnv" (which "environment" value carries the key) and "model"',
            '"config.tools": false is for a model without tool support',
            'tool servers live under "mcpServers": a command entry has "command", "args" and "env"; "enabled": false parks one',
            'values in "environment" are stored as plain text in config.json, which is written with mode 0600',
        ),
        verified="2026-11-13",
        sources=(
            "https://github.com/milind-soni/OpenMausBot/blob/main/docs/custom-engines.md",
            "https://github.com/milind-soni/OpenMausBot/blob/main/docs/custom-mcp-servers.md",
            "https://github.com/milind-soni/OpenMausBot/blob/main/README.md",
        ),
        vendor_engines="the claude, codex and grok command-line tools (they appear in its model picker by themselves when "
                       'installed and signed in) and any "customAcp" engine',
        check_format="openmausbot",
        unverified=(
            "OpenMausBot was not installed or run: none of these steps was tried end to end.",
            "Whether the name muse-tools is free (its docs list some reserved server names, not all).",
            "How its built-in command-line engines appear in config.json, if at all: its docs do not say.",
            "How well its agent loop works with free models behind Muse's aliases.",
        ),
    ),
    "claude-code": HostPreset(
        kind="claude-code",
        title="Claude Code (tools only)",
        priority="interactive",
        allow_paid=False,
        config_file=".mcp.json in a project, or ~/.claude.json",
        engine=(),
        tools=(
            "claude mcp add --transport stdio muse-tools -- {python} -m wardmuse.mcp --token-file {token_file}",
        ),
        after=(
            "Check it with `claude mcp list`, or `/mcp` inside Claude Code. Remove it with `claude mcp remove muse-tools`.",
        ),
        facts=(
            "claude mcp add [options] <name> -- <command> [args...]; options such as --transport, --env and --scope go before the name",
            "claude mcp list, claude mcp get <name>, claude mcp remove <name>; /mcp inside Claude Code",
            'a project file .mcp.json holds "mcpServers" entries with "command", "args" and "env"; an entry with no "type" is a stdio server',
        ),
        verified="2026-11-13",
        sources=("https://code.claude.com/docs/en/mcp",),
        vendor_engines="Claude Code's own model calls, which use your Anthropic sign-in",
        check_format="mcp-json",
        unverified=(
            "Claude Code was not run with the Muse tools server here.",
        ),
    ),
}


def get_host(kind: str) -> HostPreset:
    try:
        return HOSTS[kind]
    except KeyError:
        raise KeyError(f"unknown host '{kind}'; choose one of {', '.join(HOSTS)}") from None


def host_snippet(host: HostPreset, *, base_url: str, python: str, token_file: str, key: str = KEY_PLACEHOLDER,
                 model: str = HOST_MODELS[0]) -> str:
    """What to paste into the host's configuration, as text. With the default `key` it holds no secret."""
    fill = {"base": base_url, "key_env": HOST_APP_KEY_ENV, "key": key, "model": model, "python": python, "token_file": token_file}
    out = [f"Settings for {host.title}  ({host.config_file}):"]
    if host.engine:
        out += ["", "  The engine (model calls go through Muse):"] + [f"    {line.format(**fill)}" for line in host.engine]
        out.append(f"    Model names: {HOST_MODELS[0]} (Muse picks by task), or {', '.join(HOST_MODELS[1:])}.")
    else:
        out += ["", f"  No engine entry: Muse is not part of {host.vendor_engines}."]
    out += ["", "  The tools (allowance, approvals, the queue):"] + [f"    {line.format(**fill)}" for line in host.tools]
    out += [""] + [f"  {line}" for line in host.after]
    if host.verified:
        out.append(f"  Field names checked against the host's own pages on {HOSTS_CHECKED}: {', '.join(host.sources)}")
    else:
        out.append(f"  UNVERIFIED: these field names were not confirmed on the host's own pages ({HOSTS_CHECKED}).")
    out.append("  Not run or not verified here: " + " ".join(host.unverified))
    return "\n".join(out)
