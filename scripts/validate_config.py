#!/usr/bin/env python3
"""Validate the catalog and the rendered config. No provider calls, no real keys."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))  # muse.catalog: the v2 schema checks live there (standard library + PyYAML only)

from render_config import (  # noqa: E402
    CUSTOM_AUTH,
    LOCAL_PLACEHOLDER_KEY,
    PAID_ORDER_OFFSET,
    _tri,
    dumps,
    load_catalog,
    paid_fallback_enabled,
    render,
)

from verify_litellm import COMPOSE_FILES, compose_images, image_pin_problems  # noqa: E402  (standard library only)

CATALOG_PATH = ROOT / "config" / "providers.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"

REQUIRED_ALIAS = ("fast", "cheap", "smart", "long-context", "embed", "rerank")
CHAT_ALIASES = {"fast", "cheap", "smart", "long-context"}


class Failure(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def env_with(**values: str) -> dict[str, str]:
    base = {"LITELLM_MASTER_KEY": "sk-test-master"}
    base.update(values)
    return base


def ids_for(config: dict, model_name: str) -> list[str]:
    found = []
    for entry in config["model_list"]:
        if entry["model_name"] == model_name:
            found.append(entry["model_info"]["id"])
    return found


def assert_no_secret(text: str, secret: str) -> None:
    check(secret not in text, "rendered config contains a raw secret")
    check("sk-" not in text, "rendered config contains an sk- literal")


def assert_refs(config: dict) -> None:
    seen: set[str] = set()
    for entry in config["model_list"]:
        deployment_id = entry["model_info"]["id"]
        check(deployment_id not in seen, f"duplicate deployment id {deployment_id}")
        seen.add(deployment_id)
        if entry["model_info"].get("muse_kind") == "local":
            check(
                entry["litellm_params"].get("api_key") == LOCAL_PLACEHOLDER_KEY,
                f"{deployment_id} is local and must carry only the placeholder key",
            )
            base = entry["litellm_params"].get("api_base", "")
            check(base.startswith("os.environ/"), f"{deployment_id} api_base is not an env reference")
            continue
        api_key = entry["litellm_params"]["api_key"]
        check(
            isinstance(api_key, str) and api_key.startswith("os.environ/"),
            f"{deployment_id} api_key is not an env reference",
        )
        check("/" not in api_key.removeprefix("os.environ/"), f"bad env ref {api_key}")


def scenario_empty() -> None:
    catalog = load_catalog(CATALOG_PATH)
    config, enabled = render(catalog, {})
    check(enabled == [], "empty env should enable nothing")
    check(config["model_list"] == [], "empty env should render no models")
    check("master_key" not in config["general_settings"], "missing master key should be omitted")
    check(
        config["general_settings"].get("dangerously_permit_weak_or_unset_master_key") is True,
        "empty env should set the LiteLLM unset-master-key override so the proxy can boot",
    )
    check(config["general_settings"]["disable_spend_logs"] is True, "spend logs should be off without a database")
    check(config["general_settings"]["background_health_checks"] is False, "background health checks must stay off")
    text = dumps(config, enabled)
    yaml.safe_load(text)
    check("os.environ/" not in text, "empty render should not reference provider keys")


def scenario_partial() -> None:
    catalog = load_catalog(CATALOG_PATH)
    secret = "gemini-secret-value-should-not-appear"
    config, enabled = render(catalog, env_with(GEMINI_API_KEY=secret))
    check(enabled == ["gemini"], f"partial env enabled {enabled}")
    text = dumps(config, enabled)
    assert_no_secret(text, secret)
    assert_refs(config)
    for alias in ("fast", "cheap", "smart", "long-context", "embed"):
        check(ids_for(config, alias), f"gemini should fill {alias}")
    check(not ids_for(config, "rerank"), "gemini must not invent a rerank deployment")
    check("GROQ_API_KEY" not in text and "OPENROUTER_API_KEY" not in text, "partial render leaked other providers")
    # Direct names exist alongside aliases.
    check(ids_for(config, "gemini-3.8-flash") == ["gemini-3.8-flash"], "pinned name missing")
    check(
        "dangerously_permit_weak_or_unset_master_key" not in config["general_settings"],
        "a real master key should not enable the open-proxy override",
    )


def scenario_paid_gate() -> None:
    catalog = load_catalog(CATALOG_PATH)
    keys = {
        "GEMINI_API_KEY": "g",
        "DEEPSEEK_API_KEY": "d",
        "NOVITA_API_KEY": "n",
        "VOYAGE_API_KEY": "v",
        "NVIDIA_NIM_API_KEY": "nv",
    }
    # Daily paid cap 0 and no override: exactly the pre-cap behaviour (paid stays pinned-only).
    held, _ = render(catalog, env_with(**keys, MUSE_DAILY_PAID_CAP_USD="0"))
    text = dumps(held, ["gemini", "deepseek"])
    assert_no_secret(text, "should-not-matter")
    check(ids_for(held, "deepseek-flash") == ["deepseek-flash"], "paid model should still be pinned")
    check(
        all("deepseek" not in item and "novita" not in item and "voyage" not in item and not item.startswith("nim-")
            for item in ids_for(held, "smart")),
        "paid providers leaked into smart without the fallback flag",
    )
    check(not any(item.startswith("voyage") for item in ids_for(held, "embed")), "voyage leaked into embed")

    opened, enabled = render(catalog, env_with(**keys, MUSE_ALLOW_PAID_FALLBACK="true"))
    check("deepseek" in enabled and "voyage" in enabled, "flag should enable paid providers")
    smart = ids_for(opened, "smart")
    check(any(item.startswith("deepseek-v4-pro__") for item in smart), "deepseek should join smart when allowed")
    check(any(item.startswith("voyage-4-lite__") for item in ids_for(opened, "embed")), "voyage should join embed")
    # nim embed stays pinned-only because aliases is empty.
    check(not any(item.startswith("nim-embed") for item in ids_for(opened, "embed")), "nim embed must not join embed")
    check(ids_for(opened, "nim-embed") == ["nim-embed"], "nim embed pin missing")


def scenario_disable_and_openrouter() -> None:
    catalog = load_catalog(CATALOG_PATH)
    config, enabled = render(
        catalog,
        env_with(
            GEMINI_API_KEY="g",
            GROQ_API_KEY="q",
            OPENROUTER_API_KEY="o",
            MUSE_DISABLE_PROVIDERS="groq, does-not-exist",
        ),
    )
    check("groq" not in enabled and "gemini" in enabled and "openrouter" in enabled, f"disable failed: {enabled}")
    fast_ids = ids_for(config, "fast")
    check(not any("groq" in item for item in fast_ids), "disabled groq still routed")
    orders = [
        entry["litellm_params"]["order"]
        for entry in config["model_list"]
        if entry["model_name"] == "fast" and entry["model_info"]["id"].startswith("openrouter")
    ]
    check(orders and all(order == 9 for order in orders), f"openrouter should be order 9, got {orders}")
    silicon_absent = all(entry["litellm_params"].get("api_base") != "https://api.siliconflow.com/v1" for entry in config["model_list"])
    check(silicon_absent, "siliconflow appeared without a key")


def scenario_siliconflow_base() -> None:
    catalog = load_catalog(CATALOG_PATH)
    config, _ = render(catalog, env_with(SILICONFLOW_API_KEY="sf"))
    bases = {
        entry["model_info"]["id"]: entry["litellm_params"].get("api_base")
        for entry in config["model_list"]
        if entry["model_info"]["provider"] == "siliconflow"
    }
    check(bases, "siliconflow key did not render deployments")
    check(
        all(base == "https://api.siliconflow.com/v1" for base in bases.values()),
        f"unexpected siliconflow bases: {bases}",
    )
    rerank = [entry for entry in config["model_list"] if entry["model_info"]["id"] == "siliconflow-bge-reranker"]
    check(rerank and rerank[0]["litellm_params"]["model"].startswith("hosted_vllm/"), "rerank route is wrong")


def scenario_catalog_shape() -> None:
    catalog = load_catalog(CATALOG_PATH)
    check(tuple(catalog["aliases"]) == REQUIRED_ALIAS, "alias set drifted")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    gitignore = GITIGNORE.read_text(encoding="utf-8")
    check("\n.env\n" in f"\n{gitignore}\n", ".gitignore must ignore .env")
    seen_ids: set[str] = set()
    for provider in catalog["providers"]:
        policy = provider.get("policy") or {}
        for field in ("trains_on_inputs", "human_review", "region", "commercial_use"):
            check(field in policy, f"{provider['id']} policy is missing {field}")
        if provider.get("kind") == "local":
            check(provider["base_env"] in env_example, f"{provider['base_env']} missing from .env.example")
            check(_tri(policy["trains_on_inputs"]) == "no", f"{provider['id']} is local and must not train on inputs")
            check(not provider.get("deployments"), f"{provider['id']} deployments belong in the local catalog")
            continue
        check(provider["env"] in env_example, f"{provider['env']} missing from .env.example")
        check(provider["key_url"] in env_example, f"{provider['key_url']} missing from .env.example")
        for deployment in provider["deployments"]:
            check(deployment["id"] not in seen_ids, f"duplicate id {deployment['id']}")
            seen_ids.add(deployment["id"])
            check(deployment["id"] not in REQUIRED_ALIAS, f"{deployment['id']} collides with an alias")
            if deployment["mode"] == "chat" and "long-context" in deployment["aliases"]:
                check(
                    int(deployment.get("context_tokens") or 0) >= 200_000,
                    f"{deployment['id']} is in long-context with context_tokens < 200000",
                )
            if deployment["mode"] == "chat":
                check(not ({"embed", "rerank"} & set(deployment["aliases"])), f"{deployment['id']} mixes modes")
            for alias in deployment["aliases"]:
                check(catalog["aliases"][alias]["mode"] == deployment["mode"], f"{deployment['id']} mode mismatch")
    # Every chat alias has a free-tier deployment somewhere, so the router is useful with only free keys.
    free_ids = {"gemini", "groq", "cohere", "openrouter"}
    for alias in CHAT_ALIASES:
        owners = [
            provider["id"]
            for provider in catalog["providers"]
            if provider["id"] in free_ids
            for deployment in provider["deployments"]
            if alias in deployment["aliases"] and deployment["join"] == "always"
        ]
        check(owners, f"{alias} has no free-tier deployment")


def scenario_round_trip_all_free() -> None:
    catalog = load_catalog(CATALOG_PATH)
    config, enabled = render(
        catalog,
        env_with(
            GEMINI_API_KEY="g",
            GROQ_API_KEY="q",
            COHERE_API_KEY="c",
            OPENROUTER_API_KEY="o",
            SILICONFLOW_API_KEY="s",
            JINA_API_KEY="j",
        ),
    )
    check(enabled == ["gemini", "groq", "cohere", "openrouter", "siliconflow", "jina"], f"unexpected {enabled}")
    text = dumps(config, enabled)
    loaded = yaml.safe_load(text)
    check(loaded["router_settings"]["routing_strategy"] == "simple-shuffle", "routing strategy missing")
    check(loaded["litellm_settings"]["drop_params"] is True, "drop_params should be on")
    fallback_names = {next(iter(item)) for item in loaded["router_settings"]["fallbacks"]}
    check(fallback_names >= CHAT_ALIASES, f"chat fallbacks incomplete: {fallback_names}")
    for entry in loaded["model_list"]:
        if entry["model_name"] in CHAT_ALIASES | {"embed", "rerank"}:
            check("order" in entry["litellm_params"], f"alias entry missing order: {entry['model_info']['id']}")
    master = loaded["general_settings"]["master_key"]
    check(master == "os.environ/LITELLM_MASTER_KEY", master)


def scenario_local_and_policy() -> None:
    import tempfile

    catalog = load_catalog(CATALOG_PATH)
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(
            yaml.safe_dump(
                {
                    "ollama": [
                        {"id": "ollama-qwen", "model": "ollama_chat/qwen3:8b", "mode": "chat", "aliases": ["fast", "nope"]},
                        {"id": "ollama-embed", "model": "ollama/bge-m3", "mode": "embedding", "aliases": ["embed", "fast"]},
                        {"model": "no-id"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        env = env_with(GEMINI_API_KEY="g", OLLAMA_API_BASE="http://127.0.0.1:11434", MUSE_LOCAL_CATALOG=str(local))
        config, enabled = render(catalog, env)
        check(enabled == ["gemini", "ollama"], f"local provider not enabled: {enabled}")
        assert_refs(config)
        fast = [e for e in config["model_list"] if e["model_name"] == "fast"]
        local_fast = [e for e in fast if e["model_info"]["muse_kind"] == "local"]
        check([e["model_info"]["id"] for e in local_fast] == ["ollama-qwen__fast"], "local chat model should join fast once")
        check(local_fast[0]["litellm_params"]["order"] == 5, "local models default to order 5")
        check(local_fast[0]["model_info"]["muse_trains"] == "no", "local must be marked as not training")
        check(ids_for(config, "ollama-embed") == ["ollama-embed"], "local pin missing")
        check("ollama-embed__embed" in ids_for(config, "embed"), "local embedding should join embed")
        # The same upstream model shares one key across aliases; the hook blocks on it.
        upstream = {
            e["model_info"]["muse_upstream"]
            for e in config["model_list"]
            if e["model_info"]["id"].startswith("gemini-3.1-flash-lite")
        }
        check(upstream == {"gemini:gemini/gemini-3.1-flash-lite"}, f"upstream key drifted: {upstream}")
        gem = next(e for e in config["model_list"] if e["model_info"]["id"] == "gemini-3.8-flash")
        check(gem["model_info"]["muse_trains"] == "yes", "gemini free tier trains on inputs")
        # Base set but no local catalog: provider is on, with nothing to route to.
        config2, enabled2 = render(catalog, env_with(OLLAMA_API_BASE="x", MUSE_LOCAL_CATALOG=str(Path(tmp) / "missing.yaml")))
        check(enabled2 == ["ollama"] and config2["model_list"] == [], "missing local catalog should render no models")


def scenario_hook_callback() -> None:
    """The muse hook is registered in every render, and the module it names exists."""
    from render_config import HOOK_CALLBACK

    catalog = load_catalog(CATALOG_PATH)
    for env in ({}, env_with(GEMINI_API_KEY="g")):
        config, _ = render(catalog, env)
        callbacks = config["litellm_settings"].get("callbacks")
        check(callbacks == [HOOK_CALLBACK], f"muse hook not registered: {callbacks}")
    module, _, attribute = HOOK_CALLBACK.rpartition(".")
    hook_file = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    check(hook_file.is_file(), f"{hook_file} missing")
    check(f"\n{attribute} = " in hook_file.read_text(encoding="utf-8"), f"{hook_file} has no module-level {attribute}")
    config, _ = render(catalog, env_with(MUSE_DISABLE_HOOK="true"))
    check("callbacks" not in config["litellm_settings"], "MUSE_DISABLE_HOOK should drop the callback")


PAID_KEYS = {"GEMINI_API_KEY": "g", "DEEPSEEK_API_KEY": "d", "NOVITA_API_KEY": "n", "SILICONFLOW_API_KEY": "s"}


def _orders(config: dict, alias: str) -> dict[str, int]:
    return {
        entry["model_info"]["id"]: entry["litellm_params"]["order"]
        for entry in config["model_list"]
        if entry["model_name"] == alias
    }


def _paid(entry: dict) -> bool:
    return entry["model_info"].get("muse_tier") in ("paid", "metered")


def scenario_paid_cap_zero_is_unchanged() -> None:
    """Cap 0 renders exactly what the catalogue rendered before the cap existed."""
    catalog = load_catalog(CATALOG_PATH)
    for extra in ({}, {"MUSE_ALLOW_PAID_FALLBACK": "true"}):
        config, _ = render(catalog, env_with(**PAID_KEYS, MUSE_DAILY_PAID_CAP_USD="0", **extra))
        for entry in config["model_list"]:
            if entry["model_name"] in REQUIRED_ALIAS and _paid(entry):
                deployment = next(
                    d for p in catalog["providers"] for d in p.get("deployments") or []
                    if entry["model_info"]["id"].startswith(d["id"] + "__")
                )
                check(entry["litellm_params"]["order"] == deployment["order"], f"cap 0 changed the order of {entry['model_info']['id']}")
        joined = any(i.startswith("deepseek") for i in ids_for(config, "smart"))
        check(joined == bool(extra), f"cap 0 with {extra or 'no override'}: deepseek joined={joined}")
    check(not paid_fallback_enabled({"MUSE_DAILY_PAID_CAP_USD": "0"}), "cap 0 must keep paid off the aliases")
    check(not paid_fallback_enabled({"MUSE_DAILY_PAID_CAP_USD": "abc"}), "an unparsable cap must fail closed")
    check(not paid_fallback_enabled({"MUSE_DAILY_PAID_CAP_USD": "-1"}), "a negative cap must fail closed")


def scenario_paid_cap_default() -> None:
    """Default cap ($1.00): paid joins the aliases, strictly after every free, trial and local deployment."""
    catalog = load_catalog(CATALOG_PATH)
    config, enabled = render(catalog, env_with(**PAID_KEYS, GROQ_API_KEY="q", OPENROUTER_API_KEY="o", COHERE_API_KEY="c"))
    check(paid_fallback_enabled({}), "the default cap should let paid join")
    for alias in ("fast", "cheap", "smart", "long-context"):
        entries = [e for e in config["model_list"] if e["model_name"] == alias]
        free = [e["litellm_params"]["order"] for e in entries if not _paid(e)]
        paid = [e["litellm_params"]["order"] for e in entries if _paid(e)]
        check(free, f"{alias} has no free deployment in this scenario")
        if paid:
            check(min(paid) > max(free), f"{alias}: paid order {min(paid)} is not after free order {max(free)}")
            check(min(paid) > PAID_ORDER_OFFSET, f"{alias}: paid order {min(paid)} is not past the offset")
    check(any(i.startswith("deepseek-v4-pro__") for i in ids_for(config, "smart")), "deepseek should join smart under the default cap")
    check(any(i.startswith("siliconflow-qwen-3-5-9b__") for i in ids_for(config, "fast")), "metered siliconflow stays in fast")
    check(_orders(config, "fast")["siliconflow-qwen-3-5-9b__fast"] == PAID_ORDER_OFFSET + 3, "metered moves after free too")
    # A local model at a high MUSE_LOCAL_ORDER still precedes paid.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(yaml.safe_dump({"ollama": [{"id": "ollama-x", "model": "ollama_chat/x", "mode": "chat", "aliases": ["fast"]}]}))
        config, _ = render(catalog, env_with(**PAID_KEYS, OLLAMA_API_BASE="http://x", MUSE_LOCAL_CATALOG=str(local), MUSE_LOCAL_ORDER="40"))
        orders = _orders(config, "fast")
        check(orders["ollama-x__fast"] == 40, "local order should follow MUSE_LOCAL_ORDER")
        check(all(o > 40 for i, o in orders.items() if i.startswith(("novita", "siliconflow"))), f"paid not after local 40: {orders}")


def scenario_paid_explicit_false() -> None:
    """MUSE_ALLOW_PAID_FALLBACK=false wins over a positive cap."""
    catalog = load_catalog(CATALOG_PATH)
    config, _ = render(catalog, env_with(**PAID_KEYS, MUSE_DAILY_PAID_CAP_USD="5", MUSE_ALLOW_PAID_FALLBACK="false"))
    for alias in ("fast", "cheap", "smart", "long-context"):
        check(not any(i.startswith(("deepseek", "novita")) for i in ids_for(config, alias)), f"explicit false leaked paid into {alias}")
    check(ids_for(config, "deepseek-flash") == ["deepseek-flash"], "paid stays callable by name")
    check(paid_fallback_enabled({"MUSE_ALLOW_PAID_FALLBACK": "true", "MUSE_DAILY_PAID_CAP_USD": "0"}), "explicit true wins")


def scenario_price() -> None:
    """A `price:` block reaches model_info; a deployment without one has no price fields."""
    import copy

    catalog = copy.deepcopy(load_catalog(CATALOG_PATH))
    novita = next(p for p in catalog["providers"] if p["id"] == "novita")
    novita["deployments"][0]["price"] = {"input_per_mtok": 0.02, "output_per_mtok": 0.05}
    config, _ = render(catalog, env_with(NOVITA_API_KEY="n"))
    priced = [e for e in config["model_list"] if e["model_info"]["id"].startswith("novita-llama-3.1-8b")]
    check(priced and all(e["model_info"]["muse_price_in"] == 0.02 and e["model_info"]["muse_price_out"] == 0.05 for e in priced),
          "price did not reach model_info")
    unpriced = [e for e in config["model_list"] if e["model_info"]["id"].startswith("novita-gpt-oss-120b")]
    check(unpriced and all("muse_price_in" not in e["model_info"] for e in unpriced), "a price was invented")
    for bad in ({"input_per_mtok": -1, "output_per_mtok": 1}, {"input_per_mtok": "x", "output_per_mtok": 1}, {"input_per_mtok": 1}, "cheap"):
        novita["deployments"][0]["price"] = bad
        try:
            render(catalog, env_with(NOVITA_API_KEY="n"))
        except SystemExit:
            continue
        raise Failure(f"bad price {bad!r} was accepted")
    # No shipped deployment declares a price (none were verified for this file).
    for provider in load_catalog(CATALOG_PATH)["providers"]:
        for deployment in provider.get("deployments") or []:
            check("price" not in deployment, f"{deployment['id']} declares a price; verify it and update this check")
    # Declared quotas reach model_info for the reserve rule.
    config, _ = render(load_catalog(CATALOG_PATH), env_with(COHERE_API_KEY="c"))
    quotas = {tuple(sorted(q.items())) for e in config["model_list"] for q in e["model_info"].get("muse_quota", [])}
    check(quotas == {(("limit", 1000), ("metric", "requests"), ("window", "month"))}, f"cohere quota not rendered: {quotas}")


def scenario_auth_and_no_secrets() -> None:
    """custom_auth is wired with the hook; neither the master key nor an app token appears in the text."""
    import tempfile

    catalog = load_catalog(CATALOG_PATH)
    master = "sk-master-should-not-appear-0123456789"
    with tempfile.TemporaryDirectory() as tmp:
        token = "sk-muse-app-should-not-appear-abcdefghijklmnop"
        (Path(tmp) / "apps.yaml").write_text(
            "version: 1\napps:\n  - name: bot\n    priority: interactive\n"
            f"    token_sha256: {'0' * 64}\n    note: {token}\n"
        )
        for env in ({"LITELLM_MASTER_KEY": master, "MUSE_HOME": tmp, **PAID_KEYS}, {"MUSE_HOME": tmp}, {"LITELLM_MASTER_KEY": master, "DATABASE_URL": "postgresql://u:p@db/x"}):
            config, enabled = render(catalog, env)
            text = dumps(config, enabled)
            check(master not in text and token not in text and "sk-" not in text, "a secret reached the rendered config")
            check(config["general_settings"].get("custom_auth") == CUSTOM_AUTH, "custom_auth not wired")
            check(
                config["general_settings"].get("custom_auth_run_common_checks", False) == bool(env.get("DATABASE_URL")),
                "common checks should run for custom auth only when a database holds virtual keys",
            )
    config, _ = render(catalog, {"MUSE_DISABLE_HOOK": "true"})
    check("custom_auth" not in config["general_settings"], "MUSE_DISABLE_HOOK should drop custom auth too")
    module, _, attribute = CUSTOM_AUTH.rpartition(".")
    auth_file = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    check(f"async def {attribute}(" in auth_file.read_text(encoding="utf-8"), f"{auth_file} has no {attribute}")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name in ("MUSE_DAILY_PAID_CAP_USD", "MUSE_TZ", "MUSE_ACTIVE_WINDOW_S", "MUSE_RESERVE_BACKGROUND", "MUSE_RESERVE_NORMAL",
                 "MUSE_EXPIRING_DAY_S", "MUSE_EXPIRING_MONTH_S", "MUSE_LOCAL_YIELD"):
        check(name in env_example, f"{name} missing from .env.example")
        check(f"\n      {name}:" in compose, f"{name} is not passed through docker-compose.yml")


def scenario_catalogue_v2() -> None:
    """The v2 fields, resource files and subscriptions pass the schema checks in muse/catalog.py."""
    from muse import catalog

    problems = catalog.validate_catalog()
    check(not problems, "catalogue v2: " + "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
    loaded = catalog.load_catalog()
    check({r.kind for r in loaded.resources} == {"storage", "compute", "chain"}, "storage, compute and chain each need an entry")
    check(all(not r.managed for r in loaded.resources), "other resources are shown, not managed")
    check(all(s.handoff_only for s in loaded.subscriptions) and loaded.subscriptions, "subscriptions are hand-off only")
    for provider in loaded.ai_providers():
        if provider.tier in ("free", "trial"):
            check(provider.connection and provider.connection.minutes_estimate, f"{provider.id} is suggested, so it needs minutes_estimate")


def scenario_native_retry_after() -> None:
    """LiteLLM's own Retry-After cooldown must stay reachable in every render.

    router.py picks: deployment cooldown_time > Retry-After header > router
    cooldown_time. `allowed_fails` on the router switches to the legacy
    count-N-failures rule, which re-sends after a single 429.
    """
    from render_config import DEFAULT_COOLDOWN_FALLBACK_S, cooldown_fallback

    catalog = load_catalog(CATALOG_PATH)
    keys = {str(p["env"]): "k" for p in catalog["providers"] if p.get("env")}
    for env in ({}, env_with(**keys), env_with(MUSE_DISABLE_HOOK="true", **keys)):
        config, _ = render(catalog, env)
        router = config["router_settings"]
        check("allowed_fails" not in router and "allowed_fails_policy" not in router,
              "router_settings must not set allowed_fails: it disables the immediate 429 cooldown")
        check(router.get("cooldown_time") == DEFAULT_COOLDOWN_FALLBACK_S, f"fallback cooldown: {router.get('cooldown_time')}")
        check(router.get("retry_policy", {}).get("RateLimitErrorRetries") == 0,
              "a rate limit must not be retried in the same group: LiteLLM sleeps for the Retry-After first")
        for entry in config["model_list"]:
            for part in ("litellm_params", "model_info"):
                check("cooldown_time" not in entry[part] and "allowed_fails" not in entry[part],
                      f"{entry['model_info']['id']}: a deployment cooldown_time would override Retry-After")
    for raw, want in (("", 30.0), ("2", 2.0), ("3600", 3600.0), ("0", 30.0), ("-5", 30.0), ("99999", 30.0), ("nan", 30.0), ("soon", 30.0)):
        got = cooldown_fallback({"MUSE_COOLDOWN_FALLBACK_S": raw})
        check(got == want, f"MUSE_COOLDOWN_FALLBACK_S={raw!r}: {got}, expected {want}")


def scenario_oneoff_and_also_env() -> None:
    """muse_oneoff is a real boolean on every deployment; a provider with also_env needs all its variables."""
    catalog = load_catalog(CATALOG_PATH)
    config, _ = render(catalog, env_with(VOYAGE_API_KEY="v", GEMINI_API_KEY="g", MUSE_ALLOW_PAID_FALLBACK="true"))
    for entry in config["model_list"]:
        flag = entry["model_info"].get("muse_oneoff")
        check(isinstance(flag, bool), f"{entry['model_info']['id']}: muse_oneoff must be a boolean, got {flag!r}")
        check(flag == (entry["model_info"]["provider"] == "voyage"), f"{entry['model_info']['id']}: muse_oneoff {flag}")
    check(any(e["model_info"]["provider"] == "voyage" for e in config["model_list"]), "voyage did not render")
    key_only, enabled = render(catalog, env_with(CLOUDFLARE_API_KEY="c"))
    check("cloudflare" not in enabled and not key_only["model_list"], "cloudflare must stay off without CLOUDFLARE_ACCOUNT_ID")
    both, enabled = render(catalog, env_with(CLOUDFLARE_API_KEY="c", CLOUDFLARE_ACCOUNT_ID="a" * 32))
    check(enabled == ["cloudflare"], f"cloudflare should turn on with both variables: {enabled}")
    text = dumps(both, enabled)
    check("a" * 32 not in text, "the account id must stay an environment reference")
    check(ids_for(both, "fast") == ["cloudflare-llama-3-1-8b__fast"], "cloudflare should join fast")
    cerebras, enabled = render(catalog, env_with(CEREBRAS_API_KEY="x"))
    check(enabled == ["cerebras"] and "cerebras-gpt-oss-120b__smart" in ids_for(cerebras, "smart"), "cerebras should join smart")
    for provider in catalog["providers"]:
        for extra in provider.get("also_env") or []:
            check(extra in ENV_EXAMPLE.read_text(encoding="utf-8"), f"{extra} missing from .env.example")
            check(f"\n      {extra}:" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8"), f"{extra} is not passed through docker-compose.yml")


# ---------------------------------------------------------------- supply chain

# Files that can install or pin a package, by suffix or exact name.
_INSTALL_SUFFIXES = (".sh", ".bash", ".yml", ".yaml", ".md", ".toml", ".txt", ".in", ".cfg", ".ini", ".py", ".ps1", ".bat")
_INSTALL_NAMES = ("Dockerfile", "Makefile", "justfile", "Procfile")
_INSTALL_SKIP = ("tests/", "core/", "muse_stack.egg-info/")  # tests hold counter-examples on purpose
_INSTALLER = re.compile(r"\b(?:pip3?|pipx|uv\s+pip|uv\s+tool|uv)\s+(?:install|add)\b(.*)", re.IGNORECASE)
_LITELLM_REQ = re.compile(r"""^litellm(?:\[[^\]]*\])?(?![\w.-])\s*(.*)$""")  # lower case: the package, not the word in prose
_EXACT = re.compile(r"^==\s*\d+(?:\.\d+)*(?:\.?(?:post|rc|a|b|dev)\d+)?$")


def unpinned_litellm(spec: str) -> bool:
    """True when `spec` is a litellm requirement without an exact `==` version."""
    token = spec.strip().strip("\"'`,()").strip()
    match = _LITELLM_REQ.match(token)
    if match is None:
        return False
    rest = match.group(1).split(";")[0].split("#")[0].strip().strip("\"'`,")
    return _EXACT.match(rest) is None


def _command_tail(rest: str) -> str:
    """What follows `pip install` up to the end of that command (prose and other commands cut off)."""
    return re.split(r"`|&&|\|\||[;|#]", rest, maxsplit=1)[0]


def install_line_problems(text: str, requirement_file: bool = False) -> list[str]:
    """Lines that install litellm without an exact pin. A `-r <lock file>` install names no package, so it passes."""
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if requirement_file:
            stripped = line.strip()
            if stripped and not stripped.startswith(("#", "-")) and unpinned_litellm(stripped):
                problems.append(f"line {number}: {stripped[:80]}")
            continue
        for match in _INSTALLER.finditer(line):
            # A requirement may be quoted and may hold spaces inside the quotes: split on quotes first.
            for part in re.findall(r"""'[^']*'|"[^"]*"|[^\s'"]+""", _command_tail(match.group(1))):
                if unpinned_litellm(part):
                    problems.append(f"line {number}: {line.strip()[:100]}")
                    break
    return problems


