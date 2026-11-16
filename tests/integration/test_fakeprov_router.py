"""Real LiteLLM router, fake provider, the quota breaker, and the two gaps left open.

One proxy serves every scenario. Models that fail sit at order 1; the next
order is the fallback. No real provider is called.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from wardmuse.fakeprov import FakeProvider
from wardmuse.ledger import Ledger
from wardmuse.quota import next_midnight_pacific

CLOUD = {"trains_on_inputs": True, "human_review": True, "region": "us", "commercial_use": "allowed"}
GROQ_POLICY = {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"}


def catalog(fake: FakeProvider):
    def chat(dep_id: str, model: str, alias: str, order: int, **extra):
        row = {"id": dep_id, "model": f"openai/{model}", "mode": "chat", "aliases": [alias], "order": order, "rpm": 10}
        row.update(extra)
        return row

    def build(_upstream):
        return {
            "aliases": {
                "quota": {"mode": "chat", "fallbacks": ["spare"]},
                "bare": {"mode": "chat"},
                "retry": {"mode": "chat"},
                "perday": {"mode": "chat"},
                "spare": {"mode": "chat"},
                "reason": {"mode": "chat"},
                "forbid": {"mode": "chat"},
                "stream": {"mode": "chat"},
            },
            "providers": [
                {
                    "id": "gemini", "env": "GEMINI_API_KEY", "tier": "free", "api_base": fake.base("/gemini"),
                    "policy": CLOUD,
                    "deployments": [
                        chat("g-quota", "g-quota", "quota", 1),
                        chat("g-bare", "g-bare", "bare", 1),
                        chat("g-retry", "g-retry", "retry", 1),
                        chat("g-day", "g-day", "perday", 1),
                        chat("g-other", "g-other", "perday", 2),
                        chat("g-stream", "g-stream", "stream", 1),
                    ],
                },
                {
                    "id": "groq", "env": "GROQ_API_KEY", "tier": "free", "api_base": fake.base("/groq"),
                    "policy": GROQ_POLICY,
                    "deployments": [
                        chat("q-spare", "q-spare", "spare", 1),
                        chat("q-bare", "q-bare", "bare", 2),
                        chat("q-retry", "q-retry", "retry", 2),
                        chat("q-day", "q-day", "perday", 3),
                        chat("q-reason", "q-reason", "reason", 1, min_output_tokens=256),
                        chat("q-forbid", "q-forbid", "forbid", 3),
                        chat("q-stream", "q-stream", "stream", 2),
                    ],
                },
                {
                    "id": "embedder", "env": "PRIVCLOUD_API_KEY", "tier": "free", "api_base": fake.base("/embedder"),
                    "policy": CLOUD,
                    "deployments": [
                        chat("e-403", "e-403", "forbid", 1),
                        chat("e-other", "e-other", "forbid", 2),
                    ],
                },
            ],
        }

    return build


def calls(fake: FakeProvider, model: str) -> int:
    return len(fake.calls(model))


def blocks(proxy) -> dict:
    return Ledger(proxy.home / "ledger.sqlite").active_blocks()


@pytest.fixture()
def fake():
    provider = FakeProvider().start()
    provider.set("g-quota", "429_unclassified")
    provider.set("g-bare", "429_bare")
    provider.set("g-retry", "429_retry_after:30")
    provider.set("g-day", "429_gemini_day")
    provider.set("g-stream", "sse_error")
    provider.set("e-403", "403_quota")
    provider.set("q-reason", "reasoning_below:256")
    provider.set_reply("q-spare", "from-spare")
    provider.set_reply("q-bare", "from-bare")
    provider.set_reply("q-retry", "from-retry")
    provider.set_reply("q-day", "from-day")
    provider.set_reply("q-forbid", "from-forbid")
    provider.set_reply("q-reason", "from-reason")
    provider.set_reply("g-other", "from-other-gemini")
    yield provider
    provider.stop()


def test_router_fallbacks_and_the_breaker(make_proxy, fake):
    proxy = make_proxy(local=False, catalog=catalog(fake), env={"MUSE_COOLDOWN_FALLBACK_S": "1"})

    # In-group fallback: the 429 is not retried; the next order answers.
    status, _, body = proxy.chat("retry")
    assert status == 200 and body["choices"][0]["message"]["content"] == "from-retry"
    assert calls(fake, "g-retry") == 1 and calls(fake, "q-retry") == 1
    assert blocks(proxy)["gemini:openai/g-retry"]["until"] - time.time() == pytest.approx(30, abs=5)
    fake.clear_log()
    assert proxy.chat("retry")[0] == 200
    assert calls(fake, "g-retry") == 0 and calls(fake, "q-retry") == 1

    # Cross-alias fallback: `quota` lists `spare`, and `quota`'s own model is the one that 429s.
    status, _, body = proxy.chat("quota")
    assert status == 200 and body["choices"][0]["message"]["content"] == "from-spare"
    assert calls(fake, "q-spare") == 1
    quota_block = blocks(proxy)["gemini:openai/g-quota"]
    assert quota_block["kind"] == "exhausted" and "no clock" in quota_block["reason"]
    # This deployment is Gemini, so the estimated reset is midnight Pacific.
    assert quota_block["until"] == pytest.approx(next_midnight_pacific(time.time()), abs=5)

    # A bare 429 stays a one-minute cooldown.
    assert proxy.chat("bare")[2]["choices"][0]["message"]["content"] == "from-bare"
    bare = blocks(proxy)["gemini:openai/g-bare"]
    assert bare["kind"] == "cooldown" and bare["until"] - time.time() == pytest.approx(60, abs=5)

    # Gemini PerDay blocks that one model. The other Gemini deployment still answers.
    status, _, body = proxy.chat("perday")
    assert status == 200 and body["choices"][0]["message"]["content"] == "from-other-gemini"
    assert calls(fake, "g-day") == 1 and calls(fake, "g-other") == 1
    assert "provider:gemini" not in blocks(proxy)
    assert blocks(proxy)["gemini:openai/g-day"]["until"] == pytest.approx(next_midnight_pacific(time.time()), abs=5)
    fake.clear_log()
    assert proxy.chat("perday")[2]["choices"][0]["message"]["content"] == "from-other-gemini"
    assert calls(fake, "g-day") == 0 and calls(fake, "g-other") == 1

    # 403 quota text is the bad-key rule: the whole provider, for 15 minutes.
    # The next call therefore skips both embedder models.
    first = proxy.chat("forbid")
    assert "provider:embedder" in blocks(proxy), first
    fake.clear_log()
    assert proxy.chat("forbid")[2]["choices"][0]["message"]["content"] == "from-forbid"
    assert calls(fake, "e-403") == 0 and calls(fake, "e-other") == 0

    # The output floor lifts a 32-token cap, so the reasoning model has room to write.
    status, headers, body = proxy.chat("reason", max_tokens=32)
    assert status == 200, body
    assert body["choices"][0]["message"]["content"] == "from-reason"
    assert calls(fake, "q-reason") == 1
    assert fake.calls("q-reason")[0].max_tokens is not None and fake.calls("q-reason")[0].max_tokens >= 256
    assert headers.get("x-muse-min-output", "").startswith("raised:32->")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Gemini PerDay is upstream-scoped. After g-day is exhausted, g-other still answers `perday`, "
        "so this alias does not fall through to Groq. Provider scope waits on a cassette that shows "
        "the cap is project-wide. docs/reviews/2026-11-13-dsh-our-free-model-ideas.md gap 2."
    ),
)
def test_gemini_per_day_blocks_the_whole_provider(make_proxy, fake):
    proxy = make_proxy(local=False, catalog=catalog(fake), env={"MUSE_COOLDOWN_FALLBACK_S": "1"})
    assert proxy.chat("perday")[0] == 200
    fake.clear_log()
    body = proxy.chat("perday")[2]
    assert body["choices"][0]["message"]["content"] == "from-day"
    assert calls(fake, "g-other") == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "An error object inside an HTTP 200 SSE stream never reaches classify_failure, so the "
        "upstream is not blocked and the fallback is not what answers. Left open until a captured "
        "stream shows the frame the hook should read. docs/reviews/2026-11-13-dsh-our-free-model-ideas.md gap 3."
    ),
)
def test_error_frame_inside_a_200_stream_opens_the_breaker(make_proxy, fake):
    proxy = make_proxy(local=False, catalog=catalog(fake), env={"MUSE_COOLDOWN_FALLBACK_S": "1"})
    payload = json.dumps({"model": "stream", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(
        proxy.url + "/v1/chat/completions",
        data=payload,
        headers={"content-type": "application/json", "authorization": f"Bearer {proxy.master_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        pass
    time.sleep(0.6)
    assert any("g-stream" in key for key in blocks(proxy))
