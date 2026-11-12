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
