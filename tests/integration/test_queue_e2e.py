"""The queue runner against the REAL proxy: it takes free allowance, steps aside, stops at caps, never pays."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from muse import apps, keys, queue
from ration_catalog import MASTER_KEY
from test_ration_e2e import add as add_app, ledger, models, ok_rows, start, wait_for


@pytest.fixture()
def qenv(make_proxy, upstream, tmp_path, monkeypatch):
    def build(**proxy_env):
        proxy = start(make_proxy, local=False, **proxy_env)
        monkeypatch.setitem(sys.modules, "keyring", None)
        monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "q.env"))
        env = dict(keys.export_env({"MUSE_HOME": str(proxy.home), "MUSE_PROXY_URL": proxy.url, "MUSE_TZ": "UTC",
                                    "MUSE_ENV_FILE": str(tmp_path / "q.env"), "PATH": "/usr/bin"}))
        return proxy, env

    return build


def enqueue(env, text="hello", alias="solo", **kw):
    return queue.Queue(env).add(kind="prompt", payload=text, alias=alias, **kw)


def test_queue_drains_on_a_free_upstream_as_a_background_app(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0")
    jobs = [enqueue(env, f"job {i}") for i in range(3)]
    summary = queue.Runner(env).run(until_idle=True)
    assert summary.done == 3 and summary.stopped == "idle"
    for j in jobs:
        done = queue.Queue(env).get(j.id)
        assert done.status == "done" and done.provider == "freea" and Path(done.out_path).read_text().strip() == "fake reply"
        assert done.tokens_in == 11 and done.tokens_out == 7 and done.payload == ""
    assert models(upstream) == ["a-model"] * 3
    wait_for(lambda: len(ok_rows(proxy, app="queue")) == 3)
    row = ok_rows(proxy, app="queue")[0]
    assert row["priority"] == "background" and row["paid"] in (0, None)
    assert apps.load_apps(env)["queue"].allow_paid is False
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")
    for path in Path(proxy.home).rglob("*"):  # the ledger never holds a prompt, and no file holds the app token
        if path.is_file() and path.name not in ("q.env", "apps.yaml"):
            data = path.read_bytes()
            assert b"job 1" not in data or path.parent.name == "queue-out" or path.suffix == ".txt"
            assert env["MUSE_QUEUE_APP_TOKEN"].encode() not in data
    assert b"job 1" not in (Path(proxy.home) / "ledger.sqlite").read_bytes()


def test_queue_yields_to_an_active_app_then_proceeds(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="3")
    assert proxy.chat("solo")[0] == 200  # you, on the owner key, are using A
    wait_for(lambda: ok_rows(proxy, app="owner"))
    upstream.clear_log()
    j = enqueue(env)
    queue.Runner(env).run(once=True)
    d = queue.Queue(env).get(j.id)
    assert d.status == "deferred" and d.last_reason == "defer:yield" and d.attempts == 0
    assert models(upstream) == []  # nothing was sent upstream on the queue's behalf
    assert 0 < d.next_attempt_at - time.time() <= 4.5
    assert ledger(proxy).ration_counts()[("queue", "defer", "yield")] >= 1
    time.sleep(3.2)  # the active window passes
    summary = queue.Runner(env).run(until_idle=True)
    assert summary.done == 1 and models(upstream) == ["a-model"]


def test_queue_stops_at_its_app_cap_and_holds_the_rest(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0")
    first = enqueue(env, "one")
    queue.Runner(env).run(once=True)
    assert queue.Queue(env).get(first.id).status == "done"
    wait_for(lambda: len(ok_rows(proxy, app="queue")) == 1)
    apps.update_app("queue", env, daily_requests=1)
    time.sleep(0.2)
    a, b = enqueue(env, "two"), enqueue(env, "three")
    upstream.clear_log()
    queue.Runner(env).run(once=True)
    assert models(upstream) == []
    da, db = queue.Queue(env).get(a.id), queue.Queue(env).get(b.id)
    assert da.last_reason == "refuse:app-requests" and da.attempts == 0 and db.last_reason is None  # b was never even tried
    assert queue.stats(env)["hold_until"] is not None
    assert ledger(proxy).ration_counts()[("queue", "refuse", "app-requests")] == 1  # one request, not one per job


def test_queue_never_touches_a_paid_upstream_by_default(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0")
    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    enqueue(env, alias="chat")
    queue.Runner(env).run(once=True)
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")
    assert ledger(proxy).paid_spend()["calls"] == 0


def test_a_paid_job_uses_the_paid_app_and_the_cap_still_applies(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0", MUSE_DAILY_PAID_CAP_USD="0")
    upstream.set("a-model", "429_daily")
    upstream.set("b-model", "429_daily")
    j = enqueue(env, alias="chat", allow_paid=True)
    queue.Runner(env).run(once=True)
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")  # cap 0: allowed to ask, never served
    assert queue.Queue(env).get(j.id).status != "done"


def test_queue_respects_pause(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0")
    j = enqueue(env)
    queue.set_paused(env)
    assert queue.Runner(env).run(once=True).stopped == "paused" and models(upstream) == []
    queue.clear_paused(env)
    assert queue.Runner(env).run(once=True).done == 1


def test_queue_app_survives_a_rotation_while_the_proxy_runs(qenv, upstream):
    proxy, env = qenv(MUSE_ACTIVE_WINDOW_S="0")
    enqueue(env, "a")
    queue.Runner(env).run(once=True)
    apps.rotate_token("queue", env)  # `muse apps rotate queue`, proxy still running
    time.sleep(0.2)
    enqueue(env, "b")
    assert queue.Runner(env).run(until_idle=True).done == 1
