"""Where the home keeps the root key and its tokens, until the vault package takes over.

- `MUSE_HOME/root.key`: the person's root private key, one line, mode 0600.
  Loading sits behind `KeySource`, so the vault can replace the file without touching callers.
- `MUSE_HOME/permissions/`: one file per token (mode 0600), the revocation list and the spent set.

This module does I/O; the rest of the package does none.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Protocol

from wardmuse import paths
from wardmuse.permissions import token as _token
from wardmuse.permissions.lists import RevocationList, SpentSet
from wardmuse.permissions.model import PermissionError
from wardmuse.permissions.token import Mandate, revocation_ids


class KeySource(Protocol):
    """Where the root key comes from. The vault will provide another implementation."""

    def load(self) -> Any:
        """The root key pair (as `parse_private_key` returns)."""

    def public(self) -> str:
        """The root public key as text."""


def _write_private(path: Path, text: str) -> None:
    """Write `text` so that only the owner can read it, replacing the file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class FileKeySource:
    """The root key in `MUSE_HOME/root.key` (mode 0600)."""

    def __init__(self, path: Path | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.path = path or paths.home(environ) / "root.key"

    def exists(self) -> bool:
        return self.path.is_file()

    def create(self) -> str:
        """Make a new root key; refuses to replace one. Returns the public key."""
        if self.exists():
            raise PermissionError("key_exists", f"{self.path} already holds a root key")
        _write_private(self.path, _token.generate_private_key() + "\n")
        return self.public()

    def load(self) -> Any:
        if not self.exists():
            raise PermissionError("no_root_key", f"no root key at {self.path}")
        if os.name == "posix" and stat.S_IMODE(self.path.stat().st_mode) & 0o077:
            raise PermissionError("insecure_key_file", f"{self.path} is readable by others; run: chmod 600 {self.path}")
        return _token.parse_private_key(self.path.read_text(encoding="utf-8").strip())

    def public(self) -> str:
        return _token.public_key_text(self.load())


class PermissionStore:
    """Tokens, the revocation list and the spent set under `MUSE_HOME/permissions/`."""

    def __init__(self, directory: Path | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.dir = directory or paths.home(environ) / "permissions"

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.dir, 0o700)

    def save(self, mandate: Mandate) -> str:
        """Store a token and return its name: the first 12 hex digits of the mandate id for
        an issued permission, plus `-` and 8 of the last block's revocation id for a narrowed
        copy (so a family sorts together and the permission itself has the short name)."""
        self._ensure()
        token = mandate.to_base64()
        last = revocation_ids(mandate)[-1]
        name = mandate.id[3:15] + (f"-{last[:8]}" if mandate.levels else "")
        path = self.dir / f"{name}.token"
        if path.is_file() and path.read_text(encoding="utf-8").strip() != token:
            name = f"{mandate.id[3:15]}-{last[:16]}"  # the same draft signed twice, or a clash of 8 digits
        _write_private(self.dir / f"{name}.token", token + "\n")
        return name

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.token")) if self.dir.is_dir() else []

    def resolve(self, prefix: str) -> str:
        """The stored name equal to `prefix`, or else the one name that starts with it."""
        names = self.names()
        if prefix in names:
            return prefix
        found = [n for n in names if n.startswith(prefix)]
        if len(found) != 1:
            raise PermissionError("not_found" if not found else "ambiguous", f"{len(found)} stored permissions start with {prefix!r}")
        return found[0]

    def read(self, name: str) -> str:
        return (self.dir / f"{name}.token").read_text(encoding="utf-8").strip()

    def load(self, name: str, root: str) -> Mandate:
        return Mandate.parse(self.read(name), root)

    def remove(self, name: str) -> None:
        (self.dir / f"{name}.token").unlink(missing_ok=True)

    # -------- revocation list and spent set

    def revocations(self, root: str) -> RevocationList:
        path = self.dir / "revocations.json"
        return RevocationList.from_json(path.read_text(encoding="utf-8"), root) if path.is_file() else RevocationList()

    def save_revocations(self, revoked: RevocationList) -> None:
        self._ensure()
        _write_private(self.dir / "revocations.json", revoked.to_json() + "\n")

    def spent(self) -> SpentSet:
        path = self.dir / "spent.json"
        if not path.is_file():
            return SpentSet()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SpentSet.from_entries(data["entries"])
        except (ValueError, KeyError, TypeError) as exc:
            # Failing open here would forget which approvals were used.
            raise PermissionError("invalid_spent_set", f"{path} is unreadable: {exc}") from exc

    def save_spent(self, spent: SpentSet) -> None:
        self._ensure()
        _write_private(self.dir / "spent.json", json.dumps({"v": 1, "entries": spent.entries()}) + "\n")
