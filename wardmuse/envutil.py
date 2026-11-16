"""Read the renamed settings, and the old ones when the new ones are absent.

``WARDMUSE_HOME`` wins over ``MUSE_HOME``. A blank new value does not count as
set, so an empty ``WARDMUSE_*`` variable does not hide a real ``MUSE_*`` one.
Directory defaults follow the same rule: the new path, unless it is missing
and the old path is already there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def _suffix(key: str) -> str:
    if key.startswith("WARDMUSE_"):
        return key[len("WARDMUSE_") :]
    if key.startswith("MUSE_"):
        return key[len("MUSE_") :]
    return key


def envget(environ: Mapping[str, str] | None, key: str, default: str | None = None):
    """Value of ``WARDMUSE_<name>``, else ``MUSE_<name>``, else ``default``.

    ``key`` may be the new name, the old name, or just the suffix (``HOME``).
    """

    env = os.environ if environ is None else environ
    suffix = _suffix(key)
    new_key, old_key = f"WARDMUSE_{suffix}", f"MUSE_{suffix}"
    if new_key in env and str(env[new_key]).strip() != "":
        return env[new_key]
    if old_key in env:
        return env[old_key]
    if new_key in env:
        return env[new_key]
    return default


def envhas(environ: Mapping[str, str] | None, key: str) -> bool:
    """True when either the new or the old variable is present."""

    env = os.environ if environ is None else environ
    suffix = _suffix(key)
    return f"WARDMUSE_{suffix}" in env or f"MUSE_{suffix}" in env


def prefer_existing(new: Path, *olds: Path) -> Path:
    """``new`` when it exists or none of the old paths do; otherwise the first old path that exists."""

    if new.exists():
        return new
    for old in olds:
        if old.exists():
            return old
    return new
