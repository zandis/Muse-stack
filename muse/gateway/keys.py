"""Report which member-supplied provider keys are present, never their values.

Members add their own keys by copying ``.env.example`` to ``.env`` and filling
the variables (``GROQ_API_KEY``, ``GEMINI_API_KEY``, …). The proxy and the
phase 0 service read those from the environment. This module is the public
view of that: a map of variable name → ``configured`` | ``missing``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import yaml

from muse.secrets import configured_flags, secret_env_names

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "config" / "providers.yaml"
ENV_EXAMPLE = ROOT / ".env.example"

# Speech keys are documented in .env.example but are not routed by LiteLLM.
EXTRA_KEY_VARS = (
    "LITELLM_MASTER_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "ASSEMBLYAI_API_KEY",
)


def catalog_key_vars(catalog_path: Path | None = None) -> list[str]:
    path = catalog_path or CATALOG
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    names = [str(provider["env"]) for provider in data["providers"]]
    return names


def all_documented_key_vars() -> list[str]:
    names = catalog_key_vars()
    for extra in EXTRA_KEY_VARS:
        if extra not in names:
            names.append(extra)
    return names


def key_status(
    environ: Mapping[str, str],
    *,
    names: list[str] | None = None,
) -> dict[str, str]:
    wanted = names if names is not None else all_documented_key_vars()
    return configured_flags(environ, wanted)


def assert_response_safe(payload: object, environ: Mapping[str, str]) -> None:
    from muse.secrets import assert_no_secrets

    assert_no_secrets(payload, environ)
    # Also refuse to echo any other env value that looks like a secret name.
    # The flags themselves are the words "configured" and "missing" only.
    for name in secret_env_names(environ):
        if name in (payload if isinstance(payload, dict) else {}):
            # A key name as a dict key is fine; a key *value* is not.
            value = payload[name] if isinstance(payload, dict) else None  # type: ignore[index]
            if isinstance(value, str) and value not in {"configured", "missing"} and environ.get(name):
                if value == environ[name]:
                    raise ValueError("refusing to emit a secret value")
