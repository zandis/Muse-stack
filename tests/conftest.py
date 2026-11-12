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
    return home
