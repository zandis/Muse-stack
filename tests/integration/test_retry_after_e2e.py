"""A 429 with `Retry-After: 60` keeps that upstream out of use, on the real proxy.

Two paths are checked against the real LiteLLM proxy and a fake upstream:

- `native`: the Muse hook is off (MUSE_DISABLE_HOOK), so only LiteLLM's own
  per-deployment cooldown can keep the upstream out. This is the regression
  test for the rendered router settings: the fallback cooldown is set to 2 s
  here, so the upstream would be back within the watch window unless the
  provider's Retry-After (60 s) is what LiteLLM applied.
- `muse`: the hook is on. The shared block must carry the same 60 s, be visible
  in the ledger, and also cover a second alias of the same upstream.

By default the test watches for 12 s. Set MUSE_SLOW_TESTS=1 to watch the whole
60 s and then see the upstream come back.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import os
import time

import pytest

from wardmuse.ledger import Ledger

RETRY_AFTER = 60
FALLBACK = 2  # seconds; far below RETRY_AFTER on purpose
SLOW = envget(os.environ, "MUSE_SLOW_TESTS", "").strip() in {"1", "true", "yes"}
WATCH = RETRY_AFTER - 4 if SLOW else 12


def g_calls(upstream) -> int:
    return len(upstream.calls("g-flash"))


def watch_without_g(proxy, upstream, seconds: float, aliases: tuple[str, ...]) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for alias in aliases:
            status, _, body = proxy.chat(alias)
            assert status == 200, body
        assert g_calls(upstream) == 0, f"g-flash was called again {seconds - (deadline - time.time()):.1f}s after its 429"
        time.sleep(0.5)


@pytest.mark.parametrize("path", ["native", "muse"])
def test_429_retry_after_60_is_not_retried(make_proxy, upstream, path):
    env = {"MUSE_COOLDOWN_FALLBACK_S": str(FALLBACK)}
    if path == "native":
        env["MUSE_DISABLE_HOOK"] = "true"
    proxy = make_proxy(local=False, env=env)

    upstream.set("g-flash", f"429_retry_after:{RETRY_AFTER}")
    started = time.time()
    status, _, body = proxy.chat("fast")
    assert status == 200, body  # the group moved on to q-small
    assert g_calls(upstream) == 1  # one attempt, no second try at the same upstream

    # The provider recovers at once, so only the remembered Retry-After keeps it out.
    upstream.set("g-flash", "ok")
    upstream.clear_log()

    if path == "muse":
        block = Ledger(proxy.home / "ledger.sqlite").active_blocks()["gemini:openai/g-flash"]
        assert block["kind"] == "cooldown"
        assert RETRY_AFTER - 5 <= block["until"] - started <= RETRY_AFTER + 5
        # `cheap` holds a different LiteLLM deployment of the same upstream.
        watch_without_g(proxy, upstream, WATCH, ("fast", "cheap"))
    else:
        watch_without_g(proxy, upstream, WATCH, ("fast",))

    if SLOW:
        time.sleep(max(0.0, started + RETRY_AFTER + 3 - time.time()))
        for _ in range(6):
            assert proxy.chat("fast")[0] == 200
        assert g_calls(upstream) >= 1  # order 1 again once the 60 s have passed


def test_native_cooldown_alone_does_not_cover_a_second_alias(make_proxy, upstream):
    """Why muse/quota.py and the hook's shared block stay after native Retry-After is on.

    LiteLLM 1.108.6 cools one deployment. `fast` and `cheap` hold two
    deployments of the same upstream, so on the plain proxy `cheap` still calls
    the upstream that just said "stay away for 60 s". If this test starts
    failing, LiteLLM has closed the gap and the shared block can be slimmed.
    """
    proxy = make_proxy(local=False, env={"MUSE_DISABLE_HOOK": "true", "MUSE_COOLDOWN_FALLBACK_S": str(FALLBACK)})
    upstream.set("g-flash", f"429_retry_after:{RETRY_AFTER}")
    assert proxy.chat("fast")[0] == 200
    assert g_calls(upstream) == 1
    upstream.clear_log()
    assert proxy.chat("fast")[0] == 200 and g_calls(upstream) == 0  # `fast` is covered
    assert proxy.chat("cheap")[0] == 200
    assert g_calls(upstream) == 1  # `cheap` is not: the same upstream is called again inside its Retry-After


@pytest.mark.parametrize("path", ["native", "muse"])
def test_a_rate_limited_single_deployment_answers_at_once(make_proxy, upstream, path):
    """A model with one deployment: the caller gets the 429 now, not after the proxy slept for the Retry-After.

    LiteLLM sleeps for the provider's Retry-After before retrying a group with a
    single deployment, so the caller used to wait up to a minute for the same
    429. The rendered `retry_policy` turns that retry off.
    """
    env = {"MUSE_COOLDOWN_FALLBACK_S": str(FALLBACK)}
    if path == "native":
        env["MUSE_DISABLE_HOOK"] = "true"
    proxy = make_proxy(local=False, env=env)
    upstream.set("q-small", f"429_retry_after:{RETRY_AFTER}")
    started = time.time()
    status, headers, body = proxy.chat("q-small", timeout=45)  # the pinned name: a group of one
    assert status == 429, body
    assert time.time() - started < 10
    assert len(upstream.calls("q-small")) == 1  # and it was sent once
    if path == "muse":
        # The hook recorded a block until the provider's Retry-After; the caller is told the same.
        headers = {str(k).lower(): v for k, v in dict(headers).items()}
        assert headers.get("x-muse-wait") == "provider-limit", headers
        assert 55 <= int(headers["retry-after"]) <= 60, headers
        assert "x-muse-ration" not in headers  # that header means nothing was sent upstream
