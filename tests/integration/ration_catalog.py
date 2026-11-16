"""Shared constants and the rationing test catalogue (imported by conftest and tests)."""

from __future__ import annotations

from typing import Any

from fake_upstream import FakeUpstream

MASTER_KEY = "sk-test-master-0123456789abcdef"

RATION_KEYS = {"FREEA_API_KEY": "fake-a", "FREEB_API_KEY": "fake-b", "PAIDP_API_KEY": "fake-pp", "PAIDU_API_KEY": "fake-pu",
               "STRONG_API_KEY": "fake-s", "TRIALX_API_KEY": "fake-t"}
# 11 prompt + 7 completion tokens per fake reply: (11*20 + 7*40) / 1e6 = $0.0005 a call.
PRICE_IN, PRICE_OUT = 20.0, 40.0


def build_ration_catalog(upstream: FakeUpstream) -> dict[str, Any]:
    """Rationing scenarios: free A (rate-limit headers), free B (none), priced paid, unpriced paid, local.

    `solo` holds only A and falls back to `other`, which holds only A again, so a
    deferral must survive LiteLLM's cross-alias fallback.
    """
    upstream.set_headers("b-model", {})  # provider B reports nothing, like Gemini
    unknown = {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"}
    return {
        "aliases": {
            "chat": {"mode": "chat"},
            "solo": {"mode": "chat", "fallbacks": ["other"]},
            "other": {"mode": "chat"},
        },
        "providers": [
            {"id": "freea", "env": "FREEA_API_KEY", "tier": "free", "api_base": upstream.base("/a"), "policy": unknown,
             "deployments": [{"id": "a-model", "model": "openai/a-model", "mode": "chat", "aliases": ["chat", "solo", "other"], "order": 1, "rpm": 10}]},
            {"id": "freeb", "env": "FREEB_API_KEY", "tier": "free", "api_base": upstream.base("/b"), "policy": unknown,
             "deployments": [{"id": "b-model", "model": "openai/b-model", "mode": "chat", "aliases": ["chat"], "order": 2, "rpm": 10}]},
            {"id": "paidp", "env": "PAIDP_API_KEY", "tier": "paid", "api_base": upstream.base("/pp"), "policy": unknown,
             "deployments": [{"id": "pp-model", "model": "openai/pp-model", "mode": "chat", "aliases": ["chat"], "order": 1, "rpm": 10,
                              "join": "paid_fallback", "price": {"input_per_mtok": PRICE_IN, "output_per_mtok": PRICE_OUT}}]},
            {"id": "paidu", "env": "PAIDU_API_KEY", "tier": "paid", "api_base": upstream.base("/pu"), "policy": unknown,
             "deployments": [{"id": "pu-model", "model": "openai/pu-model", "mode": "chat", "aliases": ["chat"], "order": 2, "rpm": 10,
                              "join": "paid_fallback"}]},
            {"id": "local_openai", "kind": "local", "base_env": "MUSE_LOCAL_OPENAI_BASE", "tier": "local",
             "policy": {"trains_on_inputs": False, "human_review": False, "region": "local", "commercial_use": "allowed"},
             "deployments": []},
        ],
    }


def build_rules_catalog(upstream: FakeUpstream) -> dict[str, Any]:
    """Routing rules, pacing and the hand-off: the shipped alias names, small and scriptable.

    A and B are free and share order 1 in `chat` and `fast` (so the ordering hint
    has a choice); `solo` holds only A. `smart` is the strong free model S plus a
    priced paid model behind it; `spare` holds only the one-off trial T, which
    is also the second order of `cheap` behind B. `long-context` is B.
    T is marked one-off after rendering (tests set model_info.muse_oneoff, see
    mark_oneoff) until the renderer emits that field.
    """
    unknown = {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"}
    return {
        "aliases": {name: {"mode": "chat"} for name in ("chat", "solo", "fast", "cheap", "smart", "long-context", "spare")},
        "providers": [
            {"id": "freea", "env": "FREEA_API_KEY", "tier": "free", "api_base": upstream.base("/a"), "policy": unknown,
             "deployments": [{"id": "a-model", "model": "openai/a-model", "mode": "chat", "aliases": ["chat", "solo", "fast"],
                              "order": 1, "rpm": 10}]},
            {"id": "freeb", "env": "FREEB_API_KEY", "tier": "free", "api_base": upstream.base("/b"), "policy": unknown,
             "deployments": [{"id": "b-model", "model": "openai/b-model", "mode": "chat",
                              "aliases": ["chat", "fast", "cheap", "long-context"], "order": 1, "rpm": 10}]},
            {"id": "strong", "env": "STRONG_API_KEY", "tier": "free", "api_base": upstream.base("/s"), "policy": unknown,
             "deployments": [{"id": "s-model", "model": "openai/s-model", "mode": "chat", "aliases": ["smart"], "order": 1, "rpm": 10}]},
            {"id": "paidp", "env": "PAIDP_API_KEY", "tier": "paid", "api_base": upstream.base("/pp"), "policy": unknown,
             "deployments": [{"id": "pp-model", "model": "openai/pp-model", "mode": "chat", "aliases": ["smart"], "order": 1, "rpm": 10,
                              "join": "paid_fallback", "price": {"input_per_mtok": PRICE_IN, "output_per_mtok": PRICE_OUT}}]},
            {"id": "trialx", "env": "TRIALX_API_KEY", "tier": "trial", "api_base": upstream.base("/t"), "policy": unknown,
             "deployments": [{"id": "t-model", "model": "openai/t-model", "mode": "chat", "aliases": ["spare", "cheap"],
                              "order": 1, "rpm": 10}]},
            {"id": "local_openai", "kind": "local", "base_env": "MUSE_LOCAL_OPENAI_BASE", "tier": "local",
             "policy": {"trains_on_inputs": False, "human_review": False, "region": "local", "commercial_use": "allowed"},
             "deployments": []},
        ],
    }


def mark_oneoff(config: dict[str, Any]) -> None:
    """post_render: mark the trialx provider one-off, as the catalogue will once the renderer emits it."""
    for entry in config["model_list"]:
        if entry["model_info"].get("provider") == "trialx":
            entry["model_info"]["muse_oneoff"] = True
            if entry["model_name"] == "cheap":
                entry["litellm_params"]["order"] = 2  # behind B
