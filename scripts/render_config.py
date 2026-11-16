#!/usr/bin/env python3
"""Render a LiteLLM proxy config from config/providers.yaml and the environment.

Providers whose key variable is missing or blank are left out. Paid providers
are always registered under their own model names. They also join the public
aliases when paid use is on (see paid_fallback_enabled), at an order strictly
after every free, trial and local deployment, so LiteLLM tries them last; the
hook's paid gate (muse/ration.py) then decides per request. Nothing in the
rendered file is a secret: every api_key is an os.environ/NAME reference that
LiteLLM resolves at startup, and app tokens never pass through here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml


def envget(environ, key, default=""):
    """WARDMUSE_<suffix> if set and non-blank, else MUSE_<suffix>, else default.

    Same rule as wardmuse.envutil.envget. This file stays free of that import so
    the proxy can render even when the package is not what is being started.
    """
    if key.startswith("WARDMUSE_"):
        suffix = key[len("WARDMUSE_"):]
    elif key.startswith("MUSE_"):
        suffix = key[len("MUSE_"):]
    else:
        suffix = key
    new_key, old_key = f"WARDMUSE_{suffix}", f"MUSE_{suffix}"
    if new_key in environ and str(environ[new_key]).strip() != "":
        return environ[new_key]
    if old_key in environ:
        return environ[old_key]
    if new_key in environ:
        return environ[new_key]
    return default



ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "config" / "providers.yaml"
DEFAULT_OUT = ROOT / "config" / "generated" / "litellm.yaml"

TRUTHY = {"1", "true", "yes", "on"}
LOCAL_PLACEHOLDER_KEY = "local-no-key"
ALIASES_ORDER = ("fast", "cheap", "smart", "long-context", "vision", "embed", "rerank")
HOOK_CALLBACK = "wardmuse.hook.handler"
CUSTOM_AUTH = "wardmuse.auth.user_api_key_auth"
PAID_TIERS = {"paid", "metered"}
PAID_ORDER_OFFSET = 20
FALSY = {"0", "false", "no", "off"}
MIN_OUTPUT_TOKENS_MAX = 8192  # same bound as muse/catalog.py (this script imports nothing from muse)
REASONING_PARAMS = ("reasoning_effort",)  # same as muse/catalog.py
REASONING_VALUES = ("low", "medium", "high")
REASONING_TASKS = ("light", "everyday", "heavy", "frontier")


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


DEFAULT_COOLDOWN_FALLBACK_S = 30.0


def cooldown_fallback(environ: Mapping[str, str]) -> float:
    """Seconds LiteLLM cools a deployment when the failure has no Retry-After.

    MUSE_COOLDOWN_FALLBACK_S, 1 to 3600; anything else means the default (30).
    """
    raw = envget(environ, "MUSE_COOLDOWN_FALLBACK_S", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_COOLDOWN_FALLBACK_S
    if not 1 <= value <= 3600:  # also rejects nan
        return DEFAULT_COOLDOWN_FALLBACK_S
    return value


def paid_cap_positive(environ: Mapping[str, str]) -> bool:
    """MUSE_DAILY_PAID_CAP_USD > 0. Unset means the default (0.9375, NT$30 at 32); unparsable means 0.

    Mirrors muse.ration.parse_paid_cap without importing the package, so the
    validator runs with PyYAML only.
    """
    from decimal import Decimal, InvalidOperation

    raw = envget(environ, "MUSE_DAILY_PAID_CAP_USD", "")
    if not str(raw).strip():
        return True
    try:
        value = Decimal(str(raw).strip().lstrip("$").strip())
    except InvalidOperation:
        return False
    return value.is_finite() and value > 0


def paid_fallback_enabled(environ: Mapping[str, str]) -> bool:
    """Whether paid deployments join the public aliases.

    Precedence: MUSE_ALLOW_PAID_FALLBACK set to a false value -> off; set to a
    true value -> on (the paid gate still refuses at request time if the cap
    is 0); unset or empty -> on exactly when the daily paid cap is > 0.
    """
    explicit = envget(environ, "MUSE_ALLOW_PAID_FALLBACK", "").strip().lower()
    if explicit in FALSY:
        return False
    if explicit in TRUTHY:
        return True
    return paid_cap_positive(environ)


def _disabled(environ: Mapping[str, str]) -> set[str]:
    raw = envget(environ, "MUSE_DISABLE_PROVIDERS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_local(provider: Mapping[str, Any]) -> bool:
    return provider.get("kind", "cloud") == "local"


def _enabled(provider: Mapping[str, Any], environ: Mapping[str, str], disabled: set[str]) -> bool:
    provider_id = str(provider["id"]).lower()
    if provider_id in disabled:
        return False
    gate = provider["base_env"] if is_local(provider) else provider["env"]
    if not environ.get(str(gate), "").strip():
        return False
    # Some providers need more than a key (Cloudflare also needs the account id).
    return all(environ.get(str(extra), "").strip() for extra in (provider.get("also_env") or []))


def local_catalog_path(environ: Mapping[str, str]) -> Path:
    explicit = envget(environ, "MUSE_LOCAL_CATALOG", "").strip()
    if explicit:
        return Path(explicit)
    raw_home = str(envget(environ, "MUSE_HOME", "") or "").strip()
    if raw_home:
        home = raw_home
    else:
        new_home, old_home = Path.home() / ".wardmuse", Path.home() / ".muse"
        home = str(new_home if new_home.exists() or not old_home.exists() else old_home)
    return Path(home) / "local.yaml"


def load_local_deployments(environ: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Models installed on this machine, keyed by local provider id.

    Written by `muse local discover`. A missing or malformed file means no
    local models; it never stops the proxy from starting.
    """
    path = local_catalog_path(environ)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for provider_id, items in data.items():
        if isinstance(items, list):
            out[str(provider_id)] = [item for item in items if isinstance(item, dict) and item.get("id") and item.get("model")]
    return out