def tracked_files() -> list[Path]:
    """Files git tracks, plus untracked ones it does not ignore; a plain walk when git is not usable."""
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                              capture_output=True, timeout=30, check=True)
        names = [n for n in done.stdout.decode("utf-8", "replace").split("\0") if n]
        if names:
            return [ROOT / n for n in names if (ROOT / n).is_file()]
    except (OSError, subprocess.SubprocessError):
        pass
    skip = {".git", ".venv", "venv", "node_modules", "target", "__pycache__", "muse_stack.egg-info"}
    return [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]


def scenario_supply_chain() -> None:
    """LiteLLM is only ever obtained by digest (image) or exact version / lock file (pip).

    Background: on 2026-10-21 litellm==1.107.0 and the tags latest, main-latest
    and main-stable were malicious for about two hours. Anything that follows a
    floating tag or an open version range would have run it.
    """
    # The rules themselves, so a regex slip cannot silently pass everything.
    bad_images = ("ghcr.io/berriai/litellm:v1.108.5", "ghcr.io/berriai/litellm:main-latest", "litellm/litellm:latest",
                  "ghcr.io/berriai/litellm", "ghcr.io/berriai/litellm:main-stable@sha256:" + "a" * 64,
                  "ghcr.io/berriai/litellm:main@sha256:" + "a" * 64, "ghcr.io/berriai/litellm:v1.108.5@sha256:abc",
                  "ghcr.io/berriai/litellm:v1.107.0@sha256:1bbcb1443bc962e45f5aad53fe3d2c30600e0379715fb6c664cdd3ac126c339e")
    for raw in bad_images:
        check(image_pin_problems(compose_images(f"    image: {raw}")[0]), f"the image rule accepted {raw}")
    good = compose_images("    image: ghcr.io/berriai/litellm:v1.108.5@sha256:" + "a" * 64)[0]
    check(not image_pin_problems(good), "the image rule refused a version tag with a digest")
    installer = "pip" + " install "  # split so this file does not match its own rule
    for line in (installer + "litellm", installer + "'litellm[proxy]'", installer + '"litellm[proxy]>=1.108"',
                 installer + "-U litellm[proxy]~=1.108.0", "uv " + installer + "litellm==1.*", "python -m " + installer + "-e . litellm[proxy]<2",
                 "Run `" + installer + "litellm[proxy]` first."):
        check(install_line_problems(line), f"the install rule accepted: {line}")
    for line in (installer + '"litellm[proxy]==1.108.5"', installer + "--require-hashes -r requirements/proxy.lock",
                 installer + "litellm-proxy-extras", installer + "-e '.[dev]'", "# litellm is installed elsewhere",
                 "`" + installer + "-e .` installs the CLI; litellm comes from the lock file", installer + "pyyaml && echo litellm"):
        check(not install_line_problems(line), f"the install rule refused: {line}")
    check(install_line_problems("litellm[proxy]>=1.100\n", requirement_file=True) and
          not install_line_problems("# c\n-c proxy.lock\nlitellm[proxy]==1.108.5\npytest>=8\n", requirement_file=True),
          "the requirement-file rule is wrong")

    # The compose files.
    seen = 0
    for name in COMPOSE_FILES:
        path = ROOT / name
        if not path.is_file():
            continue
        for ref in compose_images(path.read_text(encoding="utf-8")):
            if not ref.is_litellm:
                continue
            seen += 1
            problems = image_pin_problems(ref)
            check(not problems, f"{name}: " + "; ".join(problems))
    check(seen >= 1, "no LiteLLM image found in the compose files")

    # Every tracked file that could install it.
    lock = ROOT / "requirements" / "proxy.lock"
    check(lock.is_file(), "requirements/proxy.lock is missing")
    lock_text = lock.read_text(encoding="utf-8")
    pins = re.findall(r"^litellm==(\S+)", lock_text, re.MULTILINE)
    check(len(pins) == 1 and "--hash=sha256:" in lock_text, "requirements/proxy.lock must pin exactly one litellm version, with hashes")
    for block in re.split(r"\n(?=[A-Za-z0-9])", lock_text):
        first = block.splitlines()[0] if block.strip() else ""
        if first and not first.startswith("#"):
            check("--hash=sha256:" in block, f"requirements/proxy.lock: {first.split()[0]} has no hash")
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(_INSTALL_SKIP) or not (path.suffix in _INSTALL_SUFFIXES or path.name.startswith(_INSTALL_NAMES)):
            continue
        if path.suffix == ".lock":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        is_requirements = path.suffix == ".in" or (path.suffix == ".txt" and "requirements" in rel.lower())
        problems = install_line_problems(text, requirement_file=is_requirements)
        check(not problems, f"{rel} installs litellm without an exact == version or a lock file: " + "; ".join(problems[:3]))
        if path.name == "pyproject.toml":
            for spec in re.findall(r"""["'](litellm[^"']*)["']""", text):
                check(not unpinned_litellm(spec), f"{rel}: dependency '{spec}' is not an exact == pin")
        # One version everywhere: an install line that names a version must name the locked one.
        for line in text.splitlines():
            commands = [line] if is_requirements else [_command_tail(m.group(1)) for m in _INSTALLER.finditer(line)]
            for command in commands:
                for version in re.findall(r"litellm(?:\[[^\]]*\])?==(\d[\w.]*)", command):
                    check(version == pins[0], f"{rel} installs litellm=={version}, but requirements/proxy.lock pins {pins[0]}")


def main() -> int:
    steps = [
        scenario_hook_callback,
        scenario_empty,
        scenario_partial,
        scenario_paid_gate,
        scenario_disable_and_openrouter,
        scenario_siliconflow_base,
        scenario_catalog_shape,
        scenario_round_trip_all_free,
        scenario_local_and_policy,
        scenario_paid_cap_zero_is_unchanged,
        scenario_paid_cap_default,
        scenario_paid_explicit_false,
        scenario_price,
        scenario_auth_and_no_secrets,
        scenario_catalogue_v2,
        scenario_oneoff_and_also_env,
        scenario_native_retry_after,
        scenario_supply_chain,
    ]
    for step in steps:
        try:
            step()
        except Failure as exc:
            print(f"FAIL  {step.__name__}: {exc}")
            return 1
        print(f"ok    {step.__name__}")
    print("config ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
