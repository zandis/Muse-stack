"""Where state lives. Everything is under MUSE_HOME (default ~/.muse)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "config" / "providers.yaml"


def home(environ: Mapping[str, str] | None = None) -> Path:
    """State directory, created 0700 on first use."""
    env = os.environ if environ is None else environ
    raw = env.get("MUSE_HOME", "").strip()
    path = Path(raw).expanduser() if raw else Path.home() / ".muse"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass  # a bind mount may not allow chmod
    return path


def ledger_path(environ: Mapping[str, str] | None = None) -> Path:
    return home(environ) / "ledger.sqlite"


def local_catalog_path(environ: Mapping[str, str] | None = None) -> Path:
    """Must agree with scripts/render_config.py::local_catalog_path."""
    env = os.environ if environ is None else environ
    explicit = env.get("MUSE_LOCAL_CATALOG", "").strip()
    return Path(explicit).expanduser() if explicit else home(environ) / "local.yaml"
