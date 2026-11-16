"""Circuit breaker and fallback, driven by the fake provider.

The breaker itself is `muse.quota.classify_failure` plus the ledger block the
hook writes. This file plays provider responses through that pair and shows
the next call skips a blocked upstream. The same scenarios against the real
LiteLLM router are in `tests/integration/test_fakeprov_router.py`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from muse.fakeprov import FakeProvider
from muse.ledger import Ledger
from muse.quota import classify_failure

NOW = 1_794_466_800.0  # 2026-11-12T07:00:00Z, the quota tests' clock


def play(base: str, model: str) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(base + "/chat/completions", data=data, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def route(ledger: Ledger, fake: FakeProvider, ladder: list[tuple[str, str, str]]) -> str | None:
    """Try each (model, provider, upstream) until one answers. A failure can block the next try."""
    for model, provider, upstream in ladder:
        blocks = ledger.active_blocks(NOW)
        if blocks.get(upstream) or blocks.get(f"provider:{provider}"):
            continue
        status, headers, body = play(fake.base(), model)
        if status == 200:
            return model
        block = classify_failure(provider, status, headers, body.decode(), NOW)
        if block is not None:
            ledger.set_block(block.key(provider, upstream), block.until, block.kind, block.reason, NOW)
    return None


@pytest.fixture()
def fake():
    provider = FakeProvider().start()
    provider.set("g-quota", "429_unclassified")
    provider.set("g-bare", "429_bare")
    provider.set("g-day", "429_gemini_day")
    provider.set("g-other", "ok")
    provider.set("g-retry", "429_retry_after:30")
    provider.set("q-ok", "ok")
    provider.set("e-403", "403_quota")
    provider.set("e-other", "ok")
    provider.set("oss", "reasoning_below:256")
    yield provider
    provider.stop()


def test_a_429_falls_through_and_the_breaker_holds(fake, tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)
    ladder = [("g-retry", "gemini", "gemini:openai/g-retry"), ("q-ok", "groq", "groq:openai/q-ok")]
    assert route(ledger, fake, ladder) == "q-ok"
    assert len(fake.calls("g-retry")) == 1
    block = ledger.active_blocks(NOW)["gemini:openai/g-retry"]
    assert block["kind"] == "cooldown" and block["until"] == NOW + 30
    fake.clear_log()
    assert route(ledger, fake, ladder) == "q-ok"
    assert fake.calls("g-retry") == []  # the breaker kept the second call off the limited model


def test_unclassified_quota_waits_out_the_day_and_a_bare_429_does_not(fake, tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)
    assert route(ledger, fake, [("g-quota", "groq", "groq:openai/g-quota"), ("q-ok", "groq", "groq:openai/q-ok")]) == "q-ok"
    block = ledger.active_blocks(NOW)["groq:openai/g-quota"]
    assert block["kind"] == "exhausted" and "no clock" in block["reason"]
    assert block["until"] == pytest.approx(NOW + 17 * 3600)  # UTC midnight: 2026-11-13T00:00:00Z

    bare = Ledger(tmp_path / "bare.sqlite", clock=lambda: NOW)
    assert route(bare, fake, [("g-bare", "gemini", "gemini:openai/g-bare"), ("q-ok", "groq", "groq:openai/q-ok")]) == "q-ok"
    short = bare.active_blocks(NOW)["gemini:openai/g-bare"]
    assert short["kind"] == "cooldown" and short["until"] == NOW + 60


def test_gemini_per_day_blocks_one_model_and_403_quota_blocks_the_provider(fake, tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)
    # PerDay is upstream-scoped: the other Gemini model still answers.
    assert route(ledger, fake, [
        ("g-day", "gemini", "gemini:openai/g-day"),
        ("g-other", "gemini", "gemini:openai/g-other"),
    ]) == "g-other"
    assert "gemini:openai/g-day" in ledger.active_blocks(NOW)
    assert "provider:gemini" not in ledger.active_blocks(NOW)
    fake.clear_log()
    assert route(ledger, fake, [
        ("g-day", "gemini", "gemini:openai/g-day"),
        ("g-other", "gemini", "gemini:openai/g-other"),
    ]) == "g-other"
    assert fake.calls("g-day") == [] and len(fake.calls("g-other")) == 1

    # A 403 whose text says quota is still the bad-key rule: the whole provider, 15 minutes.
    denied = Ledger(tmp_path / "denied.sqlite", clock=lambda: NOW)
    assert route(denied, fake, [
        ("e-403", "embedder", "embedder:openai/e-403"),
        ("e-other", "embedder", "embedder:openai/e-other"),
        ("q-ok", "groq", "groq:openai/q-ok"),
    ]) == "q-ok"
    block = denied.active_blocks(NOW)["provider:embedder"]
    assert block["kind"] == "exhausted" and block["until"] == NOW + 900
    fake.clear_log()
    assert route(denied, fake, [
        ("e-403", "embedder", "embedder:openai/e-403"),
        ("e-other", "embedder", "embedder:openai/e-other"),
        ("q-ok", "groq", "groq:openai/q-ok"),
    ]) == "q-ok"
    assert fake.calls("e-403") == [] and fake.calls("e-other") == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Gemini PerDay stays upstream-scoped, so the second Gemini model in the ladder still answers. "
        "Provider scope waits on a captured response that shows the cap is project-wide. "
        "docs/reviews/2026-11-13-dsh-our-free-model-ideas.md gap 2."
    ),
)
def test_gemini_per_day_would_skip_every_gemini_model(fake, tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)
    route(ledger, fake, [("g-day", "gemini", "gemini:openai/g-day"), ("g-other", "gemini", "gemini:openai/g-other")])
    fake.clear_log()
    answered = route(ledger, fake, [
        ("g-day", "gemini", "gemini:openai/g-day"),
        ("g-other", "gemini", "gemini:openai/g-other"),
        ("q-ok", "groq", "groq:openai/q-ok"),
    ])
    assert answered == "q-ok" and fake.calls("g-other") == []


def test_a_small_cap_on_a_reasoning_model_comes_back_empty(fake):
    """What the gate measured: gpt-oss with a small max_tokens returns no visible text."""
    status, _, body = play_with(fake, "oss", max_tokens=32)
    parsed = json.loads(body)
    assert status == 200 and parsed["choices"][0]["message"]["content"] == ""
    assert parsed["choices"][0]["finish_reason"] == "length"
    status, _, body = play_with(fake, "oss", max_tokens=256)
    assert json.loads(body)["choices"][0]["message"]["content"] == "fake reply"


def play_with(fake: FakeProvider, model: str, max_tokens: int) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps({"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": "hi"}]}).encode()
    request = urllib.request.Request(fake.base() + "/chat/completions", data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
