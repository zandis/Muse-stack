"""The scriptable provider. Loopback only; nothing here has a real key."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from wardmuse.fakeprov import FakeProvider, script_for


def post(base: str, payload: dict) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(base + "/chat/completions", data=data, headers={"content-type": "application/json", "authorization": "Bearer sk-test"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def test_each_scenario_has_the_status_headers_and_body_the_router_classifies():
    assert script_for("429_groq_rpm").headers["retry-after"] == "2"
    assert "x-ratelimit-reset-requests" in script_for("429_groq_rpm").headers
    assert "try again in 6m50.832s" in script_for("429_groq_tpd").body().decode()
    day = json.loads(script_for("429_gemini_day").body())
    assert "PerDay" in day["error"]["details"][0]["violations"][0]["quotaId"]
    assert "retryDelay" in json.dumps(day)
    minute = json.loads(script_for("429_gemini_minute").body())
    assert "PerMinute" in json.dumps(minute)
    assert script_for("429_unclassified").status == 429
    assert "quota" in script_for("429_unclassified").body().decode()
    assert "retry-after" not in script_for("429_unclassified").headers
    assert script_for("429_bare").status == 429 and "quota" not in script_for("429_bare").body().decode().lower()
    denied = json.loads(script_for("403_quota").body())
    assert script_for("403_quota").status == 403 and "quota" in denied["error"]["message"].lower()
    empty = json.loads(script_for("empty_reasoning").body())
    assert empty["choices"][0]["message"]["content"] == ""
    assert empty["usage"]["completion_tokens_details"]["reasoning_tokens"] == 16
    starved = json.loads(script_for("reasoning_below:256", max_tokens=32).body())
    assert starved["choices"][0]["message"]["content"] == ""
    fed = json.loads(script_for("reasoning_below:256", max_tokens=256).body())
    assert fed["choices"][0]["message"]["content"] == "fake reply"
    frames = script_for("sse_error")
    assert frames.status == 200 and frames.chunks is not None
    assert any('"error"' in frame for frame in frames.chunks)
    assert frames.headers["content-type"] == "text/event-stream"


def test_the_server_serves_those_scenarios_including_a_slow_one():
    provider = FakeProvider(slow_seconds=0.2).start()
    try:
        provider.set("slow-model", "slow")
        provider.set("oss", "empty_reasoning")
        provider.set("stream-model", "sse_error")
        began = time.perf_counter()
        status, _, body = post(provider.base(), {"model": "slow-model", "messages": [{"role": "user", "content": "hi"}]})
        assert status == 200 and time.perf_counter() - began >= 0.15
        assert json.loads(body)["choices"][0]["message"]["content"] == "fake reply"
        status, _, body = post(provider.base(), {"model": "oss", "max_tokens": 16, "messages": []})
        parsed = json.loads(body)
        assert status == 200 and parsed["choices"][0]["message"]["content"] == ""
        status, headers, body = post(provider.base(), {"model": "stream-model", "stream": True, "messages": []})
        assert status == 200 and "event-stream" in headers["content-type"] and b'"error"' in body
        assert provider.calls("oss")[0].max_tokens == 16
        # The bearer token is not what the log keeps.
        assert all("sk-" not in item.model for item in provider.calls())
    finally:
        provider.stop()