def _local_order(environ: Mapping[str, str]) -> int:
    try:
        return int(envget(environ, "MUSE_LOCAL_ORDER", "5"))
    except ValueError:
        return 5


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


def is_paid_tier(provider: Mapping[str, Any]) -> bool:
    return not is_local(provider) and str(provider.get("tier", "")).strip().lower() in PAID_TIERS


def _price(deployment: Mapping[str, Any]) -> tuple[float, float] | None:
    """(input, output) USD per million tokens from an optional `price:` block."""
    raw = deployment.get("price")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SystemExit(f"{deployment.get('id')}: price must be a mapping with input_per_mtok and output_per_mtok")
    out = []
    for key in ("input_per_mtok", "output_per_mtok"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value or value == float("inf"):
            raise SystemExit(f"{deployment.get('id')}: price.{key} must be a non-negative number of USD per 1M tokens")
        out.append(float(value))
    return out[0], out[1]


def _min_output_tokens(provider: Mapping[str, Any], deployment: Mapping[str, Any]) -> int | None:
    """The output floor for a chat deployment: its own `min_output_tokens`, else the provider's default.

    Same rule as muse.catalog.min_output_tokens_for. A bad value stops the
    render for a catalogue provider; a hand-edited local catalog must not stop
    startup, so there a bad value means no floor.
    """
    found: list[int] = []
    for owner, label in ((deployment, str(deployment.get("id"))), (provider, str(provider.get("id")))):
        value = owner.get("min_output_tokens")
        if value is None:
            continue
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MIN_OUTPUT_TOKENS_MAX:
            found.append(value)
        elif is_local(provider):
            return None
        else:
            raise SystemExit(f"{label}: min_output_tokens must be a whole number from 1 to {MIN_OUTPUT_TOKENS_MAX}")
    if str(deployment.get("mode")) != "chat":
        if deployment.get("min_output_tokens") is not None and not is_local(provider):
            raise SystemExit(f"{deployment.get('id')}: min_output_tokens is for chat deployments only")
        return None
    return found[0] if found else None


def _reasoning(provider: Mapping[str, Any], deployment: Mapping[str, Any]) -> dict[str, str] | None:
    """A chat deployment's `reasoning` setting, as muse.catalog.reasoning_for reads it. A bad value stops the
    render for a catalogue provider; in a hand-edited local catalog it means no setting."""
    raw = deployment.get("reasoning")
    if raw is None:
        return None
    ok = (isinstance(raw, Mapping) and raw.get("param") in REASONING_PARAMS and len(raw) > 1
          and all(k in REASONING_TASKS and v in REASONING_VALUES for k, v in raw.items() if k != "param")
          and str(deployment.get("mode")) == "chat")
    if not ok:
        if is_local(provider):
            return None
        raise SystemExit(f"{deployment.get('id')}: reasoning must be {{param: reasoning_effort, <task>: low|medium|high}} "
                         "on a chat deployment")
    return {str(k): str(v) for k, v in raw.items()}


def _quota_list(provider: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The provider's declared `quota:` as [{window, metric, limit}] for the hook's reserve rule."""
    raw = provider.get("quota")
    items: list[tuple[str, str, Any]] = []
    if isinstance(raw, list):
        items = [(str(i.get("window")), str(i.get("metric")), i.get("limit")) for i in raw if isinstance(i, Mapping)]
    elif isinstance(raw, Mapping):
        for key, value in raw.items():
            metric, _, window = str(key).partition("_per_")
            items.append((window, metric, value))
    return [
        {"window": w, "metric": m, "limit": int(v)}
        for w, m, v in items
        if w in ("minute", "day", "month") and m in ("requests", "tokens") and isinstance(v, int) and not isinstance(v, bool) and v > 0
    ]


def _litellm_params(provider: Mapping[str, Any], deployment: Mapping[str, Any], *, for_alias: bool) -> dict[str, Any]:
    params: dict[str, Any] = {"model": deployment["model"]}
    if is_local(provider):
        # No key. The base URL is not a secret but differs per machine, so it
        # stays an environment reference like the keys do.
        params["api_base"] = f"os.environ/{provider['base_env']}"
        # Local servers ignore the key, but the OpenAI client will not send a
        # request without one. This placeholder is not a secret.
        params["api_key"] = LOCAL_PLACEHOLDER_KEY
    else:
        params["api_key"] = f"os.environ/{provider['env']}"
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


def _tri(value: Any) -> str:
    """YAML reads bare yes/no as booleans. Normalise to yes | no | unknown."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    text = str(value).strip().lower() if value is not None else ""
    return text if text in {"yes", "no"} else "unknown"


def _clinical_use(value: Any) -> str:
    """forbidden | allowed | unknown. Anything else, including a missing field, is unknown."""
    text = str(value).strip().lower() if value is not None else ""
    return text if text in {"forbidden", "allowed", "unknown"} else "unknown"


_LOOPBACK_BINDS = {"", "127.0.0.1", "localhost", "::1"}


def bind_is_loopback(environ: Mapping[str, str]) -> bool:
    """Unset MUSE_BIND means the compose and native default, which is loopback."""
    return envget(environ, "MUSE_BIND", "").strip().lower() in _LOOPBACK_BINDS


def _model_info(provider: Mapping[str, Any], deployment: Mapping[str, Any], deployment_id: str) -> dict[str, Any]:
    policy = provider.get("policy") or {}
    info: dict[str, Any] = {
        "id": deployment_id,
        "mode": deployment["mode"],
        "provider": provider["id"],
        "muse_verified": deployment.get("verified", "unspecified"),
        # Read by muse/hook.py. muse_upstream is the same for every alias that
        # points at one upstream model, so a 429 on one alias blocks them all.
        "muse_upstream": f"{provider['id']}:{deployment['model']}",
        "muse_kind": provider.get("kind", "cloud"),
        "muse_tier": str(provider.get("tier", "unknown")),
        "muse_trains": _tri(policy.get("trains_on_inputs")),
        "muse_human_review": _tri(policy.get("human_review")),
        "muse_clinical_use": _clinical_use(policy.get("clinical_use")),
        "muse_region": str(policy.get("region", "unknown")),
        # Catalogue v2: trial credits that never renew. muse/ration.py holds these
        # back for jobs that need them. Always a real boolean.
        "muse_oneoff": provider.get("one_off", False) is True,
    }
    price = _price(deployment)
    if price is not None:
        # USD per million tokens; muse/cost.py and the paid gate read these.
        info["muse_price_in"], info["muse_price_out"] = price
    floor = _min_output_tokens(provider, deployment)
    if floor is not None:
        # muse/hook.py raises a smaller max_tokens to this for the one upstream call (reasoning models).
        info["muse_min_output_tokens"] = floor
    reasoning = _reasoning(provider, deployment)
    if reasoning is not None:
        # muse/hook.py sets this parameter by task tier when the client did not (see providers.yaml header).
        info["muse_reasoning"] = reasoning
    declared = _quota_list(provider)
    if declared:
        info["muse_quota"] = declared
    if deployment.get("notes"):
        info["description"] = str(deployment["notes"]).strip()
    return info


def render(catalog: Mapping[str, Any], environ: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    disabled = _disabled(environ)
    paid_fallback = paid_fallback_enabled(environ)
    # With a cap > 0, paid deployments sit strictly after every free, trial and
    # local deployment. With cap 0 the orders are left exactly as they were.
    shift_paid = paid_cap_positive(environ)
    free_orders = [
        int(d.get("order", 0))
        for p in catalog["providers"]
        if not is_paid_tier(p)
        for d in (p.get("deployments") or [])
    ]
    paid_offset = max([PAID_ORDER_OFFSET, *free_orders, _local_order(environ)])
    alias_meta: dict[str, Any] = catalog["aliases"]
    enabled: list[str] = []
    model_list: list[dict[str, Any]] = []
    present: dict[str, int] = {name: 0 for name in alias_meta}

    local_deployments = load_local_deployments(environ)
    local_order = _local_order(environ)

    for provider in catalog["providers"]:
        if not _enabled(provider, environ, disabled):
            continue
        enabled.append(provider["id"])
        deployments = list(provider.get("deployments") or [])
        if is_local(provider):
            for item in local_deployments.get(str(provider["id"]), []):
                merged = {"join": "always", "order": local_order, "rpm": 10, "aliases": [], "verified": "local-discovery"}
                merged.update(item)
                deployments.append(merged)
        for deployment in deployments:
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
                if is_local(provider) and alias not in alias_meta:
                    continue
                if alias not in alias_meta:
                    raise SystemExit(f"{pin_id} references unknown alias {alias!r}")
                if is_local(provider) and alias_meta[alias]["mode"] != deployment["mode"]:
                    continue  # a hand-edited local catalog must not stop startup
                if alias_meta[alias]["mode"] != deployment["mode"]:
                    raise SystemExit(
                        f"{pin_id} mode {deployment['mode']} does not match alias {alias}"
                    )
                entry_id = f"{pin_id}__{alias}"
                params = _litellm_params(provider, deployment, for_alias=True)
                if shift_paid and is_paid_tier(provider):
                    params["order"] = paid_offset + int(deployment["order"])
                model_list.append(
                    {
                        "model_name": alias,
                        "litellm_params": params,
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
        # No `allowed_fails` here, on purpose. Setting it switches LiteLLM
        # 1.108.6 to its legacy rule (cool a deployment only after N failures
        # in a minute), so a single 429 was sent to the same upstream again.
        # Without it, a 429 cools the deployment at once, for the provider's
        # Retry-After (router.py deployment_callback_on_failure: deployment
        # cooldown_time > Retry-After header > this router default).
        # `cooldown_time` is only that last fallback, for a failure that
        # carries no Retry-After. Never set cooldown_time on a deployment:
        # that would override the header.
        "cooldown_time": cooldown_fallback(environ),
        # A rate limit is never retried in the same group. LiteLLM would first
        # sleep for the provider's Retry-After (router.py
        # _time_to_sleep_before_retry: always, for a group with one deployment),
        # holding the caller for up to a minute before the same 429. Fallback
        # groups are still tried, and the hook's block says when to come back.
        "retry_policy": {"RateLimitErrorRetries": 0},
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
    elif not bind_is_loopback(environ):
        # The override below accepts unauthenticated calls. That is only safe
        # on loopback. Compose publishes MUSE_BIND; the process inside the
        # container always listens on 0.0.0.0, so the bind has to be checked here.
        raise SystemExit(
            f"LITELLM_MASTER_KEY is empty and MUSE_BIND={envget(environ, 'MUSE_BIND', '').strip()} "
            "is not loopback. Refusing to start an unauthenticated proxy off this machine."
        )
    else:
        # LiteLLM 1.108.6 refuses to boot with no master key. This is the
        # documented local-dev override so a first start with an empty .env
        # still comes up. Do not expose that process past localhost.
        general["dangerously_permit_weak_or_unset_master_key"] = True
    if not environ.get("DATABASE_URL", "").strip():
        general["disable_spend_logs"] = True
    if not _truthy(environ, "MUSE_DISABLE_HOOK"):
        # muse/auth.py: master key -> owner, app tokens -> their app (MUSE_HOME/apps.yaml).
        general["custom_auth"] = CUSTOM_AUTH
        if environ.get("DATABASE_URL", "").strip():
            # Admin-UI virtual keys live in the database and carry budgets; keep
            # LiteLLM's own checks for them (custom auth skips them by default).
            general["custom_auth_run_common_checks"] = True

    litellm_settings: dict[str, Any] = {
        "drop_params": True,
        "request_timeout": 120,
        "num_retries": 1,
    }
    if not _truthy(environ, "MUSE_DISABLE_HOOK"):
        # muse/hook.py: shared quota blocks, usage ledger, privacy classes.
        # LiteLLM imports it as a module, so the repo root (/opt/muse in the
        # container) must be on PYTHONPATH; render_and_start.py sets that.
        litellm_settings["callbacks"] = [HOOK_CALLBACK]

    config: dict[str, Any] = {
        "model_list": model_list,
        "litellm_settings": litellm_settings,
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
