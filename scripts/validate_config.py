#!/usr/bin/env python3
"""Validate the catalog and the rendered config. No provider calls, no real keys."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from render_config import _tri, dumps, load_catalog, render  # noqa: E402

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
            check("api_key" not in entry["litellm_params"], f"{deployment_id} is local and must not carry a key")
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
    held, _ = render(catalog, env_with(**keys))
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


def main() -> int:
    steps = [
        scenario_empty,
        scenario_partial,
        scenario_paid_gate,
        scenario_disable_and_openrouter,
        scenario_siliconflow_base,
        scenario_catalog_shape,
        scenario_round_trip_all_free,
        scenario_local_and_policy,
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
