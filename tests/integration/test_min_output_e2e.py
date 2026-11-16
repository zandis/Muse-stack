"""The output floor for reasoning models, through the real proxy and the fake upstream.

A reasoning model here answers reasoning-only (empty content, finish_reason
"length") when max_tokens is below 200, like gpt-oss did at 16-20 on real keys.
"""

from __future__ import annotations

from typing import Any

from fake_upstream import FakeUpstream

FLOOR = 256
REASONS_BELOW = "reasoning_below:200"


def build_floor_catalog(upstream: FakeUpstream) -> dict[str, Any]:
    """r-model has a floor; u-model is the same kind of model without one; s-model inherits its provider's.

    `first` holds a floored model that fails and falls back to `second` (no
    floor); `open` holds an unfloored model that fails and falls back to `think`
    (floored). Each failing model has its own provider, so its cooldown touches nothing else.
    """
    for model in ("r-model", "u-model", "s-model"):
        upstream.set(model, REASONS_BELOW)
    upstream.set("rf-model", "500")
    upstream.set("uf-model", "500")
    unknown = {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"}

    def provider(pid: str, env: str, prefix: str, deployment: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"id": pid, "env": env, "tier": "free", "api_base": upstream.base(prefix), "policy": unknown,
                "deployments": [{"mode": "chat", "order": 1, "rpm": 10, **deployment}], **extra}

    return {
        "aliases": {
            "think": {"mode": "chat"},
            "second": {"mode": "chat"},
            "first": {"mode": "chat", "fallbacks": ["second"]},
            "open": {"mode": "chat", "fallbacks": ["think"]},
        },
        "providers": [
            provider("freea", "FREEA_API_KEY", "/a",
                     {"id": "r-model", "model": "openai/r-model", "aliases": ["think"], "min_output_tokens": FLOOR}),
            provider("freeb", "FREEB_API_KEY", "/b", {"id": "u-model", "model": "openai/u-model", "aliases": ["second"]}),
            provider("strong", "STRONG_API_KEY", "/s", {"id": "s-model", "model": "openai/s-model", "aliases": []},
                     min_output_tokens=210),
            provider("flakyr", "TRIALX_API_KEY", "/fr",
                     {"id": "rf-model", "model": "openai/rf-model", "aliases": ["first"], "min_output_tokens": FLOOR}),
            provider("flakyu", "PAIDU_API_KEY", "/fu", {"id": "uf-model", "model": "openai/uf-model", "aliases": ["open"]}),
            {"id": "local_openai", "kind": "local", "base_env": "MUSE_LOCAL_OPENAI_BASE", "tier": "local",
             "policy": {"trains_on_inputs": False, "human_review": False, "region": "local", "commercial_use": "allowed"},
             "deployments": []},
        ],
    }


def text_of(body: Any) -> Any:
    return body["choices"][0]["message"]["content"]


def last(upstream: FakeUpstream, model: str) -> int | None:
    return upstream.calls(model)[-1].max_tokens


def test_small_request_is_raised_to_the_floor_and_an_unfloored_one_is_flagged(make_proxy, upstream):
    proxy = make_proxy(local=False, catalog=build_floor_catalog, env={"MUSE_TZ": "UTC"})

    # Floored deployment: 16 becomes 256 upstream, the answer has visible text, and the header says what happened.
    status, headers, body = proxy.chat("r-model", max_tokens=16)
    assert status == 200, body
    assert last(upstream, "r-model") == FLOOR
    assert text_of(body) == "fake reply" and body["choices"][0]["finish_reason"] == "stop"
    assert headers["x-muse-min-output"] == f"raised:16->{FLOOR}"
    assert "x-muse-answer" not in headers
    assert headers["x-muse-deployment"] == "r-model"

    # The same request to the same kind of model without a floor: sent as asked, empty answer, flagged, body untouched.
    status, headers, body = proxy.chat("u-model", max_tokens=16)
    assert status == 200, body
    assert last(upstream, "u-model") == 16
    assert not text_of(body) and body["choices"][0]["finish_reason"] == "length"
    assert headers["x-muse-answer"] == "empty-reasoning"
    assert "x-muse-min-output" not in headers
    assert body["usage"]["completion_tokens"] == 16  # the provider's own body, as it came

    # Through an alias, and with the newer parameter name.
    status, headers, body = proxy.chat("think", extra={"max_completion_tokens": 20})
    assert status == 200 and text_of(body) == "fake reply", body
    assert last(upstream, "r-model") == FLOOR and headers["x-muse-min-output"] == f"raised:20->{FLOOR}"

    # Never lowered, never invented: a larger cap and no cap both pass through with no header.
    status, headers, body = proxy.chat("r-model", max_tokens=400)
    assert status == 200 and last(upstream, "r-model") == 400 and "x-muse-min-output" not in headers
    status, headers, body = proxy.chat("r-model")
    assert status == 200 and last(upstream, "r-model") is None and "x-muse-min-output" not in headers
    assert "x-muse-answer" not in headers
    status, headers, body = proxy.chat("r-model", max_tokens=FLOOR)
    assert status == 200 and last(upstream, "r-model") == FLOOR and "x-muse-min-output" not in headers

    # A provider-level default reaches its deployment.
    status, headers, body = proxy.chat("s-model", max_tokens=16)
    assert status == 200 and text_of(body) == "fake reply", body
    assert last(upstream, "s-model") == 210 and headers["x-muse-min-output"] == "raised:16->210"

    # Nothing about the request or the answer is written to the proxy log.
    assert "fake reply" not in proxy.log_text() and "thinking it over" not in proxy.log_text()


def test_after_a_fallback_the_floor_of_the_deployment_used_applies(make_proxy, upstream):
    proxy = make_proxy(local=False, catalog=build_floor_catalog, env={"MUSE_TZ": "UTC"})

    # Floored model fails, the fallback has no floor: it gets the client's 16, not the 256 raised for the first.
    status, headers, body = proxy.chat("first", max_tokens=16)
    assert status == 200, body
    assert upstream.calls("rf-model") and all(c.max_tokens == FLOOR for c in upstream.calls("rf-model"))
    assert last(upstream, "u-model") == 16
    assert headers["x-muse-deployment"] == "u-model__second"
    assert "x-muse-min-output" not in headers
    assert headers["x-muse-answer"] == "empty-reasoning" and not text_of(body)

    # Unfloored model fails, the fallback has a floor: raised there, and the header reports it.
    status, headers, body = proxy.chat("open", max_tokens=16)
    assert status == 200, body
    assert upstream.calls("uf-model") and all(c.max_tokens == 16 for c in upstream.calls("uf-model"))
    assert last(upstream, "r-model") == FLOOR
    assert headers["x-muse-deployment"] == "r-model__think"
    assert headers["x-muse-min-output"] == f"raised:16->{FLOOR}"
    assert text_of(body) == "fake reply" and "x-muse-answer" not in headers
