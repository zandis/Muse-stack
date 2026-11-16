"""Shared test setup: import from the repo, and never touch the real ~/.muse."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


@pytest.fixture(autouse=True)
def _isolated_muse_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private MUSE_HOME per test so ledger and local catalogue writes stay in tmp."""
    home = tmp_path_factory.mktemp("muse-home")
    monkeypatch.setenv("MUSE_HOME", str(home))
    monkeypatch.delenv("MUSE_LOCAL_CATALOG", raising=False)
    # Older tests exercise the runner with throw-away skills written into the home. Outside tests such a skill runs
    # only inside the sandbox (docs/sandbox.md); tests/test_sandbox*.py remove this to test that rule.
    monkeypatch.setenv("MUSE_SANDBOX_UNSAFE_HOME_SKILLS", "in-process-for-tests")
    monkeypatch.delenv("MUSE_SANDBOX", raising=False)
    for name in ("MUSE_ROUTING", "MUSE_CACHE", "MUSE_CACHE_TTL_S", "MUSE_CACHE_MAX_ENTRIES"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture(autouse=True)
def _no_real_proxy_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /v1/home/restart-proxy` runs `muse restart` on a real home. No test may restart a real router: here the
    home never "manages" one unless a test says so."""
    from muse.home import app_api

    monkeypatch.setattr(app_api.AppBackend, "manages_proxy", lambda self: (False, []))
