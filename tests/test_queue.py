"""muse.queue: the runner's state machine against a scripted stand-in for the proxy and a fake clock."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import random
import sqlite3
import stat
import sys
import threading
from pathlib import Path

import pytest

from wardmuse import apps, keys, queue
from wardmuse.ration import local_day_bounds, resolve_tz
from queue_fakes import CANARY_PROMPT, CANARY_REPLY, Clock, FakeProxy, completion, error


@pytest.fixture()
def proxy():
    p = FakeProxy()
    yield p
    p.stop()


@pytest.fixture()
def env(tmp_path, monkeypatch, _isolated_muse_home, proxy):
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_PROXY_URL", proxy.url)
    monkeypatch.setenv("MUSE_TZ", "UTC")
    return dict(keys.export_env())


@pytest.fixture()
def clock():
    return Clock()


def make(env, clock, **kw):
    logs: list[str] = []
    runner = queue.Runner(env, clock=clock.now, sleep=clock.sleep, rng=random.Random(7), log=logs.append, **kw)
    runner.logs = logs  # type: ignore[attr-defined]
    return runner


def add(env, clock, prompt="hello", **kw) -> queue.Job:
    kw.setdefault("kind", "prompt")
    clock.t += 0.001  # distinct creation times keep "oldest first" deterministic
    return queue.Queue(env, clock.now).add(payload=prompt, **kw)


def job(env, clock, jid):
    return queue.Queue(env, clock.now).get(jid)


# ---------------------------------------------------------------- success


def test_success_writes_atomically_and_erases_the_prompt(env, clock, proxy, tmp_path):
    out = tmp_path / "result.txt"
    j = add(env, clock, CANARY_PROMPT, alias="cheap", privacy="private", out_path=str(out), max_tokens=77)
    runner = make(env, clock)
    summary = runner.run(once=True)
    assert summary.done == 1 and summary.stopped in ("once", "idle")
    assert out.read_text() == CANARY_REPLY + "\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    req = proxy.requests[0]
    assert req["model"] == "cheap" and req["privacy"] == "private" and req["max_tokens"] == 77 and req["prompt"] == CANARY_PROMPT
    token = envget(env, "MUSE_QUEUE_APP_TOKEN")
    assert req["auth"] == f"Bearer {token}"
    done = job(env, clock, j.id)
    assert done.status == "done" and done.provider == "freea" and done.deployment == "a-model"
    assert done.cost_usd == pytest.approx(0.0005) and (done.tokens_in, done.tokens_out) == (11, 7)
    assert done.payload == ""  # the prompt is gone once the job is finished
    raw = Path(queue.paths.queue_path(env)).read_bytes()
    assert CANARY_PROMPT.encode() not in raw and CANARY_REPLY.encode() not in raw
    assert stat.S_IMODE(queue.paths.queue_path(env).stat().st_mode) == 0o600
    assert not any(CANARY_PROMPT in line or CANARY_REPLY in line for line in runner.logs)


def test_default_output_goes_under_queue_out(env, clock, proxy):
    j = add(env, clock)
    make(env, clock).run(once=True)
    out = Path(job(env, clock, j.id).out_path)
    assert out.parent == queue.paths.queue_out_dir(env) and out.read_text().strip() == CANARY_REPLY


def test_oldest_first(env, clock, proxy):
    ids = []
    for i in range(3):
        ids.append(add(env, clock, f"p{i}").id)
        clock.t += 1
    make(env, clock).run(once=True)
    assert [r["prompt"] for r in proxy.requests] == ["p0", "p1", "p2"]


# ---------------------------------------------------------------- managed apps


def test_runner_creates_a_background_non_paid_app_and_keeps_its_token_private(env, clock, proxy):
    add(env, clock)
    make(env, clock).run(once=True)
    app = apps.load_apps(env)["queue"]
    assert app.priority == "background" and app.allow_paid is False
    token = envget(env, "MUSE_QUEUE_APP_TOKEN")
    assert apps.hash_token(token) == app.token_sha256
    assert keys.source_of("MUSE_QUEUE_APP_TOKEN", env) in ("env", "dotenv")


def test_ensure_app_repairs_after_rotation_and_forces_non_paid(env, clock):
    t1 = queue.ensure_app("queue", env)
    assert queue.ensure_app("queue", env) == t1  # idempotent
    apps.rotate_token("queue", env)
    t2 = queue.ensure_app("queue", env)
    assert t2 != t1 and apps.find_by_token(t2, env).name == "queue"
    apps.update_app("queue", env, allow_paid=True)
    queue.ensure_app("queue", env)
    assert apps.load_apps(env)["queue"].allow_paid is False


def test_allow_paid_job_uses_the_paid_app_never_the_plain_one(env, clock, proxy):
    add(env, clock, "free")
    add(env, clock, "paid", allow_paid=True)
    make(env, clock).run(once=True)
    tokens = {r["prompt"]: r["auth"].split()[1] for r in proxy.requests}
    assert tokens["free"] != tokens["paid"]
    assert apps.find_by_token(tokens["free"], env).allow_paid is False
    assert apps.find_by_token(tokens["paid"], env).name == "queue-paid"
    assert apps.find_by_token(tokens["paid"], env).allow_paid is True
    assert apps.find_by_token(tokens["paid"], env).priority == "background"


def test_close_deadline_does_not_escalate_to_paid(env, clock, proxy):
    j = add(env, clock, deadline=clock.t + 30)
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "20"})
    runner = make(env, clock)
    runner.run(once=True)
    clock.t += 25  # 5 s before the deadline: still the plain app
    runner.run(once=True)
    assert all(apps.find_by_token(r["auth"].split()[1], env).name == "queue" for r in proxy.requests)
    assert "queue-paid" not in apps.load_apps(env)


# ---------------------------------------------------------------- responses


def test_defer_requeues_at_retry_after_plus_jitter_without_an_attempt(env, clock, proxy):
    j = add(env, clock)
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "10"}, error("[muse] yielding"))
    runner = make(env, clock)
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "deferred" and d.attempts == 0 and d.deferrals == 1 and d.last_reason == "defer:yield"
    assert clock.t + 10 <= d.next_attempt_at <= clock.t + 10 + 2.0001
    assert runner.run(once=True).stopped == "once" and len(proxy.requests) == 1  # not retried early
    clock.t = d.next_attempt_at
    assert runner.run(once=True).done == 1 and len(proxy.requests) == 2


def test_daemon_sleeps_until_the_due_time_in_short_slices_and_never_zero(env, clock, proxy):
    j = add(env, clock)
    proxy.push(429, {"x-muse-ration": "defer:reserve", "retry-after": "600"})
    summary = make(env, clock).run(until_idle=True)
    assert summary.done == 1 and summary.stopped == "idle"
    assert len(proxy.requests) == 2
    assert all(0 < s <= queue.SLICE_S for s in clock.sleeps)
    assert 600 <= sum(clock.sleeps) <= 600 + 125 + queue.SLICE_S  # woke for the due time, not before
    assert len(clock.sleeps) <= (600 + 125) / queue.SLICE_S + 3  # no tight loop


def test_app_cap_holds_every_job_until_retry_after(env, clock, proxy):
    a, b = add(env, clock, "a"), add(env, clock, "b")
    proxy.push(429, {"x-muse-ration": "refuse:app-requests", "retry-after": "3600"})
    runner = make(env, clock)
    runner.run(once=True)
    assert len(proxy.requests) == 1  # b was not even tried
    assert job(env, clock, a.id).last_reason == "refuse:app-requests" and job(env, clock, a.id).attempts == 0
    assert queue.Queue(env, clock.now).holds()["queue"] >= clock.t + 3600
    clock.t += 1800
    runner.run(once=True)
    assert len(proxy.requests) == 1
    clock.t += 1900
    assert runner.run(once=True).done == 2


def test_app_token_cap_same_as_request_cap(env, clock, proxy):
    add(env, clock)
    proxy.push(429, {"x-muse-ration": "refuse:app-tokens", "retry-after": "120"})
    make(env, clock).run(once=True)
    assert queue.Queue(env, clock.now).holds()["queue"] >= clock.t + 120


@pytest.mark.parametrize("header,expect_low,expect_high", [
    ("refuse:paid-spend-unknown", 60, 60 + 12.01),
    ("defer:paid-spend-unknown", 60, 60 + 12.01),
])
def test_paid_spend_unknown_retries_in_about_a_minute(env, clock, proxy, header, expect_low, expect_high):
    j = add(env, clock)
    proxy.push(429, {"x-muse-ration": header, "retry-after": "5"})
    make(env, clock).run(once=True)
    d = job(env, clock, j.id)
    assert expect_low <= d.next_attempt_at - clock.t <= expect_high and d.attempts == 0


def test_refuse_paid_waits_until_local_midnight_without_retry_after(env, clock, proxy):
    j = add(env, clock)
    proxy.push(429, {"x-muse-ration": "refuse:paid-cap"})
    make(env, clock).run(once=True)
    midnight = local_day_bounds(clock.t, resolve_tz("UTC"))[1]
    d = job(env, clock, j.id)
    assert midnight <= d.next_attempt_at <= midnight + 30.01 and d.attempts == 0 and d.last_reason == "refuse:paid-cap"


def test_defer_paid_cap_uses_retry_after(env, clock, proxy):
    j = add(env, clock)
    proxy.push(429, {"x-muse-ration": "defer:paid-cap", "retry-after": "5000"})
    make(env, clock).run(once=True)
    assert 5000 <= job(env, clock, j.id).next_attempt_at - clock.t <= 5030.01


def test_provider_429_backs_off_exponentially_with_attempts_and_fails_at_the_limit(env, clock, proxy):
    j = add(env, clock, max_attempts=4)
    for _ in range(4):
        proxy.push(429, {}, error("rate limited"))
    runner = make(env, clock)
    waits = []
    for attempt in range(1, 4):
        runner.run(once=True)
        d = job(env, clock, j.id)
        assert d.status == "deferred" and d.attempts == attempt and d.last_reason == "http-429"
        waits.append(d.next_attempt_at - clock.t)
        clock.t = d.next_attempt_at
    for n, w in enumerate(waits):
        full = 30 * 2 ** n
        assert 0.5 * full <= w <= full
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "failed" and d.last_reason == "max-attempts:http-429" and d.payload == ""
    assert len(proxy.requests) == 4


def test_backoff_is_capped_and_never_earlier_than_retry_after(env, clock, proxy):
    j = add(env, clock, max_attempts=50)
    runner = make(env, clock)
    for _ in range(12):
        proxy.push(429, {}, error("slow down"))
        runner.run(once=True)
        d = job(env, clock, j.id)
        assert d.next_attempt_at - clock.t <= queue.BACKOFF_CAP_S + 0.01
        clock.t = d.next_attempt_at
    proxy.push(500, {"retry-after": "7200"}, error("boom", 500))
    runner.run(once=True)
    assert job(env, clock, j.id).next_attempt_at - clock.t >= 7200


def test_provider_limit_wait_is_used_instead_of_the_backoff_guess(env, clock, proxy):
    """`x-muse-wait: provider-limit` + Retry-After: the proxy knows when the block ends."""
    j = add(env, clock, max_attempts=50)
    runner = make(env, clock)
    for _ in range(6):  # a late attempt, where the plain backoff would be 8..16 minutes
        proxy.push(429, {}, error("slow down"))
        runner.run(once=True)
        clock.t = job(env, clock, j.id).next_attempt_at
    proxy.push(429, {"x-muse-wait": "provider-limit", "retry-after": "58"}, error("slow down"))
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert 58 <= d.next_attempt_at - clock.t <= 58 + 11.6 + 0.01  # Retry-After + at most 20 % jitter
    assert d.attempts == 7 and d.last_reason == "http-429"  # the provider was asked: still an attempt
    # A huge value is capped like any Retry-After; without the header the old rule holds.
    clock.t = d.next_attempt_at
    proxy.push(429, {"x-muse-wait": "provider-limit", "retry-after": "999999"}, error("slow down"))
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert queue.RETRY_AFTER_CAP_S <= d.next_attempt_at - clock.t <= queue.RETRY_AFTER_CAP_S + 30.01
    clock.t = d.next_attempt_at
    proxy.push(429, {"x-muse-wait": "provider-limit"}, error("slow down"))  # no Retry-After: nothing to prefer
    runner.run(once=True)
    assert job(env, clock, j.id).next_attempt_at - clock.t <= queue.BACKOFF_CAP_S + 0.01


def test_5xx_and_unreadable_200_are_attempts(env, clock, proxy):
    j = add(env, clock)
    proxy.push(503, {}, error("down", 503))
    proxy.push(200, {}, b"not json at all")
    runner = make(env, clock)
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert (d.attempts, d.last_reason) == (1, "http-503")
    clock.t = d.next_attempt_at
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert (d.attempts, d.last_reason) == (2, "bad-response")


def test_privacy_refusal_is_final(env, clock, proxy):
    j = add(env, clock, privacy="local")
    proxy.push(403, {}, error("[muse] no provider is allowed for privacy class 'local'", 403))
    make(env, clock).run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "failed" and d.last_reason == "privacy-refused" and len(proxy.requests) == 1


@pytest.mark.parametrize("status,reason", [(403, "http-403"), (400, "http-400"), (404, "http-404")])
def test_other_client_errors_fail_without_retry(env, clock, proxy, status, reason):
    j = add(env, clock)
    proxy.push(status, {}, error("nope", status))
    make(env, clock).run(once=True)
    assert (job(env, clock, j.id).status, job(env, clock, j.id).last_reason) == ("failed", reason)


def test_401_with_changed_registry_is_repaired_and_continues(env, clock, proxy):
    j = add(env, clock)
    make(env, clock).run(once=True)  # creates the app
    j2 = add(env, clock, "second")
    runner = make(env, clock)
    runner.tokens["queue"] = envget(env, "MUSE_QUEUE_APP_TOKEN")
    apps.rotate_token("queue", env)  # someone ran `muse apps rotate queue`
    proxy.push(401, {}, error("bad key", 401))
    assert runner.run(once=True).stopped in ("once", "idle")
    assert job(env, clock, j2.id).status in ("queued", "done")
    runner.run(once=True)
    assert job(env, clock, j2.id).status == "done"
    assert apps.find_by_token(proxy.requests[-1]["auth"].split()[1], env).name == "queue"


def test_401_that_persists_stops_the_runner_and_keeps_the_job(env, clock, proxy):
    j = add(env, clock)
    proxy.push(401, {}, error("bad key", 401))
    summary = make(env, clock).run()
    assert summary.stopped == "auth"
    d = job(env, clock, j.id)
    assert d.status == "queued" and d.last_reason == "auth" and d.attempts == 0


def test_unreachable_proxy_waits_and_costs_no_attempt(env, clock):
    j = add(env, clock)
    runner = make(env, clock, post=lambda *a: queue.Reply(0, {}, b""))
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "deferred" and d.attempts == 0 and d.last_reason == "network"
    assert 15 <= d.next_attempt_at - clock.t <= 30.01


def test_unknown_defer_and_refuse_codes_are_handled(env, clock, proxy):
    a = add(env, clock, "a")
    proxy.push(429, {"x-muse-ration": "defer:something-new"})
    make(env, clock).run(once=True)
    assert 60 <= job(env, clock, a.id).next_attempt_at - clock.t <= 72.01
    b = add(env, clock, "b")
    proxy.push(429, {"x-muse-ration": "refuse:something-new"})
    clock.t += 1
    make(env, clock).run(once=True)
    assert job(env, clock, b.id).next_attempt_at - clock.t >= 3600


# ---------------------------------------------------------------- deadline, cancel, pause


def test_deadline_fails_a_waiting_job_at_the_deadline(env, clock, proxy):
    j = add(env, clock, deadline=clock.t + 100)
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "3000"})
    summary = make(env, clock).run(until_idle=True)
    d = job(env, clock, j.id)
    assert d.status == "failed" and d.last_reason == "deadline" and d.payload == ""
    assert len(proxy.requests) == 1
    assert clock.t <= j.created_at + 100 + queue.SLICE_S + 1  # woke for the deadline, not for the due time
    assert summary.stopped == "idle"


def test_job_past_deadline_is_never_sent(env, clock, proxy):
    j = add(env, clock, deadline=clock.t + 5)
    clock.t += 10
    make(env, clock).run(once=True)
    assert proxy.requests == [] and job(env, clock, j.id).last_reason == "deadline"


def test_cancel_a_waiting_job(env, clock, proxy):
    j = add(env, clock, CANARY_PROMPT)
    queue.Queue(env, clock.now).cancel(j.id)
    make(env, clock).run(once=True)
    assert proxy.requests == [] and job(env, clock, j.id).status == "cancelled"
    assert job(env, clock, j.id).payload == ""
    with pytest.raises(queue.QueueError):
        queue.Queue(env, clock.now).cancel(j.id)


def test_cancel_while_running_discards_the_result(env, clock, proxy, tmp_path):
    out = tmp_path / "o.txt"
    j = add(env, clock, out_path=str(out))
    proxy.on_request = lambda n: queue.Queue(env, clock.now).cancel(j.id)
    runner = make(env, clock)
    summary = runner.run(once=True)
    assert job(env, clock, j.id).status == "cancelled" and not out.exists() and summary.done == 0


def test_pause_stops_sending_and_resume_continues(env, clock, proxy):
    add(env, clock)
    queue.set_paused(env)
    assert stat.S_IMODE(queue.paths.paused_path(env).stat().st_mode) == 0o600
    assert make(env, clock).run(once=True).stopped == "paused" and proxy.requests == []
    clock.on_sleep = lambda n: queue.clear_paused(env) if n == 3 else None
    assert make(env, clock).run(until_idle=False, max_jobs=1).done == 1
    assert len(clock.sleeps) >= 3 and all(s == queue.SLICE_S for s in clock.sleeps[:3])


def test_pause_is_not_busy_polling_when_empty(env, clock):
    class Stop(Exception):
        pass

    clock.on_sleep = lambda n: (_ for _ in ()).throw(KeyboardInterrupt()) if n == 4 else None
    summary = make(env, clock).run()
    assert summary.stopped == "interrupted" and clock.sleeps == [queue.SLICE_S] * 4


def test_max_jobs_and_once(env, clock, proxy):
    for i in range(5):
        add(env, clock, f"p{i}")
    assert make(env, clock).run(max_jobs=2).stopped == "max-jobs" and len(proxy.requests) == 2
    assert make(env, clock).run(once=True).done == 3


# ---------------------------------------------------------------- single runner, crash recovery


def test_second_runner_is_refused_while_the_first_holds_the_lock(env, clock, proxy):
    add(env, clock)
    with queue.runner_lock(env) as got:
        assert got
        summary = make(env, clock).run(once=True)
    assert summary.stopped == "locked" and proxy.requests == []
    assert make(env, clock).run(once=True).done == 1  # released


def test_concurrent_claims_give_a_job_to_exactly_one(env, clock, proxy):
    add(env, clock)
    winners = []

    def grab(name):
        got = queue.Queue(env, clock.now).claim_next(clock.t, name)
        if got:
            winners.append(name)

    threads = [threading.Thread(target=grab, args=(f"r{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1


def test_job_left_running_by_a_dead_runner_is_requeued_at_startup(env, clock, proxy):
    j = add(env, clock)
    assert queue.Queue(env, clock.now).claim_next(clock.t, "dead-runner")
    assert job(env, clock, j.id).status == "running"
    make(env, clock).run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "done" and d.attempts == 1  # the lost run counted


def test_stale_timeout_requeues_only_old_heartbeats(env, clock):
    q = queue.Queue(env, clock.now)
    fresh, stale = add(env, clock, "f"), add(env, clock, "s")
    q.claim_next(clock.t, "r1")
    clock.t += 700
    q.claim_next(clock.t, "r2")  # claims the second job later
    assert q.requeue_stale(clock.t, timeout=600) == 1
    assert job(env, clock, fresh.id).status == "queued" and job(env, clock, stale.id).status == "running"


def test_poison_job_that_keeps_crashing_eventually_fails(env, clock):
    q = queue.Queue(env, clock.now)
    j = add(env, clock, max_attempts=2)
    for _ in range(2):
        q.claim_next(clock.t, "dead")
        q.requeue_stale(clock.t, timeout=0)
    d = job(env, clock, j.id)
    assert d.status == "failed" and d.last_reason == "max-attempts:runner-lost"


# ---------------------------------------------------------------- outputs


def test_atomic_write_leaves_the_old_file_and_no_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "out.txt"
    target.write_text("old")
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        queue.atomic_write(target, "new")
    assert target.read_text() == "old" and [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_unwritable_output_is_a_retry_not_a_lost_result(env, clock, proxy, tmp_path):
    out = tmp_path / "gone" / "o.txt"
    (tmp_path / "gone").mkdir()
    j = add(env, clock, out_path=str(out))
    (tmp_path / "gone").rmdir()
    out_parent_file = tmp_path / "gone"
    out_parent_file.write_text("a file, not a folder")
    make(env, clock).run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "deferred" and d.last_reason == "output-unwritable" and d.payload != ""


def test_add_refuses_overwrite_and_duplicate_targets(env, clock, tmp_path):
    existing = tmp_path / "e.txt"
    existing.write_text("x")
    with pytest.raises(queue.QueueError):
        add(env, clock, out_path=str(existing))
    add(env, clock, out_path=str(existing), force=True)
    with pytest.raises(queue.QueueError, match="already writes"):
        add(env, clock, out_path=str(existing), force=True)


# ---------------------------------------------------------------- kinds


def test_file_summary_reads_the_file_at_run_time_and_fails_when_gone(env, clock, proxy, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("alpha beta")
    j1 = add(env, clock, str(f), kind="file-summary", instruction="summarise")
    j2 = add(env, clock, str(tmp_path / "missing.txt"), kind="file-summary", instruction="summarise")
    make(env, clock).run(once=True)
    assert proxy.requests[0]["prompt"] == "summarise\n\n---\nalpha beta"
    assert job(env, clock, j1.id).status == "done"
    assert job(env, clock, j2.id).last_reason == "file-unreadable"


def test_read_text_file_limits(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (queue.MAX_FILE_BYTES + 1))
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\x00\x01")
    for path in (big, binary, tmp_path):
        with pytest.raises(queue.QueueError):
            queue.read_text_file(path)


def test_expand_directory_caps_skips_hidden_and_binary(tmp_path):
    for i in range(4):
        (tmp_path / f"f{i}.txt").write_text("t")
    (tmp_path / ".hidden").write_text("t")
    (tmp_path / "bin.dat").write_bytes(b"\x00")
    files, skipped = queue.expand_directory(tmp_path, False, 10)
    assert [f.name for f in files] == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"] and len(skipped) == 1
    with pytest.raises(queue.QueueError, match="limit is 3"):
        queue.expand_directory(tmp_path, False, 3)
    with pytest.raises(queue.QueueError):
        queue.expand_directory(tmp_path, False, 500)


def test_batch_resumes_after_a_deferral_and_joins_the_outputs(env, clock, proxy, tmp_path):
    out = tmp_path / "b.txt"
    j = add(env, clock, json.dumps(["one", "two", "three"]), kind="batch", out_path=str(out))
    proxy.push(200, {"x-muse-provider": "freea", "x-muse-cost-usd": "0.001"}, completion("R1"))
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "30"})
    runner = make(env, clock)
    runner.run(once=True)
    d = job(env, clock, j.id)
    assert d.status == "deferred" and d.progress == 1 and d.partial == json.dumps(["R1"])
    proxy.push(200, {"x-muse-cost-usd": "0.001"}, completion("R2"))
    proxy.push(200, {"x-muse-cost-usd": "0.001"}, completion("R3"))
    clock.t = d.next_attempt_at
    runner.run(once=True)
    assert [r["prompt"] for r in proxy.requests] == ["one", "two", "two", "three"]
    d = job(env, clock, j.id)
    assert d.status == "done" and d.cost_usd == pytest.approx(0.003) and d.tokens_out == 21 and d.partial is None
    assert out.read_text() == "R1\n\n---\n\nR2\n\n---\n\nR3\n"


def test_split_batch():
    assert queue.split_batch("a\n---\nb\n\n---\n  \n---\nc") == ["a", "b", "c"]
    with pytest.raises(queue.QueueError):
        queue.split_batch("  \n---\n")
    with pytest.raises(queue.QueueError):
        queue.split_batch("\n---\n".join("x" * 3 for _ in range(21)))


@pytest.mark.parametrize("text,ok", [("90m", True), ("2h", True), ("1d", True), ("08:30", True), ("2999-01-01", True),
                                       ("2999-01-01T08:00", True), ("soon", False), ("25:00", False), ("0m", False), ("2001-01-01", False)])
def test_parse_deadline(text, ok):
    now = 1_800_000_000.0
    if ok:
        assert queue.parse_deadline(text, now, resolve_tz("UTC")) > now
    else:
        with pytest.raises(queue.QueueError):
            queue.parse_deadline(text, now, resolve_tz("UTC"))


def test_parse_deadline_clock_time_is_the_next_occurrence():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc).timestamp()
    tz = resolve_tz("UTC")
    assert queue.parse_deadline("10:00", now, tz) - now == 3600
    assert queue.parse_deadline("08:00", now, tz) - now == 23 * 3600


# ---------------------------------------------------------------- validation, stats, no content leaks


def test_add_validation(env, clock):
    q = queue.Queue(env, clock.now)
    with pytest.raises(queue.QueueError):
        q.add(kind="nope", payload="x")
    with pytest.raises(queue.QueueError):
        q.add(kind="prompt", payload="x", privacy="secret")
    with pytest.raises(queue.QueueError):
        q.add(kind="prompt", payload="x", alias="bad alias!")
    with pytest.raises(queue.QueueError):
        q.add(kind="prompt", payload="x", deadline=clock.t - 1)
    with pytest.raises(queue.QueueError):
        q.find("zz")


def test_stats_counts_and_never_creates_the_database(env, clock, proxy):
    assert queue.stats(env)["waiting"] == 0 and not queue.paths.queue_path(env).exists()
    add(env, clock, "a")
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "100"})
    add(env, clock, "b", deadline=clock.t + 1000)
    make(env, clock).run(once=True)
    s = queue.stats(env, clock.t)
    assert s["counts"]["deferred"] == 1 and s["counts"]["done"] == 1 and s["waiting"] == 1
    assert s["next_due_at"] and s["oldest_queued_at"] and s["hold_until"] is None


def test_nothing_with_content_leaves_the_queue_files_and_logs(env, clock, proxy, tmp_path, caplog):
    out = tmp_path / "keep" / "r.txt"
    out.parent.mkdir()
    proxy.push(429, {"x-muse-ration": "defer:yield", "retry-after": "5"}, error(f"[muse] {CANARY_PROMPT}"))
    proxy.push(500, {}, error(f"upstream said {CANARY_REPLY}", 500))
    j = add(env, clock, CANARY_PROMPT, out_path=str(out))
    runner = make(env, clock)
    for _ in range(3):
        runner.run(once=True)
        clock.t += 10_000
    d = job(env, clock, j.id)
    assert d.status == "done"
    blob = json.dumps(d.public())
    assert CANARY_PROMPT not in blob and CANARY_REPLY not in blob
    assert not any(CANARY_PROMPT in line or CANARY_REPLY in line for line in runner.logs)
    assert CANARY_PROMPT not in caplog.text and CANARY_REPLY not in caplog.text
    home = queue.paths.home(env)
    for path in home.rglob("*"):
        if path.is_file() and path != out:
            data = path.read_bytes()
            assert CANARY_PROMPT.encode() not in data and CANARY_REPLY.encode() not in data, path
    assert not (home / "ledger.sqlite").exists()  # the queue never touches the usage ledger
    assert envget(env, "MUSE_QUEUE_APP_TOKEN").encode() not in b"".join(p.read_bytes() for p in home.rglob("*") if p.is_file() and p.name != ".env" and p.name != "apps.yaml.lock")


def test_queue_db_is_rollback_journal_with_busy_timeout(env, clock):
    add(env, clock)
    conn = sqlite3.connect(queue.paths.queue_path(env))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()
