"""Shared helpers for the sandbox tests (no tests here): the fixture skills, the layer matrix, skip reasons.

The fixture skills live in tests/sandbox_skills/. Tests reach them as SHIPPED skills (MUSE_SKILLS_DIR points at that
folder), because a shipped skill follows the requested mode: that lets a test switch single layers off and prove what
each one stops by itself. The rule for skills that are NOT shipped (always `require`) has its own tests.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from wardmuse import sandbox
from wardmuse.sandbox import Policy
from wardmuse.sandbox.policy import ENV_UNSAFE_HOME_SKILLS
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

FIXTURES = Path(__file__).resolve().parent / "sandbox_skills"
CANARY_ENV = "CANARY-ENV-7f3a91"
CANARY_HOME = "CANARY-HOME-b20c55"
CANARY_OUTSIDE = "CANARY-OUTSIDE-e41d08"
LINUX = sys.platform.startswith("linux")

# One layer at a time, then everything this machine has.
# Without bubblewrap there is no user namespace, and RLIMIT_NPROC then counts every process and thread of
# the person running the tests: with parallel test workers the product's default of 64 makes a skill's own
# fork fail with EAGAIN before any layer is asked. Those layers get a roomy limit here; the limit itself is
# tested on its own (test_fork_bomb_is_stopped_by_the_process_limit sets it explicitly).
_ROOMY = 1 << 20
LAYERS: dict[str, Policy] = {
    "bwrap": Policy(use_landlock=False, use_seccomp=False),
    "landlock": Policy(use_bwrap=False, use_seccomp=False, processes=_ROOMY),
    "seccomp": Policy(use_bwrap=False, use_landlock=False, processes=_ROOMY),
    "all": Policy() if sandbox.probe().bwrap_works else Policy(processes=_ROOMY),  # no bubblewrap here: no namespace either
    "none": Policy(use_bwrap=False, use_landlock=False, use_seccomp=False, processes=_ROOMY),
}


def caps() -> sandbox.Capabilities:
    return sandbox.probe()


def need(layer: str, *, landlock_abi: int = 1) -> None:
    """Skip the calling test, with the reason, unless `layer` really works on this machine."""
    c = caps()
    if not LINUX:
        pytest.skip(f"the sandbox layers are Linux-only; this is {sys.platform}")
    if layer == "bwrap" and not c.bwrap_works:
        pytest.skip(f"bubblewrap cannot run here: {c.bwrap_reason or 'not installed'}")
    if layer == "landlock" and not c.landlock_works:
        pytest.skip(f"Landlock cannot be used here: {c.landlock_reason or 'not offered by the kernel'}")
    if layer == "landlock" and c.landlock_abi < landlock_abi:
        pytest.skip(f"this needs Landlock ABI {landlock_abi}; the kernel has ABI {c.landlock_abi}")
    if layer == "seccomp" and not c.seccomp_works:
        pytest.skip(f"the seccomp filter cannot be used here: {c.seccomp_reason or 'unknown'}")
    if layer == "all" and not c.can_require:
        pytest.skip("no complete sandbox here: " + "; ".join(c.missing()))


@pytest.fixture()
def box(env, tmp_path, monkeypatch):  # noqa: F811
    """A home with canaries, a folder to read, a folder that must stay out of reach, and the fixture skills as shipped."""
    monkeypatch.setenv("MUSE_SKILLS_DIR", str(FIXTURES))
    monkeypatch.setenv("MUSE_CANARY_SECRET", CANARY_ENV)
    monkeypatch.setenv("GROQ_API_KEY", CANARY_ENV)
    env = dict(env, MUSE_SKILLS_DIR=str(FIXTURES), MUSE_CANARY_SECRET=CANARY_ENV, GROQ_API_KEY=CANARY_ENV)
    home = Path(envget(env, "MUSE_HOME"))
    (home / "canary.txt").write_text(CANARY_HOME, encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    (docs / "b.md").write_text("# Title\n\ndelta epsilon\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text(CANARY_OUTSIDE, encoding="utf-8")
    return type("Box", (), {"env": env, "home": home, "docs": docs, "outside": outside, "tmp": tmp_path,
                            "data": home / "skills-data" / "escape"})()


def run(box: Any, name: str, inputs: dict[str, Any], layer: str = "all", mode: str = "auto", **options: Any):
    """(Result, Report) for one sandboxed run of a fixture skill."""
    options.setdefault("roots", [box.docs])
    policy = options.pop("policy", None) or LAYERS[layer]
    return sandbox.run_skill(name, inputs, mode=mode, policy=policy, environ=box.env, **options)


def attempt(box: Any, what: str, layer: str = "all", **inputs: Any):
    """Run one escape attempt. Returns (the skill's own report dict or None, Result, Report).

    `policy=` and `budget=` are options of the run; every other keyword is an input for the skill.
    """
    options = {key: inputs.pop(key) for key in ("policy", "budget") if key in inputs}
    result, report = run(box, "escape", {"attempt": what, **inputs}, layer, **options)
    return (result.output if isinstance(result.output, dict) else None), result, report


def real_home_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch the test-only escape hatch off, so skills that are not shipped get the real rule."""
    monkeypatch.delenv(ENV_UNSAFE_HOME_SKILLS, raising=False)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
