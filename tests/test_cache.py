"""muse.cache: what may be cached, the key, and where an entry may live (privacy)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wardmuse import cache
from wardmuse.cache import CacheSettings, ExactCache

MSG = [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize("data, deterministic, ok", [
    ({"messages": MSG, "temperature": 0}, False, True),
    ({"messages": MSG, "temperature": 0.0}, False, True),
    ({"messages": MSG}, True, True),  # unset on a task declared deterministic
    ({"messages": MSG}, False, False),
    ({"messages": MSG, "temperature": 0.2}, True, False),  # an explicit temperature wins
    ({"messages": MSG, "temperature": "x"}, False, False),
    ({"messages": MSG, "temperature": 0, "stream": True}, False, False),
    ({"messages": MSG, "temperature": 0, "tools": [{"type": "function"}]}, False, False),
    ({"messages": MSG, "temperature": 0, "n": 2}, False, False),
    ({"messages": MSG, "temperature": 0, "n": 1}, False, True),
    ({"messages": MSG, "temperature": 0, "logprobs": True}, False, False),
    ({"messages": MSG, "temperature": 0, "stream": False, "tools": []}, False, True),
    ({"input": "x", "temperature": 0}, False, False),  # not chat
    ({"messages": [], "temperature": 0}, False, False),
])
def test_cacheable(data, deterministic, ok):
    assert cache.cacheable(data, deterministic=deterministic)[0] is ok


BASE = {"messages": MSG, "temperature": 0}


@pytest.mark.parametrize("change", [
    {"alias": "smart"},
    {"data": {"messages": [{"role": "user", "content": "hello!"}], "temperature": 0}},
    {"data": {**BASE, "max_tokens": 10}},
    {"data": {**BASE, "seed": 1}},
    {"data": {**BASE, "stop": ["\n"]}},
    {"data": {**BASE, "response_format": {"type": "json_object"}}},
    {"privacy": "public"},
    {"app_privacy": "private"},
])
def test_key_changes_with_everything_that_decides_the_answer_or_who_sees_it(change):
    base = cache.cache_key("fast", BASE, "standard", "standard")
    args = {"alias": "fast", "data": BASE, "privacy": "standard", "app_privacy": "standard", **change}
    assert cache.cache_key(args["alias"], args["data"], args["privacy"], args["app_privacy"]) != base


def test_key_ignores_what_does_not_change_the_answer():
    base = cache.cache_key("fast", BASE, "standard", "standard")
    noisy = {**BASE, "metadata": {"x": 1}, "user": "u", "litellm_call_id": "abc", "request_timeout": 5}
    assert cache.cache_key("fast", noisy, "standard", "standard") == base
    assert len(base) == 64 and "hello" not in base


@pytest.mark.parametrize("privacy, disk", [("public", True), ("standard", True), ("private", False), ("local", False)])
def test_on_disk_allowed(privacy, disk):
    assert cache.on_disk_allowed(privacy) is disk


def test_private_and_local_never_touch_disk(tmp_path):
    store = ExactCache(CacheSettings(True, 60, 10), {"MUSE_HOME": str(tmp_path)})
    for cls in ("private", "local"):
        assert store.put(f"k-{cls}", cls, "secret answer", now=0) == "memory"
        assert store.get(f"k-{cls}", cls, now=1) == "secret answer"
    assert not (tmp_path / "cache.sqlite").exists()
    assert store.put("k-std", "standard", "plain answer", now=0) == "disk"
    db = (tmp_path / "cache.sqlite")
    assert db.exists() and (db.stat().st_mode & 0o777) == 0o600
    assert b"secret answer" not in db.read_bytes()
    # A fresh process (another worker) sees the disk entry, not the memory ones.
    other = ExactCache(CacheSettings(True, 60, 10), {"MUSE_HOME": str(tmp_path)})
    assert other.get("k-std", "standard", now=1) == "plain answer"
    assert other.get("k-private", "private", now=1) is None


def test_class_is_checked_again_on_read(tmp_path):
    store = ExactCache(CacheSettings(True, 60, 10), {"MUSE_HOME": str(tmp_path)})
    store.put("k", "standard", "a", now=0)
    assert store.get("k", "public", now=1) is None
    store.put("m", "private", "b", now=0)
    assert store.get("m", "local", now=1) is None


def test_ttl_and_size_bound(tmp_path):
    store = ExactCache(CacheSettings(True, 10, 2), {"MUSE_HOME": str(tmp_path)})
    store.put("a", "standard", "1", now=0)
    assert store.get("a", "standard", now=9) == "1" and store.get("a", "standard", now=10) is None
    for i, key in enumerate("xyz"):
        store.put(key, "standard", key, now=100 + i)
        store.put(key, "private", key, now=100 + i)
    assert store.get("x", "standard", now=103) is None and store.get("z", "standard", now=103) == "z"
    assert store.get("x", "private", now=103) is None and store.get("y", "private", now=103) == "y"


def test_settings_from_env():
    assert CacheSettings.from_env({}) == CacheSettings(False, 3600.0, 1000)
    assert CacheSettings.from_env({"MUSE_CACHE": "exact", "MUSE_CACHE_TTL_S": "60", "MUSE_CACHE_MAX_ENTRIES": "5"}) == CacheSettings(True, 60.0, 5)
    assert CacheSettings.from_env({"MUSE_CACHE": "on", "MUSE_CACHE_TTL_S": "-1"}) == CacheSettings(False, 3600.0, 1000)


def choice(content="hi", finish="stop", tool_calls=None):
    return SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, tool_calls=tool_calls, function_call=None))


@pytest.mark.parametrize("response, text", [
    (SimpleNamespace(choices=[choice()]), "hi"),
    (SimpleNamespace(choices=[choice(finish="length")]), None),  # cut short: not cached
    (SimpleNamespace(choices=[choice(tool_calls=[{}])]), None),
    (SimpleNamespace(choices=[choice(content=None)]), None),
    (SimpleNamespace(choices=[choice(), choice()]), None),
    (SimpleNamespace(), None),
])
def test_answer_text(response, text):
    assert cache.answer_text(response) == text


def test_mock_response_is_a_dict_so_no_text_is_read_as_a_mock_error():
    mock = cache.mock_response("litellm.RateLimitError", "openai/x")
    assert isinstance(mock, dict) and mock["choices"][0]["message"]["content"] == "litellm.RateLimitError"
    assert mock["usage"]["total_tokens"] == 0
