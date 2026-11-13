"""Shared helpers for the skills tests (no tests here): a scripted router and throw-away skills."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from muse import keys
from muse.skills import loader
from queue_fakes import FakeProxy, completion

OK_HEADERS = {"x-muse-provider": "freea", "x-muse-deployment": "a-model", "x-muse-cost-usd": "0.000000",
              "x-muse-privacy": "standard", "x-muse-app": "skills"}

FRONT: dict[str, Any] = {
    "name": "echo",
    "description": "Repeats what a model says. Used by the tests.",
    "muse": {
        "version": "0.1.0", "entry": "skill.py:run", "tier_default": "light", "privacy_default": "standard",
        "budget": {"requests": 5, "tokens": 20000, "seconds": 60},
        "needs": {"files.read": [], "files.write": [], "net": []},
        "writes": False, "check": "check",
    },
}

ECHO = """
def run(ctx):
    reply = ctx.llm.ask([{"role": "user", "content": ctx.inputs.get("q", "hello")}], task=ctx.inputs.get("step"))
    return {"text": reply.text, "task": reply.task}


def check(output, ctx):
    return [] if "GOOD" in output["text"] else ["the answer lacks the word the test looks for"]
"""


def skill_md(body: str = "What it does, for people.", **changes: Any) -> str:
    """SKILL.md text. `changes` replace top-level keys; keys starting `muse_` replace keys under muse: (None deletes)."""
    front = json.loads(json.dumps(FRONT))
    for key, value in changes.items():
        target, name = (front["muse"], key[5:]) if key.startswith("muse_") else (front, key)
        if value is None:
            target.pop(name, None)
        else:
            target[name] = value
    return "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n" + body + "\n"


def write_skill(home: Path, name: str = "echo", code: str = ECHO, approve: bool = True, **changes: Any) -> Path:
    folder = Path(home) / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(skill_md(name=name, **changes), encoding="utf-8")
    (folder / "skill.py").write_text(textwrap.dedent(code), encoding="utf-8")
    if approve:
        loader.approve(name, {"MUSE_HOME": str(home)})
    return folder


def chat(text: str, task: str = "", provider: str = "freea", cost: str = "0.000000", tin: int = 11, tout: int = 7,
         **headers: str) -> tuple[int, dict[str, str], bytes]:
    extra = {**OK_HEADERS, "x-muse-provider": provider, "x-muse-cost-usd": cost, **headers}
    if task:
        extra["x-muse-task"] = task
    return 200, extra, completion(text, tin, tout)


@pytest.fixture()
def proxy():
    p = FakeProxy()
    # Once a test's script is used up: a free chat answer that passes the test skills' check (embeddings: the fake's default).
    p.responder = lambda request: None if request["path"].endswith("/embeddings") else chat("GOOD")
    yield p
    p.stop()


@pytest.fixture()
def env(tmp_path, monkeypatch, _isolated_muse_home, proxy):
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_PROXY_URL", proxy.url)
    monkeypatch.setenv("MUSE_TZ", "UTC")
    return dict(keys.export_env())
