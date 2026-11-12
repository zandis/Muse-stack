"""Rationing end to end: real LiteLLM proxy, muse.auth + muse.hook from the rendered config, fake upstreams.

Catalogue (conftest.build_ration_catalog): free A (a-model, scripted rate-limit
headers), free B (b-model), priced paid (pp-model, $0.0005 a fake call), unpriced
paid (pu-model), local (local-model). Aliases: chat (all), solo (A only) with
fallback other (A only).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from ration_catalog import MASTER_KEY, PRICE_IN, PRICE_OUT, build_ration_catalog
from muse import apps
from muse.ledger import Ledger

CALL_COST = (11 * PRICE_IN + 7 * PRICE_OUT) / 1e6  # the fake upstream always reports 11 + 7 tokens


def ledger(proxy) -> Ledger:
    return Ledger(Path(proxy.home) / "ledger.sqlite", environ={"MUSE_TZ": "UTC", "MUSE_HOME": str(proxy.home)})


def models(upstream) -> list[str]:
    return [item.model for item in upstream.calls()]


def wait_for(cond: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def start(make_proxy, workers: int = 1, local: bool = True, master_key: str | None = MASTER_KEY, **env: str):
    base = {"MUSE_TZ": "UTC"}
    base.update(env)
    return make_proxy(workers=workers, local=local, catalog=build_ration_catalog, local_aliases=("chat",), env=base,
                      master_key=master_key)


def add(proxy, name: str, priority: str, **kw) -> str:
    _, token = apps.add_app(name, priority, environ={"MUSE_HOME": str(proxy.home)}, **kw)
    return token


def message(body: Any) -> str:
    return body["error"]["message"] if isinstance(body, dict) else str(body)


def ok_rows(proxy, **match) -> list[dict]:
    return [r for r in ledger(proxy).recent(500) if r["ok"] == 1 and all(r[k] == v for k, v in match.items())]


# ---------------------------------------------------------------- a. tokens

def test_tokens_select_the_app(make_proxy, upstream):
    proxy = start(make_proxy)
    status, headers, body = proxy.chat("solo")
    assert status == 200, body
    assert headers["x-muse-app"] == "owner"

    # Registered while the proxy runs: picked up without a restart.
    bot = add(proxy, "bot", "interactive")
    batch = add(proxy, "batch", "background")
    status, headers, _ = proxy.chat("solo", key=bot)
    assert status == 200 and headers["x-muse-app"] == "bot"

    status, headers, body = proxy.chat("solo", key="sk-not-a-registered-token-12345")
    assert status == 401 and "[muse] unknown API key" in message(body)
    assert "sk-not-a-registered-token-12345" not in str(body) and "sk-not-a-registered-token-12345" not in proxy.log_text()

    status, headers, _ = proxy.chat("chat", headers={"x-muse-app": "batch"})
    assert status == 200 and headers["x-muse-app"] == "batch"
    status, _, body = proxy.chat("chat", key=bot, headers={"x-muse-app": "batch"})
    assert status == 403 and "cannot switch app" in message(body)
    status, _, body = proxy.chat("chat", headers={"x-muse-app": "ghost"})
    assert status == 400 and "'ghost'" in message(body)

    assert proxy.get("/v1/models", key=batch)[0] == 200
    assert proxy.get("/key/info", key=batch)[0] == 403  # app tokens: model endpoints only

    wait_for(lambda: {r["app"] for r in ok_rows(proxy)} >= {"owner", "bot", "batch"})
    usage = ledger(proxy).usage_by_app()
    assert usage["owner"]["requests"] == 1 and usage["bot"]["requests"] == 1 and usage["batch"]["requests"] == 1
    rows = {r["app"]: r for r in ok_rows(proxy)}
    assert rows["batch"]["priority"] == "background" and rows["bot"]["priority"] == "interactive"
    for token in (bot, batch, MASTER_KEY):
        for path in Path(proxy.home).rglob("*"):
            if path.is_file():
                assert token.encode() not in path.read_bytes(), f"a token leaked into {path}"
        assert token not in proxy.log_text()


# ---------------------------------------------------------------- b. yield

def test_background_yields_to_the_active_app(make_proxy, upstream):
    proxy = start(make_proxy, MUSE_ACTIVE_WINDOW_S="4")
    batch = add(proxy, "batch", "background")
    assert proxy.chat("solo")[0] == 200  # owner, interactive, uses A
    wait_for(lambda: ok_rows(proxy, app="owner"))

    upstream.clear_log()
    t0 = time.time()
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 429, body
    assert time.time() - t0 < 3, "a deferral must answer at once"
    assert "yielding to the active app; retry in" in message(body)
    assert headers["x-muse-ration"] == "defer:yield" and 1 <= int(headers["retry-after"]) <= 4
    assert headers["x-muse-app"] == "batch"
    assert models(upstream) == []  # nothing reached A, not via solo's fallback 'other' either

    # Naming the deployment id skips LiteLLM's filter; the deployment hook still defers.
    status, headers, body = proxy.chat("a-model__solo", key=batch)
    assert status == 429 and "yielding" in message(body), body
    assert models(upstream) == []

    # No block, no cooldown, no failure row: the owner is served by A at once.
    db = ledger(proxy)
    assert db.active_blocks() == {}
    assert all(r["ok"] == 1 for r in db.recent(50))
    status, headers, _ = proxy.chat("solo")
    assert status == 200 and models(upstream) == ["a-model"]
    status, _, _ = proxy.chat("a-model__solo")
    assert status == 200
    counts = db.ration_counts()
    assert counts[("batch", "defer", "yield")] >= 2

    time.sleep(4.5)
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 200, body
    assert headers["x-muse-provider"] == "freea" and "x-muse-ration" not in headers


def test_background_takes_another_upstream_and_says_so(make_proxy, upstream):
    proxy = start(make_proxy, local=False)
    batch = add(proxy, "batch", "background")
    assert proxy.chat("solo")[0] == 200  # owner on A
    wait_for(lambda: ok_rows(proxy, app="owner"))
    upstream.clear_log()
    status, headers, body = proxy.chat("chat", key=batch)
    assert status == 200, body
    assert headers["x-muse-provider"] == "freeb" and headers["x-muse-ration"].split(",") == ["paid-not-allowed", "yield"]
    assert models(upstream) == ["b-model"]


# ---------------------------------------------------------------- c. reserve and expiring

def day_headers(remaining: int, reset: str = "5h") -> dict[str, str]:
    return {"x-ratelimit-limit-requests-day": "100", "x-ratelimit-remaining-requests-day": str(remaining),
            "x-ratelimit-reset-requests-day": reset}


def test_reserve_and_use_it_before_it_resets(make_proxy, upstream):
    proxy = start(make_proxy, local=False, MUSE_ACTIVE_WINDOW_S="0")
    batch = add(proxy, "batch", "background")
    script = add(proxy, "script", "normal")

    def remaining() -> int | None:
        rows = [r for r in ledger(proxy).limits("freea:openai/a-model") if r["window"] == "day"]
        return rows[0]["remaining"] if rows else None

    upstream.set_headers("a-model", day_headers(25))
    assert proxy.chat("solo", key=script)[0] == 200
    wait_for(lambda: remaining() == 25)

    upstream.clear_log()
    status, headers, body = proxy.chat("solo", key=batch)  # 25 % < 30 %
    assert status == 429 and headers["x-muse-ration"] == "defer:reserve", body
    assert "keeping a reserve for interactive use" in message(body)
    assert 4 * 3600 < int(headers["retry-after"]) <= 5 * 3600
    assert models(upstream) == []

    upstream.set_headers("a-model", day_headers(10))
    assert proxy.chat("solo", key=script)[0] == 200  # 24 left after it: above 10 %
    wait_for(lambda: remaining() == 10)
    status, headers, _ = proxy.chat("solo", key=script)  # 9 % after it
    assert status == 429 and headers["x-muse-ration"] == "defer:reserve"
    status, _, _ = proxy.chat("solo")  # interactive: no reserve
    assert status == 200

    # Inside the last hour of the day window: background may soak it up.
    upstream.set_headers("a-model", day_headers(10, reset="30m"))
    assert proxy.chat("solo")[0] == 200
    wait_for(lambda: any(r["reset_at"] and r["reset_at"] - time.time() < 1900 for r in ledger(proxy).limits("freea:openai/a-model")))
    upstream.clear_log()
    status, headers, body = proxy.chat("solo", key=batch)
    assert status == 200, body
    assert proxy.chat("solo", key=script)[0] == 200
    assert models(upstream) == ["a-model", "a-model"]


# ---------------------------------------------------------------- d. paid cap

def test_paid_cap(make_proxy, upstream):
    proxy = start(make_proxy, local=False, MUSE_DAILY_PAID_CAP_USD="0.001")
    stingy = add(proxy, "stingy", "normal", allow_paid=False)

    # The unpriced paid provider: callable by its own name, recorded as unknown cost.
    status, headers, body = proxy.chat("pu-model")
    assert status == 200, body
    assert "x-muse-cost-usd" not in headers
    wait_for(lambda: ledger(proxy).paid_spend()["unknown_cost_calls"] == 1)

    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    status, headers, body = proxy.chat("chat")
    assert status == 200 and headers["x-muse-provider"] == "paidp", body
    assert float(headers["x-muse-cost-usd"]) == pytest.approx(CALL_COST)
    wait_for(lambda: ledger(proxy).paid_spend()["usd"] > 0)

    # An app without allow_paid never reaches a paid upstream.
    before = len(upstream.calls("pp-model"))
    status, headers, body = proxy.chat("chat", key=stingy)
    assert status == 429, body
    assert len(upstream.calls("pp-model")) == before and not upstream.calls("pu-model")[1:]
    status, _, body = proxy.chat("pp-model", key=stingy)
    assert status == 429 and "may not use paid providers" in message(body)
    assert len(upstream.calls("pp-model")) == before

    status, headers, _ = proxy.chat("chat")  # spend 0.0005 < 0.001: may cross
    assert status == 200 and headers["x-muse-provider"] == "paidp"
    wait_for(lambda: ledger(proxy).paid_spend()["usd"] >= 0.001 - 1e-12)

    status, headers, body = proxy.chat("chat")
    assert status == 429, body
    assert "daily paid cap of $0.001 reached" in message(body)
    assert headers["x-muse-ration"] == "defer:paid-cap" and int(headers["retry-after"]) > 0
    status, _, body = proxy.chat("pp-model")
    assert status == 429 and "daily paid cap" in message(body)

    assert len(upstream.calls("pp-model")) == 2
    assert len(upstream.calls("pu-model")) == 1  # only the direct call
    rows = [r for r in ledger(proxy).recent(100) if r["paid"] == 1 and r["ok"] == 1]
    priced = [r for r in rows if r["deployment_id"].startswith("pp-model")]
    assert len(priced) == 2 and all(r["cost_usd"] == pytest.approx(CALL_COST) for r in priced)
    assert all(r["app"] == "owner" for r in rows)
    spend = ledger(proxy).paid_spend()
    assert spend["usd"] == pytest.approx(2 * CALL_COST) and spend["unknown_cost_calls"] == 1 and spend["held_usd"] == 0


def test_paid_cap_zero_never_pays(make_proxy, upstream):
    proxy = start(make_proxy, local=False, MUSE_DAILY_PAID_CAP_USD="0")
    rendered = yaml.safe_load((proxy.workdir / "litellm.yaml").read_text())
    chat = [e["model_info"]["id"] for e in rendered["model_list"] if e["model_name"] == "chat"]
    assert chat == ["a-model__chat", "b-model__chat"]  # paid stays pinned-only, as before the cap existed
    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    assert proxy.chat("chat")[0] == 429
    for name in ("pp-model", "pu-model"):
        status, _, body = proxy.chat(name)
        assert status == 429 and "cap is 0" in message(body)
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")


# ---------------------------------------------------------------- e. per-app cap

def test_per_app_daily_request_cap(make_proxy, upstream):
    proxy = start(make_proxy, local=False)
    capped = add(proxy, "capped", "normal", daily_requests=2)
    for _ in range(2):
        assert proxy.chat("chat", key=capped)[0] == 200
        time.sleep(0.3)
    wait_for(lambda: len(ok_rows(proxy, app="capped")) == 2)
    upstream.clear_log()
    status, headers, body = proxy.chat("chat", key=capped)
    assert status == 429, body
    assert "app 'capped' reached its daily request cap (2)" in message(body)
    assert headers["x-muse-ration"] == "refuse:app-requests" and int(headers["retry-after"]) > 0
    assert models(upstream) == []
    assert proxy.chat("chat")[0] == 200  # other apps are unaffected


# ---------------------------------------------------------------- f. privacy still wins

def test_local_request_from_background_waits_and_never_goes_to_cloud(make_proxy, upstream):
    proxy = start(make_proxy)
    batch = add(proxy, "batch", "background")
    local = {"x-muse-privacy": "local"}
    status, headers, _ = proxy.chat("chat", headers=local)
    assert status == 200 and headers["x-muse-provider"] == "local_openai"
    wait_for(lambda: ok_rows(proxy, app="owner"))
    upstream.clear_log()
    status, headers, body = proxy.chat("chat", key=batch, headers=local)
    assert status == 429 and headers["x-muse-ration"] == "defer:yield", body
    assert models(upstream) == []  # neither the busy local model nor any cloud one


def test_private_request_never_reaches_a_paid_provider_that_may_train(make_proxy, upstream):
    proxy = start(make_proxy, local=False)
    upstream.set("a-model", "429_daily")
    status, _, body = proxy.chat("chat", headers={"x-muse-privacy": "private"})
    assert status == 403 and "'private'" in message(body)
    assert models(upstream) == []
    # (A paid provider that is marked no-train/no-review does get private requests:
    # test_proxy_e2e.test_private_class_and_unknown_class, privcloud.)


# ---------------------------------------------------------------- g. two workers

def test_two_workers_agree_through_the_ledger(make_proxy, upstream):
    proxy = start(make_proxy, workers=2, local=False, MUSE_ACTIVE_WINDOW_S="60", MUSE_DAILY_PAID_CAP_USD="0.001")
    batch = add(proxy, "batch", "background")
    capped = add(proxy, "capped", "normal", daily_requests=3)

    assert proxy.chat("solo")[0] == 200
    wait_for(lambda: ok_rows(proxy, app="owner"))
    upstream.clear_log()
    assert [proxy.chat("solo", key=batch)[0] for _ in range(10)] == [429] * 10
    assert models(upstream) == []

    served = 0
    for _ in range(8):
        status = proxy.chat("chat", key=capped)[0]
        served += status == 200
        if status == 200:
            wait_for(lambda: len(ok_rows(proxy, app="capped")) == served)
    assert served == 3

    # Paid cap under concurrency: holds are taken atomically in the shared ledger.
    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    for _ in range(3):  # let both workers learn the blocks through the ledger
        proxy.chat("chat", max_tokens=7)
    wait_for(lambda: "freea:openai/a-model" in ledger(proxy).active_blocks() and "freeb:openai/b-model" in ledger(proxy).active_blocks())
    wait_for(lambda: ledger(proxy).paid_spend()["held_usd"] == 0)
    results: list[int] = []

    def fire() -> None:
        results.append(proxy.chat("chat", max_tokens=7)[0])

    threads = [threading.Thread(target=fire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    wait_for(lambda: ledger(proxy).paid_spend()["held_usd"] == 0)
    spend = ledger(proxy).paid_spend()
    paid_calls = len(upstream.calls("pp-model"))
    assert spend["calls"] == paid_calls
    # Never more than the crossing call beyond the cap.
    assert spend["usd"] <= 0.001 + CALL_COST + 1e-12, (spend, results)
    assert paid_calls <= 3
    assert results.count(429) >= 8 - (3 - (paid_calls - results.count(200)))  # every other request was refused
    assert not upstream.calls("pu-model")


# ---------------------------------------------------------------- h. boot expectations

def test_master_key_boot_expectations(make_proxy, upstream):
    proxy = start(make_proxy)
    status, body = proxy.get("/v1/models")
    assert status == 200 and {"chat", "solo", "other", "a-model", "pp-model"} <= {m["id"] for m in body["data"]}
    assert proxy.get("/model/info")[0] == 200
    assert proxy.get("/health/liveliness", key=None)[0] == 200
    assert proxy.get("/v1/models", key=None)[0] == 401


def test_no_key_local_dev_mode_boots(make_proxy, upstream):
    proxy = start(make_proxy, master_key=None)
    status, body = proxy.get("/v1/models", key=None)
    assert status == 200 and "chat" in {m["id"] for m in body["data"]}
    status, headers, body = proxy.chat("chat", key=None)
    assert status == 200 and headers["x-muse-app"] == "anonymous", body
    status, headers, _ = proxy.chat("chat", key="whatever-a-client-sends")
    assert status == 200 and headers["x-muse-app"] == "anonymous"
    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    upstream.set("local-model", "500")
    proxy.chat("chat", key=None)
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")  # anonymous never pays
