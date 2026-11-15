"""scripts/export_link_hosts.py: the hosts the app may open come from the published catalogue."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("export_link_hosts_under_test", ROOT / "scripts" / "export_link_hosts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_file_is_current_and_holds_the_key_pages():
    mod = load()
    assert mod.main(["--check"]) == 0
    doc = json.loads(mod.OUT.read_text(encoding="utf-8"))
    hosts = doc["hosts"]
    assert hosts == sorted(set(hosts))
    for host in ("chatgpt.com", "claude.ai", "gemini.google.com", "openrouter.ai", "aistudio.google.com", "console.groq.com"):
        assert host in hosts
    assert all(mod.DNS_NAME.fullmatch(h) and "*" not in h for h in hosts)


@pytest.mark.parametrize("url, host", [
    ("https://openrouter.ai/auth", "openrouter.ai"),
    ("https://Console.Groq.com/keys", "console.groq.com"),
    ("http://openrouter.ai/keys", None),
    ("https://openrouter.ai:8443/", None),
    ("https://user@openrouter.ai/", None),
    ("https://127.0.0.1/", None),
    ("claude://claude.ai/new", None),
    ("https://*.example.com/", None),
    (None, None),
])
def test_only_plain_https_hosts(url, host):
    assert load().host_of(url) == host


def test_a_catalogue_with_a_bad_address_is_refused_and_app_schemes_are_ignored():
    mod = load()
    doc = {"ai_services": [{"connection": {"key_url": "https://a.example.com/k", "auth_url": "https://b.example.com/auth"}}],
           "other_allowances": [{"connection": {"key_url": "https://c.example.com"}}],
           "subscriptions": [{"open": {"web": {"url": "https://d.example.com/"}, "app": {"url": "d://open"}}}]}
    assert mod.hosts(doc) == ["a.example.com", "b.example.com", "c.example.com", "d.example.com"]
    doc["ai_services"][0]["connection"]["key_url"] = "http://a.example.com/k"
    with pytest.raises(ValueError):
        mod.hosts(doc)
