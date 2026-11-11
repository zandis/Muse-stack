#!/usr/bin/env python3
"""Render a LiteLLM proxy config from config/providers.yaml and the environment.

Providers whose key variable is missing or blank are left out. Paid providers
are registered under their own model names, and join the public aliases only
when MUSE_ALLOW_PAID_FALLBACK is true. Nothing in the rendered file is a secret:
every api_key is an os.environ/NAME reference that LiteLLM resolves at startup.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "config" / "providers.yaml"
DEFAULT_OUT = ROOT / "config" / "generated" / "litellm.yaml"

TRUTHY = {"1", "true", "yes", "on"}
ALIASES_ORDER = ("fast", "cheap", "smart", "long-context", "embed", "rerank")


def load_dotenv(path: Path, environ: dict[str, str]) -> None:
    """Fill environ with KEY=VALUE lines. Existing keys win. No interpolation."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        environ[key] = value


def load_catalog(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must be a mapping")
    if "providers" not in data or "aliases" not in data:
        raise SystemExit(f"{path} needs 'providers' and 'aliases'")
    return data


def _truthy(environ: Mapping[str, str], name: str) -> bool:
    return environ.get(name, "").strip().lower() in TRUTHY


def _disabled(environ: Mapping[str, str]) -> set[str]:
    raw = environ.get("MUSE_DISABLE_PROVIDERS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _enabled(provider: Mapping[str, Any], environ: Mapping[str, str], disabled: set[str]) -> bool:
    provider_id = str(provider["id"]).lower()
    if provider_id in disabled:
        return False
    return bool(environ.get(str(provider["env"]), "").strip())


def _deployment_joins(
    deployment: Mapping[str, Any],
    *,
    paid_fallback: bool,
) -> bool:
    join = deployment.get("join", "always")
    if join == "always":
        return True
    if join == "paid_fallback":
        return paid_fallback
    raise SystemExit(f"Unknown join value {join!r} on {deployment.get('id')}")


def _litellm_params(provider: Mapping[str, Any], deployment: Mapping[str, Any], *, for_alias: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": deployment["model"],
        "api_key": f"os.environ/{provider['env']}",
    }
    api_base = deployment.get("api_base") or provider.get("api_base")
    if api_base:
        params["api_base"] = api_base
    headers = {}
    headers.update(provider.get("extra_headers") or {})
    headers.update(deployment.get("extra_headers") or {})
    if headers:
        params["extra_headers"] = headers
    if for_alias:
        params["order"] = int(deployment["order"])
        if deployment.get("rpm") is not None:
            params["rpm"] = int(deployment["rpm"])
    return params


def _model_info(provider: Mapping[str, Any], deployment: Mapping[str, Any], deployment_id: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": deployment_id,
        "mode": deployment["mode"],
        "provider": provider["id"],
        "muse_verified": deployment.get("verified", "unspecified"),
    }
    if deployment.get("notes"):
        info["description"] = str(deployment["notes"]).strip()
    return info


def render(catalog: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    disabled = _disabled(environ)
    paid_fallback = _truthy(environ, "MUSE_ALLOW_PAID_FALLBACK")
    alias_meta: dict[str, Any] = catalog["aliases"]
    enabled: list[str] = []
    model_list: list[dict[str, Any]] = []
    present: dict[str, int] = {name: 0 for name in alias_meta}

    for provider in catalog["providers"]:
        if not _enabled(provider, environ, disabled):
            continue
        enabled.append(provider["id"])
        for deployment in provider["deployments"]:
            # Direct name, so a paid model can be called without joining an alias.
            pin_id = str(deployment["id"])
            model_list.append(
                {
                    "model_name": pin_id,
                    "litellm_params": _litellm_params(provider, deployment, for_alias=False),
                    "model_info": _model_info(provider, deployment, pin_id),
                }
            )
            if not _deployment_joins(deployment, paid_fallback=paid_fallback):
                continue
            for alias in deployment.get("aliases") or []:
                if alias not in alias_meta:
                    raise SystemExit(f"{pin_id} references unknown alias {alias!r}")
                if alias_meta[alias]["mode"] != deployment["mode"]:
                    raise SystemExit(
                        f"{pin_id} mode {deployment['mode']} does not match alias {alias}"
                    )
                entry_id = f"{pin_id}__{alias}"
                model_list.append(
                    {
                        "model_name": alias,
                        "litellm_params": _litellm_params(provider, deployment, for_alias=True),
                        "model_info": _model_info(provider, deployment, entry_id),
                    }
                )
                present[alias] += 1

    fallbacks: list[dict[str, list[str]]] = []
    context_fallbacks: list[dict[str, list[str]]] = []
    for name, meta in alias_meta.items():
        if present.get(name, 0) == 0:
            continue
        targets = [item for item in meta.get("fallbacks") or [] if present.get(item, 0) > 0]
        if targets:
            fallbacks.append({name: targets})
        context_target = meta.get("context_window_fallback")
        if context_target and present.get(context_target, 0) > 0:
            context_fallbacks.append({name: [context_target]})

    router: dict[str, Any] = {
        "routing_strategy": "simple-shuffle",
        "num_retries": 1,
        "timeout": 120,
        "allowed_fails": 2,
        "cooldown_time": 30,
    }
    if fallbacks:
        router["fallbacks"] = fallbacks
    if context_fallbacks:
        router["context_window_fallbacks"] = context_fallbacks

    general: dict[str, Any] = {
        # Free tiers are small. Do not probe providers in the background.
        "background_health_checks": False,
    }
    if environ.get("LITELLM_MASTER_KEY", "").strip():
        general["master_key"] = "os.environ/LITELLM_MASTER_KEY"
    else:
        # LiteLLM 1.108.5 refuses to boot with no master key. This is the
        # documented local-dev override so a first start with an empty .env
        # still comes up. Do not expose that process past localhost.
        general["dangerously_permit_weak_or_unset_master_key"] = True
    if not environ.get("DATABASE_URL", "").strip():
        general["disable_spend_logs"] = True

    config: dict[str, Any] = {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "request_timeout": 120,
            "num_retries": 1,
        },
        "router_settings": router,
        "general_settings": general,
    }
    return config, enabled


def dumps(config: Mapping[str, Any], enabled: list[str]) -> str:
    body = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    names = ", ".join(enabled) if enabled else "(none)"
    header = (
        "# Generated by scripts/render_config.py. Do not edit.\n"
        "# Source: config/providers.yaml. Secrets stay in the environment.\n"
        f"# Enabled providers: {names}\n"
    )
    return header + body


def write_config(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the LiteLLM config for the current environment.")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env-file", type=Path, default=None, help="Optional .env. Existing process env wins.")
    parser.add_argument("--stdout", action="store_true", help="Print the config instead of writing it.")
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    if args.env_file:
        load_dotenv(args.env_file, environ)
    catalog = load_catalog(args.catalog)
    config, enabled = render(catalog, environ)
    text = dumps(config, enabled)
    if args.stdout:
        sys.stdout.write(text)
    else:
        write_config(text, args.out)
        print(f"wrote {args.out} with providers: {', '.join(enabled) or '(none)'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
