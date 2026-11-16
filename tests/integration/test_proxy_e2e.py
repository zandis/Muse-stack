"""End to end: real LiteLLM proxy, muse hook loaded from the rendered config, fake upstream."""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from wardmuse.ledger import Ledger
from wardmuse.status import build_snapshot

CLOUD = {"g-flash", "q-small", "p-model"}


def ledger(proxy) -> Ledger:
    return Ledger(Path(proxy.home) / "ledger.sqlite")


def models(upstream) -> list[str]:
    return [item.model for item in upstream.calls()]


def test_success_is_recorded_without_content(make_proxy, upstream):
    proxy = make_proxy()
    marker = f"MARKER-{uuid.uuid4().hex}"
    status, headers, body = proxy.chat("fast", marker)
    assert status == 200, body
    assert headers["x-muse-provider"] in {"gemini", "groq", "local_openai"}
    assert headers["x-muse-deployment"].endswith("__fast")
    assert headers["x-muse-privacy"] == "standard"
    time.sleep(0.5)  # success logging is async after the response
    rows = ledger(proxy).recent(5)
    assert rows and rows[0]["ok"] == 1 and rows[0]["tokens_in"] == 11 and rows[0]["tokens_out"] == 7
    assert rows[0]["alias"] == "fast" and rows[0]["privacy"] == "standard"
    for path in Path(proxy.home).rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes(), f"prompt text leaked into {path}"
    # Observed limits from x-ratelimit-* response headers.
    upstream_key = f"{rows[0]['provider']}:{'openai/' + upstream.calls()[-1].model}"
    if rows[0]["provider"] != "local_openai":
        assert {(r["window"], r["metric"]) for r in ledger(proxy).limits(upstream_key)}


def test_daily_exhaustion_is_shared_across_aliases_and_survives_restart(make_proxy, upstream):
    proxy = make_proxy(local=False)
    upstream.set("g-flash", "429_daily")
    status, _, body = proxy.chat("fast")
    assert status == 200, body  # LiteLLM moved on to q-small
    assert models(upstream).count("g-flash") == 1
    blocks = ledger(proxy).active_blocks()
    assert blocks["gemini:openai/g-flash"]["kind"] == "exhausted"
    assert "estimated" in blocks["gemini:openai/g-flash"]["reason"]

    # cheap holds a *different* LiteLLM deployment of the same upstream; LiteLLM's
    # own cooldown does not cover it. The shared block must.
    upstream.set("g-flash", "ok")
    upstream.clear_log()
    for _ in range(4):
        assert proxy.chat("cheap")[0] == 200
    assert "g-flash" not in models(upstream)

    proxy.restart()  # no in-memory cooldown survives this; only the ledger does
    upstream.clear_log()
    for alias in ("fast", "cheap"):
        status, headers, _ = proxy.chat(alias)
        assert status == 200
        assert headers["x-muse-provider"] != "gemini"
    assert "g-flash" not in models(upstream)


def test_late_success_does_not_clear_an_active_block(make_proxy, upstream):
    proxy = make_proxy(local=False)
    upstream.set("g-flash", "slow_ok")
    upstream.slow_seconds = 3.0
    result: dict = {}
    first = threading.Thread(target=lambda: result.setdefault("a", proxy.chat("g-flash")))
    first.start()
    deadline = time.time() + 10
    while not upstream.calls("g-flash") and time.time() < deadline:
        time.sleep(0.05)
    assert upstream.calls("g-flash"), "slow request never reached the upstream"
    upstream.set("g-flash", "429_daily")
    status, _, _ = proxy.chat("g-flash")
    assert status == 429
    assert "gemini:openai/g-flash" in ledger(proxy).active_blocks()
    first.join(timeout=30)
    assert result["a"][0] == 200  # the request sent before the 429 succeeded late
    time.sleep(0.5)
    assert ledger(proxy).recent(1)[0]["ok"] == 1
    assert "gemini:openai/g-flash" in ledger(proxy).active_blocks()
    upstream.set("g-flash", "ok")
    upstream.clear_log()
    assert proxy.chat("fast")[0] == 200
    assert "g-flash" not in models(upstream)


def test_local_privacy_only_reaches_local_and_fails_closed(make_proxy, upstream):
    proxy = make_proxy()
    local = {"x-muse-privacy": "local"}

    status, headers, body = proxy.chat("fast", headers=local)
    assert status == 200, body
    assert headers["x-muse-provider"] == "local_openai" and headers["x-muse-privacy"] == "local"
    assert set(models(upstream)) == {"local-model"}

    # No local deployment in cheap: 4xx naming the class, nothing sent anywhere.
    upstream.clear_log()
    status, _, body = proxy.chat("cheap", headers=local)
    assert status == 403
    assert "'local'" in body["error"]["message"]
    assert models(upstream) == []

    # Addressing a cloud deployment by id skips LiteLLM's filter; the deployment hook catches it.
    status, _, _ = proxy.chat("g-flash__fast", headers=local)
    assert status == 403
    assert models(upstream) == []

    # The local model fails; fast falls back to cheap; cheap has nothing allowed.
    for behaviour in ("500", "429_minute"):
        upstream.set("local-model", behaviour)
        upstream.clear_log()
        status, _, body = proxy.chat("fast", headers=local)
        assert 400 <= status < 600, body
        assert not (set(models(upstream)) & CLOUD), models(upstream)
    # With the local upstream now blocked (429 cooldown), the answer is a 4xx.
    status, _, body = proxy.chat("fast", headers=local)
    assert 400 <= status < 500, body
    assert not (set(models(upstream)) & CLOUD)


