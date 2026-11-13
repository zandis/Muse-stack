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

The file also remembers the SHA-256 of each app key removed with its permission (never the key), so a stopped
key is answered with "the permission was stopped" rather than "unknown key".
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

_cache: dict[str, tuple[Any, Any]] = {}
_lock = threading.Lock()


def stopped_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "permissions" / FILE_NAME


def _stamp(path: Path) -> Any:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


MAX_APP_HASHES = 5000  # removed app keys remembered, so a stopped key gets a clear answer rather than "unknown key"


def _parse(text: str) -> tuple[dict[str, int], dict[str, str]]:
    """(stopped permission id -> when, sha256 of a removed app key -> its permission id)."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("mandates"), dict):
        raise ValueError("not a stopped list")
    if not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) for k, v in data["mandates"].items()):
        raise ValueError("not a stopped list")
    keys = data.get("apps", {})
    if not isinstance(keys, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items()):
        raise ValueError("not a stopped list")
    return dict(data["mandates"]), dict(keys)


def _read(environ: Mapping[str, str] | None) -> tuple[frozenset[str], Mapping[str, str]] | None | bool:
    """Cached by the file's stamp. False: no file. None: a file that cannot be read."""
    path = stopped_path(environ)
    stamp = _stamp(path)
    if stamp is None:
        return False
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        ids, hashes = _parse(path.read_text(encoding="utf-8"))
        got: Any = (frozenset(ids), hashes)
    except (OSError, UnicodeDecodeError, ValueError):
        got = None
    with _lock:
        _cache[key] = (stamp, got)
    return got


def stopped_ids(environ: Mapping[str, str] | None = None) -> frozenset[str] | None:
    """The ids of stopped permissions; an empty set when there is no file; None when the file cannot be read."""
    got = _read(environ)
    if got is False:
        return frozenset()
    return None if got is None else got[0]


def stopped_key(token_sha256: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The permission id behind an app key that was removed because its permission stopped or ended, or None."""
    got = _read(environ)
    if not got:
        return None
    return got[1].get(token_sha256)


def mark_stopped(mandate_ids: Iterable[str], environ: Mapping[str, str] | None = None, now: float | None = None,
                 app_keys: Mapping[str, str] | None = None) -> None:
    """Add ids to the stopped list (it only grows), and the hashes of app keys about to be removed with them.
    Written 0600 through a temp file and os.replace.

    A file that cannot be read is replaced by one holding the ids given: `sweep` gives every revoked id.
    One writer at a time (an advisory lock), so two stops at once cannot lose each other's id."""
    path = stopped_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(".stopped.lock")), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            pass
        _mark_locked(path, mandate_ids, now, app_keys)
    finally:
        os.close(fd)


def _mark_locked(path: Path, mandate_ids: Iterable[str], now: float | None, app_keys: Mapping[str, str] | None) -> None:
    try:
        current: tuple[dict[str, int], dict[str, str]] | None = _parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = ({}, {})
    except (OSError, UnicodeDecodeError, ValueError):
        current = None
    when = int(time.time() if now is None else now)
    merged = dict(current[0] if current else {})
    hashes = dict(current[1] if current else {})
    for ident in mandate_ids:
        merged.setdefault(ident, when)
    for digest, ident in (app_keys or {}).items():
        hashes[digest] = ident
    while len(hashes) > MAX_APP_HASHES:
        hashes.pop(next(iter(hashes)))
    if current is not None and (merged, hashes) == current and path.is_file():
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"v": 1, "mandates": merged, "apps": hashes}, sort_keys=True) + "\n")
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
