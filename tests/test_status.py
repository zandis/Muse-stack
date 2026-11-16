"""muse.status.build_snapshot against the real catalogue and a small one."""

from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from muse import status
from muse.ledger import Ledger
from muse.quota import ObservedLimit
from muse.status import build_snapshot


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-11-12T07:00:30")


def env_for(home: Path, **extra: str) -> dict[str, str]:
    return {"MUSE_HOME": str(home), **extra}


def test_empty_everything(_isolated_muse_home: Path):
    snap = build_snapshot(now=NOW, environ=env_for(_isolated_muse_home))
    json.dumps(snap)  # JSON-serialisable
    assert snap["generated_at"] == "2026-11-12T07:00:30Z"
    assert snap["default_privacy"] == "standard"
    assert snap["proxy"] == {"url": "http://127.0.0.1:4000", "reachable": None}
    assert all(p["state"] == "no_key" and not p["enabled"] for p in snap["providers"])
    assert all(p["usage"]["day"] == {"requests": 0, "tokens": 0} for p in snap["providers"])
    assert all(a["total"] == 0 and a["next"] is None for a in snap["aliases"])
    assert snap["recent"] == [] and snap["totals"] == {"requests_today": 0, "tokens_today": 0}
    local = next(p for p in snap["providers"] if p["id"] == "ollama")
    assert local["kind"] == "local" and local["key_env"] is None
    gemini = next(p for p in snap["providers"] if p["id"] == "gemini")
    assert gemini["policy"]["trains_on_inputs"] == "yes" and gemini["key_env"] == "GEMINI_API_KEY"


def test_missing_local_catalogue_and_disabled(_isolated_muse_home: Path):
    env = env_for(_isolated_muse_home, GEMINI_API_KEY="x", GROQ_API_KEY="y", MUSE_DISABLE_PROVIDERS="groq",
                  OLLAMA_API_BASE="http://127.0.0.1:11434", MUSE_LOCAL_CATALOG=str(_isolated_muse_home / "nope.yaml"))
    snap = build_snapshot(now=NOW, environ=env)
    states = {p["id"]: p["state"] for p in snap["providers"]}
    assert states["gemini"] == "ok" and states["groq"] == "disabled" and states["ollama"] == "ok"
    ollama = next(p for p in snap["providers"] if p["id"] == "ollama")
    assert ollama["deployments"] == [] and ollama["configured"]
    groq = next(p for p in snap["providers"] if p["id"] == "groq")
    assert all(d["state"] == "disabled" and d["aliases"] == [] for d in groq["deployments"])
    text = json.dumps(snap)
    assert '"x"' not in text and '"y"' not in text  # no key values


def test_keys_module_failure_falls_back_to_environ(_isolated_muse_home: Path, monkeypatch: pytest.MonkeyPatch):
    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "muse.keys" or (name == "muse" and args and args[2] and "keys" in args[2]):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    snap = build_snapshot(now=NOW, environ=env_for(_isolated_muse_home, COHERE_API_KEY="c"))
    states = {p["id"]: p["state"] for p in snap["providers"]}
    assert states["cohere"] == "ok" and states["gemini"] == "no_key"


@pytest.fixture()
def small(tmp_path: Path, _isolated_muse_home: Path):
    catalog = {
        "aliases": {"fast": {"mode": "chat", "fallbacks": []}},
        "providers": [
            {"id": "gemini", "env": "GEMINI_API_KEY", "tier": "free",
             "policy": {"trains_on_inputs": True, "human_review": True, "region": "us", "commercial_use": "allowed"},
             "deployments": [
                 {"id": "g1", "model": "gemini/g1", "mode": "chat", "aliases": ["fast"], "order": 1, "rpm": 5},
                 {"id": "g2", "model": "gemini/g2", "mode": "chat", "aliases": ["fast"], "order": 1, "rpm": 50},
             ]},
            {"id": "groq", "env": "GROQ_API_KEY", "tier": "free",
             "quota": {"requests_per_day": 100, "tokens_per_minute": 1000},
             "policy": {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"},
             "deployments": [{"id": "q1", "model": "groq/q1", "mode": "chat", "aliases": ["fast"], "order": 2, "rpm": 5}]},
        ],
    }
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(catalog))
    env = env_for(_isolated_muse_home, MUSE_CATALOG=str(path), GEMINI_API_KEY="g", GROQ_API_KEY="q")
    return env, Ledger(_isolated_muse_home / "ledger.sqlite", clock=lambda: NOW)


