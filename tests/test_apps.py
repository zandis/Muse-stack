"""muse.apps: registry file safety, token hashing, constant-time lookup, reload, validation."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import hashlib
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest
import yaml

from wardmuse import apps
from wardmuse.apps import ANONYMOUS, OWNER, AppError


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {"MUSE_HOME": str(tmp_path)}


def path(env) -> Path:
    return Path(envget(env, "MUSE_HOME")) / "apps.yaml"


def test_absent_file_is_no_apps(env):
    assert apps.load_apps(env) == {}
    assert apps.find_by_token("sk-muse-app-x", env) is None


def test_add_returns_token_once_and_stores_only_hash(env):
    app, token = apps.add_app("notes-batch", "background", daily_requests=50, note="nightly", environ=env, now=0)
    assert token.startswith("sk-muse-app-") and len(token) > 40
    text = path(env).read_text()
    assert token not in text
    assert app.token_sha256 == hashlib.sha256(token.encode()).hexdigest() and app.token_sha256 in text
    assert stat.S_IMODE(path(env).stat().st_mode) == 0o600
    assert app.created_at == "1970-01-01T00:00:00Z"
    assert app.allow_paid is False  # background default
    loaded = apps.load_apps(env)["notes-batch"]
    assert loaded == app and loaded.daily_requests == 50 and loaded.note == "nightly"
    assert apps.find_by_token(token, env) == app


def test_defaults_per_priority(env):
    assert apps.add_app("a", "interactive", environ=env)[0].allow_paid is True
    assert apps.add_app("b", "normal", environ=env)[0].allow_paid is True
    assert apps.add_app("c", "background", allow_paid=True, environ=env)[0].allow_paid is True


def test_tokens_are_unique_and_lookup_is_exact(env):
    _, t1 = apps.add_app("one", "normal", environ=env)
    _, t2 = apps.add_app("two", "normal", environ=env)
    assert t1 != t2
    assert apps.find_by_token(t1, env).name == "one" and apps.find_by_token(t2, env).name == "two"
    for bad in (t1[:-1], t1 + "x", "", None, "Bearer " + t1, t1.upper()):
        assert apps.find_by_token(bad, env) is None


def test_lookup_uses_compare_digest_on_every_entry(env, monkeypatch):
    for name in ("a", "b", "c"):
        apps.add_app(name, "normal", environ=env)
    _, token = apps.add_app("d", "normal", environ=env)
    seen = []
    real = apps.hmac.compare_digest

    def spy(x, y):
        seen.append(y)
        return real(x, y)

    monkeypatch.setattr(apps.hmac, "compare_digest", spy)
    apps.find_by_token(token, env)
    assert len(seen) == 4  # no early exit on a match
    seen.clear()
    apps.find_by_token("sk-muse-app-nope", env)
    assert len(seen) == 4


def test_remove_and_rotate(env):
    _, old = apps.add_app("bot", "interactive", environ=env)
    app, new = apps.rotate_token("bot", env)
    assert new != old and apps.find_by_token(old, env) is None and apps.find_by_token(new, env) == app
    assert apps.remove_app("bot", env) is True
    assert apps.find_by_token(new, env) is None
    assert apps.remove_app("bot", env) is False
    with pytest.raises(AppError):
        apps.rotate_token("bot", env)


def test_update(env):
    apps.add_app("job", "background", environ=env)
    app = apps.update_app("job", env, priority="normal", daily_tokens=1000)
    assert app.priority == "normal" and app.daily_tokens == 1000 and app.allow_paid is False  # stored value kept
    app = apps.update_app("job", env, allow_paid=None)
    assert app.allow_paid is True  # back to normal's default
    app = apps.update_app("job", env, daily_tokens=None)
    assert app.daily_tokens is None
    with pytest.raises(AppError):
        apps.update_app("job", env, token_sha256="0" * 64)
    with pytest.raises(AppError):
        apps.update_app("nope", env, priority="normal")


@pytest.mark.parametrize("kwargs, message", [
    ({"name": "Bad Name", "priority": "normal"}, "app name"),
    ({"name": "-x", "priority": "normal"}, "app name"),
    ({"name": "a" * 41, "priority": "normal"}, "app name"),
    ({"name": "owner", "priority": "normal"}, "reserved"),
    ({"name": "anonymous", "priority": "normal"}, "reserved"),
    ({"name": "x", "priority": "urgent"}, "priority"),
    ({"name": "x", "priority": "normal", "daily_requests": -1}, "negative"),
    ({"name": "x", "priority": "normal", "daily_tokens": "lots"}, "whole number"),
    ({"name": "x", "priority": "normal", "daily_requests": True}, "whole number"),
    ({"name": "x", "priority": "normal", "allow_paid": "maybe"}, "true or false"),
    ({"name": "x", "priority": "normal", "default_privacy": "secret"}, "default_privacy"),
    ({"name": "x", "priority": "normal", "note": "two\nlines"}, "note"),
])
def test_validation(env, kwargs, message):
    name = kwargs.pop("name")
    priority = kwargs.pop("priority")
    with pytest.raises(AppError, match=message):
        apps.add_app(name, priority, environ=env, **kwargs)
    assert not path(env).exists()


def test_duplicate_name(env):
    apps.add_app("x", "normal", environ=env)
    with pytest.raises(AppError, match="already exists"):
        apps.add_app("x", "background", environ=env)


@pytest.mark.parametrize("content", ["{not yaml", "- just\n- a list\n", "apps: nope\n", "\x00\x01", "apps: [1, 2]\n"])
def test_malformed_file_is_no_apps_and_is_never_overwritten(env, content):
    path(env).write_text(content)
    assert apps.load_apps(env) == {}
    if content.startswith(("{", "-", "apps: nope", "\x00")):
        with pytest.raises(AppError, match="malformed"):
            apps.add_app("x", "normal", environ=env)
        assert path(env).read_text() == content


def test_bad_entries_are_skipped_individually(env):
    good = apps.add_app("good", "normal", environ=env)[0]
    data = yaml.safe_load(path(env).read_text())
    data["apps"] += [
        {"name": "bad", "priority": "urgent", "token_sha256": "a" * 64},
        {"name": "nohash", "priority": "normal"},
        {"name": "good", "priority": "normal", "token_sha256": "b" * 64},  # duplicate
        {"name": "owner", "priority": "normal", "token_sha256": "c" * 64},  # reserved
        "garbage",
    ]
    path(env).write_text(yaml.safe_dump(data))
    assert apps.load_apps(env) == {"good": good}


def test_empty_file(env):
    path(env).write_text("")
    assert apps.load_apps(env) == {}
    apps.add_app("x", "normal", environ=env)
    assert set(apps.load_apps(env)) == {"x"}


def test_reload_on_change_without_restart(env):
    assert apps.load_apps(env) == {}
    _, token = apps.add_app("x", "normal", environ=env)
    assert apps.find_by_token(token, env) is not None
    # Another process edits the file: the cache must notice.
    data = yaml.safe_load(path(env).read_text())
    data["apps"][0]["priority"] = "background"
    time.sleep(0.01)
    path(env).write_text(yaml.safe_dump(data))
    assert apps.load_apps(env)["x"].priority == "background"
    path(env).unlink()
    assert apps.load_apps(env) == {}


def test_cache_returns_copies(env):
    apps.add_app("x", "normal", environ=env)
    first = apps.load_apps(env)
    first.clear()
    assert set(apps.load_apps(env)) == {"x"}


def test_atomic_write_leaves_no_temp_files(env):
    for i in range(5):
        apps.add_app(f"a{i}", "normal", environ=env)
    leftovers = [p.name for p in Path(envget(env, "MUSE_HOME")).iterdir() if p.name.startswith(".apps-")]
    assert leftovers == []


def _adder(home: str, prefix: str, n: int) -> None:
    for i in range(n):
        apps.add_app(f"{prefix}{i}", "normal", environ={"MUSE_HOME": home})


def test_concurrent_writers_lose_nothing(env):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_adder, args=(envget(env, "MUSE_HOME"), f"p{w}-", 8)) for w in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert len(apps.load_apps(env)) == 24


def test_resolve_and_builtins(env):
    assert apps.resolve("owner", env) is OWNER and OWNER.priority == "interactive" and OWNER.allow_paid
    assert apps.resolve("anonymous", env) is ANONYMOUS and ANONYMOUS.priority == "normal" and not ANONYMOUS.allow_paid
    assert apps.resolve("missing", env) is None and apps.resolve(None, env) is None
    app, _ = apps.add_app("x", "background", default_privacy="private", environ=env)
    assert apps.resolve("x", env) == app


def test_policy_round_trip(env):
    app, _ = apps.add_app("x", "background", daily_requests=3, daily_tokens=9, default_privacy="local", environ=env)
    back = apps.from_policy(app.policy())
    assert (back.name, back.priority, back.daily_requests, back.daily_tokens, back.allow_paid, back.default_privacy) == (
        "x", "background", 3, 9, False, "local")
    assert "token_sha256" not in str(app.policy())
    assert apps.from_policy(None) is None
    assert apps.from_policy({"muse_app": "x", "muse_priority": "urgent"}) is None
    odd = apps.from_policy({"muse_app": "x", "muse_priority": "normal", "muse_allow_paid": "yes", "muse_daily_requests": -5})
    assert odd.allow_paid is False and odd.daily_requests is None  # only a real True/int counts


def test_explicit_registry_path(tmp_path):
    custom = tmp_path / "elsewhere.yaml"
    env = {"MUSE_APPS_FILE": str(custom), "MUSE_HOME": str(tmp_path / "home")}
    apps.add_app("x", "normal", environ=env)
    assert custom.exists() and not (tmp_path / "home" / "apps.yaml").exists()
    os.chmod(custom, 0o644)
    apps.add_app("y", "normal", environ=env)
    assert stat.S_IMODE(custom.stat().st_mode) == 0o600
