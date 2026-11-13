"""The router's view of permissions: is the permission behind this app entry still in force?

Read by `muse/auth.py` on every request from an app that was made from a permission, so it is
small and needs no extra library (the proxy's environment has no Biscuit library):

  * validity comes from the app entry itself (`valid_from`, `valid_until`, written by the compiler);
  * "stopped" comes from `MUSE_HOME/permissions/stopped.json`, which the compiler writes in the same
    step that signs the revocation, before it removes anything. The file is re-read only when it
    changes on disk (mtime, size, inode).

The signed revocation list (`revocations.json`) stays the record of truth; this file is the copy
the router can read. `sweep` rebuilds it from the signed list. A file that exists but cannot be
read refuses every app made from a permission: failing open would un-stop one.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from muse import paths

FILE_NAME = "stopped.json"
STOPPED = "stopped"
EXPIRED = "expired"
NOT_YET = "not_yet_valid"
UNREADABLE = "unreadable"

_cache: dict[str, tuple[Any, frozenset[str] | None]] = {}
_lock = threading.Lock()


def stopped_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "permissions" / FILE_NAME


def _stamp(path: Path) -> Any:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _parse(text: str) -> dict[str, int]:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("mandates"), dict):
        raise ValueError("not a stopped list")
    if not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in data["mandates"].items()):
        raise ValueError("not a stopped list")
    return dict(data["mandates"])


def stopped_ids(environ: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The ids of stopped permissions; an empty set when there is no file; None when the file cannot be read."""
    path = stopped_path(environ)
    stamp = _stamp(path)
    if stamp is None:
        return frozenset()
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        ids: frozenset[str] | None = frozenset(_parse(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        ids = None
    with _lock:
        _cache[key] = (stamp, ids)
    return ids


def mark_stopped(mandate_ids: Iterable[str], environ: Mapping[str, str] | None = None, now: float | None = None) -> None:
    """Add ids to the stopped list (it only grows). Written 0600 through a temp file and os.replace.

    A file that cannot be read is replaced by one holding the ids given: `sweep` gives every revoked id."""
    path = stopped_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current: dict[str, int] | None = _parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except (OSError, UnicodeDecodeError, ValueError):
        current = None
    when = int(time.time() if now is None else now)
    merged = dict(current or {})
    for ident in mandate_ids:
        merged.setdefault(ident, when)
    if current is not None and merged == current and path.is_file():
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 1, "mandates": merged}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refusal(app: Any, environ: Mapping[str, str] | None = None, now: float | None = None) -> str | None:
    """Why an app made from a permission must be turned away now: one of the codes above, or None.

    `app` is a `muse.apps.App`. An app with no `mandate` is not this module's business (None)."""
    mandate = getattr(app, "mandate", None)
    if not mandate:
        return None
    when = time.time() if now is None else now
    until, start = getattr(app, "valid_until", None), getattr(app, "valid_from", None)
    if until is None or when > until:
        return EXPIRED
    if start is not None and when < start:
        return NOT_YET
    ids = stopped_ids(environ)
    if ids is None:
        return UNREADABLE
    return STOPPED if mandate in ids else None


SENTENCES = {
    STOPPED: "the permission this app key was made from was stopped, so the key no longer works. "
             "The person can give a new permission in Muse (Permissions).",
    EXPIRED: "the permission this app key was made from has ended, so the key no longer works. "
             "The person can give a new permission in Muse (Permissions).",
    NOT_YET: "the permission this app key was made from has not started yet.",
    UNREADABLE: "Muse could not read its list of stopped permissions, so app keys made from a permission are refused "
                "until it can. Run `muse permissions sweep` on the home.",
}