def test_blocks_states_and_next(small):
    env, db = small
    snap = build_snapshot(now=NOW, environ=env)
    fast = snap["aliases"][0]
    assert (fast["available"], fast["total"], fast["next"]) == (3, 3, "g2")  # order 1, higher rpm weight

    db.set_block("gemini:gemini/g2", NOW + 30, "cooldown", "rpm")
    snap = build_snapshot(now=NOW, environ=env)
    gem = snap["providers"][0]
    assert gem["state"] == "ok"  # g1 still open
    assert {d["id"]: d["state"] for d in gem["deployments"]} == {"g1": "ok", "g2": "cooling"}
    assert snap["aliases"][0]["next"] == "g1"

    db.set_block("gemini:gemini/g1", NOW + 3600, "exhausted", "daily quota (estimated)")
    snap = build_snapshot(now=NOW, environ=env)
    gem = snap["providers"][0]
    assert gem["state"] == "cooling" and gem["blocked_until"] == "2026-11-12T07:01:00Z"
    assert snap["aliases"][0] == {"name": "fast", "mode": "chat", "available": 1, "total": 3, "next": "q1"}

    db.set_block("provider:groq", NOW + 900, "exhausted", "bad key")
    snap = build_snapshot(now=NOW, environ=env)
    groq = snap["providers"][1]
    assert groq["state"] == "exhausted" and groq["block_reason"] == "bad key"
    assert groq["deployments"][0]["state"] == "exhausted"
    assert snap["aliases"][0]["next"] is None and snap["aliases"][0]["available"] == 0

    # Time passes: everything expires.
    later = build_snapshot(now=NOW + 4000, environ=env)
    assert all(p["state"] == "ok" for p in later["providers"])


def test_limits_header_then_estimate(small):
    env, db = small
    db.record_call(provider="groq", deployment_id="q1__fast", upstream="groq:groq/q1", ok=True, status=200,
                   tokens_in=100, tokens_out=50, alias="fast", privacy="standard", now=NOW - 5)
    snap = build_snapshot(now=NOW, environ=env)
    groq = snap["providers"][1]
    assert groq["limits"] == [
        {"window": "day", "metric": "requests", "limit": 100, "remaining": 99, "reset_at": "2026-11-13T00:00:00Z", "source": "estimate"},
        {"window": "minute", "metric": "tokens", "limit": 1000, "remaining": 850, "reset_at": "2026-11-12T07:01:00Z", "source": "estimate"},
    ]
    assert groq["usage"]["minute"] == {"requests": 1, "tokens": 150}
    dep = groq["deployments"][0]
    assert dep["last_status"] == 200 and dep["success_rate_24h"] == 1.0 and dep["last_used_at"] == "2026-11-12T07:00:25Z"
    assert snap["recent"][0]["deployment"] == "q1__fast" and snap["totals"]["requests_today"] == 1

    db.record_limits("groq:groq/q1", [ObservedLimit("day", "requests", 14400, 14000, NOW + 600)])
    groq = build_snapshot(now=NOW, environ=env)["providers"][1]
    assert groq["limits"] == [{"window": "day", "metric": "requests", "limit": 14400, "remaining": 14000,
                               "reset_at": "2026-11-12T07:10:30Z", "source": "header"}]
    # Stale header data (past its reset) falls back to the estimate.
    groq = build_snapshot(now=NOW + 700, environ=env)["providers"][1]
    assert {limit["source"] for limit in groq["limits"]} == {"estimate"}
    # No quota and no headers: empty.
    assert build_snapshot(now=NOW, environ=env)["providers"][0]["limits"] == []


def test_default_privacy_shapes_availability(small):
    env, _ = small
    snap = build_snapshot(now=NOW, environ={**env, "MUSE_DEFAULT_PRIVACY": "private"})
    assert snap["default_privacy"] == "private"
    assert snap["aliases"][0]["available"] == 0  # nothing in the small catalogue qualifies


def test_check_proxy_unreachable(_isolated_muse_home: Path):
    snap = build_snapshot(now=NOW, environ=env_for(_isolated_muse_home, MUSE_PROXY_URL="http://127.0.0.1:9"), check_proxy=True)
    assert snap["proxy"] == {"url": "http://127.0.0.1:9", "reachable": False}


def test_unreadable_ledger_does_not_break_snapshot(_isolated_muse_home: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(self, *a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(Ledger, "_connect", boom)
    snap = build_snapshot(now=NOW, environ=env_for(_isolated_muse_home, GEMINI_API_KEY="g"))
    assert snap["recent"] == [] and snap["totals"]["requests_today"] == 0
    assert next(p for p in snap["providers"] if p["id"] == "gemini")["state"] == "ok"


def test_core_modules_do_not_import_litellm(_isolated_muse_home: Path):
    import subprocess
    import sys

    code = (
        "import sys; import muse.status, muse.ledger, muse.quota; "
        "muse.status.build_snapshot(); print(sorted(m for m in sys.modules if m.split('.')[0] == 'litellm'))"
    )
    root = Path(status.__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
