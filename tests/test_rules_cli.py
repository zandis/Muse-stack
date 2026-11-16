"""`muse rules`, `muse ask --task`, `muse queue add --task` and the queue's routing task."""

from __future__ import annotations

import io
import json
import random
import sqlite3
import sys
from pathlib import Path

import pytest

from wardmuse import cli, keys, paths, queue
from queue_fakes import Clock, FakeProxy


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "NO_COLOR", "MUSE_CATALOG"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setitem(sys.modules, "keyring", None)


@pytest.fixture()
def proxy(monkeypatch):
    p = FakeProxy()
    monkeypatch.setenv("MUSE_PROXY_URL", p.url)
    yield p
    p.stop()


def run(argv, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_rules_show_in_both_languages_and_json(monkeypatch):
    code, out, _ = run(["rules"], monkeypatch)
    assert code == 0
    assert out.splitlines()[0].startswith("Task 'light'") and "Rules file: " in out and "routing.yaml" in out
    code, out, _ = run(["rules", "--lang", "zh"], monkeypatch)
    assert code == 0 and out.startswith("「light」類工作") and "規則檔：" in out
    code, out, _ = run(["rules", "--json"], monkeypatch)
    data = json.loads(out)
    assert code == 0 and data["tasks"]["light"]["alias"] == "fast" and data["path"].endswith("routing.yaml") and data["problems"] == []


def test_rules_check(monkeypatch, tmp_path):
    code, out, err = run(["rules", "check"], monkeypatch)
    assert code == 0 and out.strip().endswith("ok (6 tasks)") and err == ""
    bad = tmp_path / "routing.yaml"
    bad.write_text("tasks:\n  light: {alias: fast, max_output_tokens: lots}\n")
    monkeypatch.setenv("MUSE_ROUTING", str(bad))
    code, out, err = run(["rules", "check"], monkeypatch)
    assert code == 1 and "tasks.light.max_output_tokens" in err and "built-in rules" in err
    code, out, _ = run(["rules"], monkeypatch)
    assert code == 1 and "could not be used" in out


def test_rules_check_warns_about_aliases_missing_from_the_catalogue(monkeypatch, tmp_path):
    catalogue = tmp_path / "providers.yaml"
    catalogue.write_text("aliases: {fast: {mode: chat}, cheap: {mode: chat}}\nproviders: []\n")
    monkeypatch.setenv("MUSE_CATALOG", str(catalogue))
    code, _, err = run(["rules", "check"], monkeypatch)
    assert code == 0 and "alias 'smart' is not in the provider catalogue" in err and "'long-context'" in err


def test_ask_with_a_task(monkeypatch, proxy):
    code, out, _ = run(["ask", "hi", "--task", "light"], monkeypatch)
    assert code == 0 and proxy.requests[0]["task"] == "light" and proxy.requests[0]["model"] == "auto"
    run(["ask", "hi", "--task", "heavy", "--alias", "smart"], monkeypatch)
    assert proxy.requests[1]["task"] == "heavy" and proxy.requests[1]["model"] == "smart"
    run(["ask", "hi"], monkeypatch)
    assert proxy.requests[2]["task"] is None and proxy.requests[2]["model"] == "fast"


def jobs(monkeypatch) -> list[dict]:
    return json.loads(run(["queue", "list", "--json"], monkeypatch)[1])


def test_queue_add_task_and_defaults(monkeypatch):
    code, out, _ = run(["queue", "add", "x", "--task", "heavy"], monkeypatch)
    assert code == 0 and "task heavy" in out
    run(["queue", "add", "y"], monkeypatch)  # the rules file: queue -> light
    run(["queue", "add", "z", "--alias", "cheap"], monkeypatch)  # an alias alone: no task
    run(["queue", "add", "w", "--allow-paid"], monkeypatch)  # queue-paid has no override: the default task
    got = {(j["task"], j["alias"]) for j in jobs(monkeypatch)}
    assert got == {("heavy", "auto"), ("light", "auto"), (None, "cheap"), ("everyday", "auto")}
    assert run(["queue", "add", "v", "--task", "Bad Task!"], monkeypatch)[0] == 1


def test_queue_runner_sends_the_task(monkeypatch, proxy):
    monkeypatch.setenv("MUSE_PROXY_URL", proxy.url)
    env = dict(keys.export_env())
    clock = Clock()
    queue.Queue(env, clock.now).add(kind="prompt", payload="hello", alias="auto", task="light")
    runner = queue.Runner(env, clock=clock.now, sleep=clock.sleep, rng=random.Random(1), log=lambda line: None)
    assert runner.run(once=True).done == 1
    assert proxy.requests[0]["task"] == "light" and proxy.requests[0]["model"] == "auto"


def test_old_queue_file_gains_the_task_column(monkeypatch):
    env = dict(keys.export_env())
    path = paths.queue_path(env)
    conn = sqlite3.connect(path)
    old = queue.SCHEMA.split("  task TEXT")[0].rstrip().rstrip(",") + "\n);\n"
    old = old.replace("partial TEXT,", "partial TEXT")
    conn.executescript(old)
    conn.close()
    assert "task" not in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(jobs)")}
    job = queue.Queue(env).add(kind="prompt", payload="p", alias="cheap")
    assert job.task is None and queue.Queue(env).get(job.id).public()["task"] is None
