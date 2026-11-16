"""Routing rules, pacing, the ordering hint, the hand-off, one-off credits and the cache, end to end.

Real LiteLLM proxy with muse.auth + muse.hook from the rendered config, against
the fake upstream. Catalogue: ration_catalog.build_rules_catalog (A and B free at
one order level in chat/fast, solo = A, smart = strong S then priced paid,
spare = one-off trial T, cheap = B then T).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from ration_catalog import PRICE_IN, PRICE_OUT, build_rules_catalog, mark_oneoff
from wardmuse import apps, quota
from wardmuse.ledger import Ledger
from wardmuse.ration import parse_options


def ledger(proxy) -> Ledger:
    return Ledger(Path(proxy.home) / "ledger.sqlite", environ={"MUSE_TZ": "UTC", "MUSE_HOME": str(proxy.home)})


def wait_for(cond: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def start(make_proxy, workers: int = 1, local: bool = False, **env: str):
    base = {"MUSE_TZ": "UTC", "MUSE_ACTIVE_WINDOW_S": "0"}
    base.update(env)
    return make_proxy(workers=workers, local=local, catalog=build_rules_catalog, local_aliases=("chat",), env=base,
                      post_render=mark_oneoff)


def add(proxy, name: str, priority: str, **kw) -> str:
    _, token = apps.add_app(name, priority, environ={"MUSE_HOME": str(proxy.home)}, **kw)
    return token


def message(body: Any) -> str:
    return body["error"]["message"] if isinstance(body, dict) else str(body)


def models(upstream) -> list[str]:
    return [item.model for item in upstream.calls()]


def day_headers(remaining: int, reset: str = "12h", limit: int = 100) -> dict[str, str]:
    return {"x-ratelimit-limit-requests-day": str(limit), "x-ratelimit-remaining-requests-day": str(remaining),
            "x-ratelimit-reset-requests-day": reset}


def observe(proxy, upstream, model: str, headers: dict[str, str], remaining: int) -> None:
    """Make the ledger learn `headers` for one upstream by calling its deployment directly."""
    upstream.set_headers(model, headers)
    status, _, body = proxy.chat(model)
    assert status == 200, body
    scope = f"{'freea' if model == 'a-model' else 'freeb'}:openai/{model}"
    wait_for(lambda: any(r["window"] == "day" and r["remaining"] == remaining for r in ledger(proxy).limits(scope)))


# ---------------------------------------------------------------- a. pacing holds background work


def test_background_waits_over_the_pacing_line_and_resumes_under_it(make_proxy, upstream):
    proxy = start(make_proxy)
    batch = add(proxy, "batch", "background")

    # 12 h to reset of a day window: the line expects 50 left. 40 left is over the line
    # (surplus -0.10) but above the 30 % reserve, so only pacing holds it.
    observe(proxy, upstream, "a-model", day_headers(40), 40)
    upstream.clear_log()
    t0 = time.time()
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 429, body
    assert time.time() - t0 < 3
    assert headers["x-muse-ration"] == "defer:pace"
    assert "pacing:" in message(body) and "ahead of schedule" in message(body)
    # Released when the line catches up: start + (1 - 0.40) * 24 h = 2.4 h from now.
    assert 2.3 * 3600 < int(headers["retry-after"]) <= 2.45 * 3600
    assert models(upstream) == []  # nothing was sent upstream while held
    assert ledger(proxy).ration_counts()[("batch", "defer", "pace")] == 1

    # Normal and interactive apps are not held by pacing.
    script = add(proxy, "script", "normal")
    assert proxy.chat("solo", key=script)[0] == 200
    assert proxy.chat("solo")[0] == 200

    # Under the line (60 left, 50 expected): background may take it.
    observe(proxy, upstream, "a-model", day_headers(60), 60)
    upstream.clear_log()
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 200, body
    assert models(upstream) == ["a-model"]

    # Over the line again but inside the last hour before the reset: reserve and pacing are waived.
    observe(proxy, upstream, "a-model", day_headers(5, reset="30m"), 5)
    upstream.clear_log()
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 200, body
    assert models(upstream) == ["a-model"]


def test_pace_margin_setting(make_proxy, upstream):
    proxy = start(make_proxy, MUSE_PACE_MARGIN="0.2")
    batch = add(proxy, "batch", "background")
    observe(proxy, upstream, "a-model", day_headers(60), 60)  # surplus +0.10 < margin 0.20
    upstream.clear_log()
    status, headers, _ = proxy.chat("solo", key=batch)
    assert status == 429 and headers["x-muse-ration"] == "defer:pace"
    assert models(upstream) == []


# ---------------------------------------------------------------- b. spend what expires soonest


def test_prefers_the_upstream_with_more_to_spare_and_flips(make_proxy, upstream):
    proxy = start(make_proxy)
    observe(proxy, upstream, "a-model", day_headers(80), 80)  # surplus +0.30
    observe(proxy, upstream, "b-model", day_headers(20), 20)  # surplus -0.30
    upstream.clear_log()
    for _ in range(6):
        status, headers, body = proxy.chat("chat")
        assert status == 200, body
        assert headers["x-muse-provider"] == "freea"
        assert "pace:prefer" in headers["x-muse-ration"].split(",")
    assert set(models(upstream)) == {"a-model"}

    observe(proxy, upstream, "a-model", day_headers(20), 20)
    observe(proxy, upstream, "b-model", day_headers(80), 80)
    upstream.clear_log()
    for _ in range(6):
        status, headers, _ = proxy.chat("chat")
        assert status == 200 and headers["x-muse-provider"] == "freeb"
    assert set(models(upstream)) == {"b-model"}

    # Neither is decisive (both on the line): LiteLLM load-balances as before.
    observe(proxy, upstream, "a-model", day_headers(50), 50)
    observe(proxy, upstream, "b-model", day_headers(52), 52)
    upstream.clear_log()
    seen = set()
    for _ in range(24):
        status, headers, _ = proxy.chat("chat")
        assert status == 200 and "pace:prefer" not in headers.get("x-muse-ration", "")
        seen.add(headers["x-muse-provider"])
    assert seen == {"freea", "freeb"}

    # The preferred upstream failing still falls back to the other one (never an empty list).
    observe(proxy, upstream, "a-model", day_headers(80), 80)
    observe(proxy, upstream, "b-model", day_headers(20), 20)
    upstream.set("a-model", "500")
    status, headers, body = proxy.chat("chat")
    assert status == 200 and headers["x-muse-provider"] == "freeb", body


# ---------------------------------------------------------------- c. x-muse-task and the rules file


def test_task_header_replaces_the_model_and_caps_output(make_proxy, upstream, tmp_path):
    rules_file = tmp_path / "routing.yaml"
    rules_file.write_text((Path(__file__).resolve().parents[2] / "config" / "routing.yaml").read_text())
    proxy = start(make_proxy, MUSE_ROUTING=str(rules_file), MUSE_PACE_PREFER_GAP="2")

    status, headers, body = proxy.chat("auto", headers={"x-muse-task": "everyday"})
    assert status == 200, body
    assert headers["x-muse-task"] == "everyday" and headers["x-muse-provider"] == "freeb"  # cheap = B (T is held)
    assert upstream.calls()[-1].max_tokens == 1200

    status, headers, body = proxy.chat("smart", headers={"x-muse-task": "light"}, max_tokens=50)
    assert status == 200, body
    assert headers["x-muse-task"] == "light" and headers["x-muse-provider"] in ("freea", "freeb")  # fast, not smart
    assert upstream.calls()[-1].max_tokens == 50  # the client's lower limit is kept
    status, _, _ = proxy.chat("auto", headers={"x-muse-task": "light"}, max_tokens=5000)
    assert status == 200 and upstream.calls()[-1].max_tokens == 300

    # A hint in a task name; the default task for model auto with no header.
    status, headers, _ = proxy.chat("auto", headers={"x-muse-task": "classify-emails"})
    assert status == 200 and headers["x-muse-task"] == "light"
    status, headers, _ = proxy.chat("auto")
    assert status == 200 and headers["x-muse-task"] == "everyday"

    status, _, body = proxy.chat("auto", headers={"x-muse-task": "poetry"})
    assert status == 400
    assert "unknown task 'poetry'" in message(body) and "light, everyday, heavy, frontier, embed, rerank" in message(body)

    # Edit the file while the proxy runs: light now uses smart (S).
    text = rules_file.read_text().replace("  light:\n    alias: fast", "  light:\n    alias: smart")
    rules_file.write_text(text)
    status, headers, _ = proxy.chat("auto", headers={"x-muse-task": "light"})
    assert status == 200 and headers["x-muse-provider"] == "strong"

    # A broken file: the built-in rules (light = fast) and one log line, the proxy keeps serving.
    rules_file.write_text("tasks:\n  light: {alias: fast, max_output_tokens: lots}\n")
    for _ in range(3):
        status, headers, body = proxy.chat("auto", headers={"x-muse-task": "light"})
        assert status == 200 and headers["x-muse-provider"] in ("freea", "freeb"), body
    log = proxy.log_text()
    assert log.count("routing rules not used") == 1 and "max_output_tokens" in log


# ---------------------------------------------------------------- d. hand-off and the options trailer


def test_frontier_suggests_the_handoff_and_lists_only_real_options(make_proxy, upstream):
    proxy = start(make_proxy)
    stingy = add(proxy, "stingy", "interactive", allow_paid=False)

    status, headers, body = proxy.chat("auto", headers={"x-muse-task": "frontier"})
    assert status == 200 and headers["x-muse-provider"] == "strong", body
    assert headers["x-muse-handoff"] == "suggest" and headers["x-muse-task"] == "frontier"
    status, headers, _ = proxy.chat("auto", headers={"x-muse-task": "everyday"})
    assert status == 200 and "x-muse-handoff" not in headers

    # The strong free model's day is used up; only paid remains.
    upstream.set("s-model", "429_daily")
    proxy.chat("smart")
    wait_for(lambda: "strong:openai/s-model" in ledger(proxy).active_blocks())
    upstream.clear_log()
    reset = quota.next_midnight_utc(time.time())

    status, headers, body = proxy.chat("auto", headers={"x-muse-task": "frontier"})
    assert status == 429, body
    assert headers["x-muse-handoff"] == "suggest" and headers["x-muse-ration"].endswith("handoff-first")
    options = parse_options(message(body))
    assert set(options) == {"wait", "handoff", "pay"}
    assert options["wait"] == quota.iso(reset)
    tokens_in, tokens_out = 2, 1024  # "hello": 5 ASCII chars // 3 + 1; no max_tokens -> 1024 held
    assert options["pay"] == pytest.approx((tokens_in * PRICE_IN + tokens_out * PRICE_OUT) / 1e6, abs=1e-6)
    assert models(upstream) == []  # nothing was spent while offering

    # The user chose "pay".
    status, headers, body = proxy.chat("auto", headers={"x-muse-task": "frontier", "x-muse-allow-paid": "true"})
    assert status == 200 and headers["x-muse-provider"] == "paidp", body
    assert models(upstream) == ["pp-model"]

    # An app that may not pay: wait and hand-off only.
    status, headers, body = proxy.chat("auto", key=stingy, headers={"x-muse-task": "frontier"})
    assert status == 429 and headers["x-muse-handoff"] == "suggest", body
    assert set(parse_options(message(body))) == {"wait", "handoff"}

    # A plain request for the strong alias that cannot be served: hand-off suggested too.
    status, headers, body = proxy.chat("smart", key=stingy)
    assert status == 429 and headers["x-muse-handoff"] == "suggest"
    assert set(parse_options(message(body))) == {"wait", "handoff"}

    # Not the strong allowance: wait only, no hand-off.
    upstream.set("a-model", "429_daily")
    proxy.chat("a-model")
    wait_for(lambda: "freea:openai/a-model" in ledger(proxy).active_blocks())
    status, headers, body = proxy.chat("solo")
    assert status == 429 and "x-muse-handoff" not in headers, body
    assert set(parse_options(message(body))) == {"wait"}


# ---------------------------------------------------------------- e. one-off credits


def test_oneoff_credits_need_approval(make_proxy, upstream):
    proxy = start(make_proxy)
    status, headers, body = proxy.chat("spare")
    assert status == 429, body
    assert headers["x-muse-ration"] == "refuse:oneoff-held"
    assert "held until you approve" in message(body) and "retry-after" not in headers
    assert proxy.chat("t-model")[0] == 429  # by its own name too
    assert not upstream.calls("t-model")

    # cheap = B, then T: B answers; with B used up the request is refused, not sent to T.
    assert proxy.chat("cheap")[2]["model"]
    upstream.set("b-model", "429_daily")
    proxy.chat("b-model")
    wait_for(lambda: "freeb:openai/b-model" in ledger(proxy).active_blocks())
    status, headers, body = proxy.chat("cheap")
    assert status == 429 and headers["x-muse-ration"] == "refuse:oneoff-held", body
    assert not upstream.calls("t-model")

    status, headers, body = proxy.chat("spare", headers={"x-muse-allow-oneoff": "true"})
    assert status == 200 and headers["x-muse-provider"] == "trialx", body
    status, headers, body = proxy.chat("cheap", headers={"x-muse-allow-oneoff": "true"})
    assert status == 200 and headers["x-muse-provider"] == "trialx", body
    assert len(upstream.calls("t-model")) == 2


# ---------------------------------------------------------------- f. exact-repeat cache


def test_exact_repeat_cache(make_proxy, upstream):
    proxy = start(make_proxy, local=True, MUSE_CACHE="exact")
    zero = {"temperature": 0}
    status, headers, body = proxy.chat("solo", text="what is 2+2", extra=zero)
    assert status == 200 and headers["x-muse-cache"] == "miss", body
    wait_for(lambda: _disk_rows(proxy))
    upstream.clear_log()
    status, headers, body = proxy.chat("solo", text="what is 2+2", extra=zero)
    assert status == 200, body
    assert headers["x-muse-cache"] == "hit" and headers["x-muse-provider"] == "cache"
    assert body["choices"][0]["message"]["content"] == "fake reply"
    assert models(upstream) == []
    rows = wait_for(lambda: [r for r in ledger(proxy).recent(50) if r["cached"] == 1])
    assert rows[0]["cost_usd"] == 0 and rows[0]["tokens_in"] == 0 and rows[0]["provider"] == "cache"
    assert ledger(proxy).usage(upstream="freea:openai/a-model")["day"]["requests"] == 1  # the hit is not quota use

    # Not cached: a different privacy class, a non-zero temperature.
    status, headers, _ = proxy.chat("solo", text="what is 2+2", extra=zero, headers={"x-muse-privacy": "public"})
    assert status == 200 and headers["x-muse-cache"] == "miss"
    status, headers, _ = proxy.chat("solo", text="what is 2+2", extra={"temperature": 0.7})
    assert status == 200 and "x-muse-cache" not in headers

    # A local-class request: cached in memory only, never written to disk.
    before = _disk_rows(proxy)
    local = {"x-muse-privacy": "local"}
    status, headers, _ = proxy.chat("chat", text="my diary entry", extra=zero, headers=local)
    assert status == 200 and headers["x-muse-provider"] == "local_openai" and headers["x-muse-cache"] == "miss"
    wait_for(lambda: [r for r in ledger(proxy).recent(50) if r["privacy"] == "local" and r["ok"] == 1])
    status, headers, _ = proxy.chat("chat", text="my diary entry", extra=zero, headers=local)
    assert status == 200 and headers["x-muse-cache"] == "hit"
    rows = _disk_rows(proxy)
    assert rows == before and all(r[0] in ("public", "standard") for r in rows)


def _disk_rows(proxy) -> list[tuple]:
    path = Path(proxy.home) / "cache.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT privacy, key FROM entries ORDER BY key").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------- g. two workers


def test_two_workers(make_proxy, upstream):
    proxy = start(make_proxy, workers=2, MUSE_CACHE="exact")
    batch = add(proxy, "batch", "background")
    observe(proxy, upstream, "a-model", day_headers(40), 40)
    upstream.clear_log()
    for _ in range(6):  # both workers read the pacing position from the shared ledger
        status, headers, _ = proxy.chat("solo", key=batch)
        assert status == 429 and headers["x-muse-ration"] == "defer:pace"
    assert models(upstream) == []

    observe(proxy, upstream, "a-model", day_headers(80), 80)
    observe(proxy, upstream, "b-model", day_headers(20), 20)
    upstream.clear_log()
    for i in range(6):
        status, headers, _ = proxy.chat("auto", text=f"q{i}", headers={"x-muse-task": "light"})
        assert status == 200 and headers["x-muse-provider"] == "freea" and headers["x-muse-task"] == "light"
    assert all(c.max_tokens == 300 for c in upstream.calls())

    for _ in range(4):
        status, headers, _ = proxy.chat("spare")
        assert status == 429 and headers["x-muse-ration"] == "refuse:oneoff-held"
    assert not upstream.calls("t-model")

    # The disk cache is shared: whichever worker answers, a repeat is a hit.
    stored = len(_disk_rows(proxy))  # the light-task answers above are cached too (deterministic task)
    assert proxy.chat("solo", text="shared?", extra={"temperature": 0})[0] == 200
    wait_for(lambda: len(_disk_rows(proxy)) == stored + 1)
    upstream.clear_log()
    hits = [proxy.chat("solo", text="shared?", extra={"temperature": 0})[1].get("x-muse-cache") for _ in range(6)]
    assert hits == ["hit"] * 6 and models(upstream) == []