def test_private_class_and_unknown_class(make_proxy, upstream):
    proxy = make_proxy()
    status, headers, _ = proxy.chat("cheap", headers={"x-muse-privacy": "private"})
    assert status == 200 and headers["x-muse-deployment"] == "p-model__cheap"
    status, headers, _ = proxy.chat("fast", headers={"x-muse-privacy": "private"})
    assert status == 200 and headers["x-muse-provider"] == "local_openai"
    assert set(models(upstream)) == {"p-model", "local-model"}

    upstream.clear_log()
    status, _, body = proxy.chat("fast", headers={"x-muse-privacy": "secret-ish"})
    assert status == 400
    assert "secret-ish" in body["error"]["message"]
    assert models(upstream) == []


def test_bad_key_blocks_provider_but_caller_errors_do_not(make_proxy, upstream):
    proxy = make_proxy(local=False)
    upstream.set("q-small", "401")
    assert proxy.chat("q-small")[0] == 401
    block = ledger(proxy).active_blocks()["provider:groq"]
    assert block["kind"] == "exhausted" and "key" in block["reason"]
    assert 14 * 60 <= block["until"] - block["set_at"] <= 15 * 60 + 1

    upstream.set("q-small", "ok")
    upstream.clear_log()
    status, _, body = proxy.chat("q-small")
    assert status == 429 and "soonest reset" in str(body)
    assert models(upstream) == []  # not hammered with a known-bad key

    upstream.set("g-flash", "400")
    assert proxy.chat("g-flash")[0] == 400
    assert not any(scope.startswith(("gemini", "provider:gemini")) for scope in ledger(proxy).active_blocks())
    upstream.clear_log()
    assert proxy.chat("g-flash")[0] == 400
    assert models(upstream) == ["g-flash"]  # still contacted: a 400 is the caller's problem


def _children(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            except OSError:
                continue
            if int(fields[1]) == pid:
                out.append(int(entry.name))
    return out


def test_two_workers_share_blocks(make_proxy, upstream):
    proxy = make_proxy(workers=2, local=False)
    if os.path.isdir("/proc"):
        workers = [p for p in _children(proxy.proc.pid) if "litellm" in Path(f"/proc/{p}/cmdline").read_bytes().decode(errors="replace")
                   or "python" in Path(f"/proc/{p}/cmdline").read_bytes().decode(errors="replace")]
        assert len(workers) >= 2, f"expected 2 worker processes, saw {workers}"
    upstream.set("g-flash", "429_daily")
    assert proxy.chat("fast")[0] == 200
    assert models(upstream).count("g-flash") == 1
    # g-flash is order 1 in fast. A worker that had not seen the 429 would try it
    # first; only the shared ledger keeps every worker away.
    upstream.set("g-flash", "ok")
    upstream.clear_log()
    for _ in range(20):
        assert proxy.chat("fast")[0] == 200
    assert "g-flash" not in models(upstream)


def test_snapshot_reflects_proxy_state(make_proxy, upstream):
    proxy = make_proxy()
    assert proxy.chat("cheap", headers={"x-muse-privacy": "private"})[0] == 200  # p-model
    upstream.set("g-flash", "429_daily")
    upstream.set("q-small", "ok")
    assert proxy.chat("fast")[0] == 200
    upstream.set("q-small", "401")
    proxy.chat("q-small")
    time.sleep(0.5)

    snap = build_snapshot(environ=proxy.env)
    providers = {p["id"]: p for p in snap["providers"]}
    gem = providers["gemini"]
    assert gem["state"] == "exhausted" and gem["blocked_until"].endswith("Z")
    assert gem["deployments"][0]["state"] == "exhausted" and gem["deployments"][0]["last_status"] == 429
    assert providers["groq"]["state"] == "exhausted" and "key" in providers["groq"]["block_reason"]
    assert providers["privcloud"]["state"] == "ok"
    assert providers["privcloud"]["usage"]["day"]["requests"] == 1
    limits = providers["privcloud"]["limits"]
    assert limits and limits[0]["source"] in {"header", "estimate"}
    assert providers["local_openai"]["state"] == "ok" and providers["local_openai"]["kind"] == "local"
    aliases = {a["name"]: a for a in snap["aliases"]}
    assert aliases["fast"]["next"] == "local-model"  # gemini and groq are blocked
    assert aliases["fast"]["available"] == 1 and aliases["fast"]["total"] == 3
    assert snap["recent"] and snap["recent"][0]["ts"].endswith("Z")
    assert snap["totals"]["requests_today"] >= 2
    assert snap["proxy"]["reachable"] is None
