"""Applications that share this one endpoint, and the tokens they authenticate with.

Why this exists: one person runs several programs (the built-in bot, chat
front-ends, scripts, coding tools) through one proxy. Each gets its own token
and a priority, so the router can ration free quota between them (muse/ration.py)
and say which one used what (muse/ledger.py).

The registry is `MUSE_HOME/apps.yaml`:

    version: 1
    apps:
      - name: notes-batch
        token_sha256: 5e88...        # only the hash; the token is shown once
        priority: background         # interactive | normal | background
        daily_requests: 500          # optional
        daily_tokens: 2000000        # optional
        allow_paid: false            # default false for background, true otherwise
        default_privacy: private     # optional; replaces MUSE_DEFAULT_PRIVACY for this app
        created_at: "2026-11-12T07:00:00Z"
        note: nightly summaries      # optional

File safety: written 0600 through a temp file and os.replace (a reader never
sees half a file), under an advisory lock so two `muse apps` commands cannot
lose each other's change. A missing or malformed file means "no apps" and never
stops the proxy; a malformed file is never overwritten (fix or delete it).
A running proxy re-reads the file when its mtime/size/inode change.

Built-in apps, never stored: `owner` (the master key; interactive, paid allowed)
and `anonymous` (no master key and no recognised token; normal, no paid).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from muse import paths

log = logging.getLogger("muse.apps")

PRIORITIES: tuple[str, ...] = ("interactive", "normal", "background")
PRIVACY_CLASSES: tuple[str, ...] = ("public", "standard", "private", "local")
TOKEN_PREFIX = "sk-muse-app-"
TOKEN_BYTES = 32
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
OWNER_NAME = "owner"
ANONYMOUS_NAME = "anonymous"
RESERVED_NAMES = frozenset({OWNER_NAME, ANONYMOUS_NAME, "all", "none", "default", "muse"})
FILE_VERSION = 1


class AppError(ValueError):
    """A registry change or entry that is not valid. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class App:
    """One application's policy. `token_sha256` is empty for the built-ins."""

    name: str
    priority: str
    token_sha256: str = ""
    daily_requests: int | None = None
    daily_tokens: int | None = None
    allow_paid: bool = False
    default_privacy: str | None = None
    created_at: str = ""
    note: str | None = None
    builtin: bool = field(default=False, compare=False)

    def to_record(self) -> dict[str, Any]:
        """The YAML entry (no `builtin`, no empty optionals)."""
        out = {k: v for k, v in asdict(self).items() if k != "builtin" and v is not None}
        return out

    def policy(self) -> dict[str, Any]:
        """What the proxy carries with each request (no hash: it is not needed downstream)."""
        return {
            "muse_app": self.name,
            "muse_priority": self.priority,
            "muse_allow_paid": bool(self.allow_paid),
            "muse_daily_requests": self.daily_requests,
            "muse_daily_tokens": self.daily_tokens,
            "muse_default_privacy": self.default_privacy,
        }


OWNER = App(name=OWNER_NAME, priority="interactive", allow_paid=True, builtin=True)
ANONYMOUS = App(name=ANONYMOUS_NAME, priority="normal", allow_paid=False, builtin=True)


# ---------------------------------------------------------------- validation


def hash_token(token: str) -> str:
    """SHA-256 hex of a token. Tokens are 256-bit random, so no salt or KDF is needed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def _opt_cap(value: Any, what: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise AppError(f"{what} must be a whole number, not {value!r}")
    try:
        number = int(str(value).strip())
    except ValueError:
        raise AppError(f"{what} must be a whole number, not {value!r}") from None
    if number < 0:
        raise AppError(f"{what} cannot be negative")
    return number


def _bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise AppError(f"{what} must be true or false, not {value!r}")


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise AppError(
            "app name must be 1-40 characters of lower-case letters, digits and '-', "
            "starting and ending with a letter or digit"
        )
    return name


def _validate(raw: Mapping[str, Any], *, new: bool = False) -> App:
    name = validate_name(raw.get("name"))
    if new and name in RESERVED_NAMES:
        raise AppError(f"'{name}' is reserved")
    priority = raw.get("priority")
    if priority not in PRIORITIES:
        raise AppError(f"priority must be one of {', '.join(PRIORITIES)}, not {priority!r}")
    token_sha256 = str(raw.get("token_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", token_sha256):
        raise AppError(f"app '{name}' has no valid token_sha256")
    allow = raw.get("allow_paid")
    allow_paid = (priority != "background") if allow is None else _bool(allow, "allow_paid")
    privacy = raw.get("default_privacy")
    if privacy is not None:
        privacy = str(privacy).strip().lower()
        if privacy not in PRIVACY_CLASSES:
            raise AppError(f"default_privacy must be one of {', '.join(PRIVACY_CLASSES)}, not {raw.get('default_privacy')!r}")
    note = raw.get("note")
    if note is not None:
        note = str(note)
        if len(note) > 200 or "\n" in note:
            raise AppError("note must be one line of at most 200 characters")
    return App(
        name=name,
        priority=str(priority),
        token_sha256=token_sha256,
        daily_requests=_opt_cap(raw.get("daily_requests"), "daily_requests"),
        daily_tokens=_opt_cap(raw.get("daily_tokens"), "daily_tokens"),
        allow_paid=allow_paid,
        default_privacy=privacy,
        created_at=str(raw.get("created_at") or ""),
        note=note,
    )


# ---------------------------------------------------------------- file access


def apps_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("MUSE_APPS_FILE", "").strip()
    return Path(explicit).expanduser() if explicit else paths.home(environ) / "apps.yaml"


_cache: dict[str, tuple[tuple[int, int, int], dict[str, App]]] = {}
_cache_lock = threading.Lock()
_warned: set[tuple[str, tuple[int, int, int]]] = set()


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class _Malformed(Exception):
    pass


def _parse(text: str) -> dict[str, App]:
    """Strict whole-file parse. Raises _Malformed; bad single entries are skipped."""
    try:
        data = yaml.safe_load(text) if text.strip() else {"apps": []}
    except yaml.YAMLError:
        raise _Malformed("not valid YAML") from None
    if data is None:
        data = {"apps": []}
    if not isinstance(data, dict) or not isinstance(data.get("apps", []), list):
        raise _Malformed("expected a mapping with an 'apps' list")
    out: dict[str, App] = {}
    for index, item in enumerate(data.get("apps") or []):
        if not isinstance(item, Mapping):
            log.warning("muse apps: entry %d is not a mapping; skipped", index)
            continue
        try:
            app = _validate(item)
        except AppError as exc:
            log.warning("muse apps: entry %d skipped: %s", index, exc)
            continue
        if app.name in RESERVED_NAMES or app.name in out:
            log.warning("muse apps: entry %d skipped: duplicate or reserved name", index)
            continue
        out[app.name] = app
    return out


def load_apps(environ: Mapping[str, str] | None = None) -> dict[str, App]:
    """Registered apps by name. Missing or malformed file -> {} (logged once), never raises."""
    path = apps_path(environ)
    key = str(path)
    stamp = _stamp(path)
    if stamp is None:
        return {}
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == stamp:
            return dict(cached[1])
    try:
        text = path.read_text(encoding="utf-8")
        apps = _parse(text)
    except (OSError, UnicodeDecodeError, _Malformed) as exc:
        if (key, stamp) not in _warned:
            _warned.add((key, stamp))
            log.warning("muse apps: %s is unreadable or malformed (%s); treating it as no apps", path, exc)
        apps = {}
    with _cache_lock:
        _cache[key] = (stamp, apps)
    return dict(apps)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Advisory lock next to the registry. No-op where fcntl is missing (Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            pass
        yield
    finally:
        os.close(fd)


def _read_for_update(path: Path) -> dict[str, App]:
    """Like load_apps, but a malformed file is an error: we must not overwrite it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise AppError(f"cannot read {path}: {type(exc).__name__}") from None
    try:
        return _parse(text)
    except _Malformed as exc:
        raise AppError(f"{path} is malformed ({exc}); fix or remove it before changing apps") from None


def _write(path: Path, apps: Mapping[str, App]) -> None:
    body = {"version": FILE_VERSION, "apps": [apps[name].to_record() for name in sorted(apps)]}
    text = (
        "# muse application registry. Written by muse; tokens are stored only as SHA-256.\n"
        + yaml.safe_dump(body, sort_keys=False, default_flow_style=False, allow_unicode=True)
    )
    fd, tmp = tempfile.mkstemp(prefix=".apps-", suffix=".yaml", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _utc_now(now: float | None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- public API


def add_app(
    name: str,
    priority: str,
    *,
    daily_requests: int | None = None,
    daily_tokens: int | None = None,
    allow_paid: bool | None = None,
    default_privacy: str | None = None,
    note: str | None = None,
    environ: Mapping[str, str] | None = None,
    now: float | None = None,
) -> tuple[App, str]:
    """Register an app. Returns (app, token); the token is not stored and cannot be shown again."""
    path = apps_path(environ)
    token = new_token()
    with _locked(path):
        apps = _read_for_update(path)
        if name in apps:
            raise AppError(f"app '{name}' already exists; use rotate_token or remove it first")
        app = _validate(
            {
                "name": name, "priority": priority, "token_sha256": hash_token(token),
                "daily_requests": daily_requests, "daily_tokens": daily_tokens, "allow_paid": allow_paid,
                "default_privacy": default_privacy, "created_at": _utc_now(now), "note": note,
            },
            new=True,
        )
        apps[app.name] = app
        _write(path, apps)
    return app, token


def update_app(name: str, environ: Mapping[str, str] | None = None, **changes: Any) -> App:
    """Change priority, caps, allow_paid, default_privacy or note. Pass None to clear an optional cap."""
    allowed = {"priority", "daily_requests", "daily_tokens", "allow_paid", "default_privacy", "note"}
    unknown = set(changes) - allowed
    if unknown:
        raise AppError(f"cannot change {', '.join(sorted(unknown))}")
    path = apps_path(environ)
    with _locked(path):
        apps = _read_for_update(path)
        if name not in apps:
            raise AppError(f"no app named '{name}'")
        record = apps[name].to_record()
        record.update(changes)
        if "allow_paid" in changes and changes["allow_paid"] is None:
            record.pop("allow_paid")  # back to the priority's default
        app = _validate(record)
        apps[name] = app
        _write(path, apps)
    return app


def remove_app(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Delete an app. Its token stops working at the next request. False if it did not exist."""
    path = apps_path(environ)
    with _locked(path):
        apps = _read_for_update(path)
        if name not in apps:
            return False
        del apps[name]
        _write(path, apps)
    return True


def rotate_token(name: str, environ: Mapping[str, str] | None = None) -> tuple[App, str]:
    """Issue a new token for an app; the old one stops working at once."""
    path = apps_path(environ)
    token = new_token()
    with _locked(path):
        apps = _read_for_update(path)
        if name not in apps:
            raise AppError(f"no app named '{name}'")
        app = replace(apps[name], token_sha256=hash_token(token))
        apps[name] = app
        _write(path, apps)
    return app, token


def find_by_token(token: str | None, environ: Mapping[str, str] | None = None) -> App | None:
    """The app this token belongs to, or None.

    Compares the token's hash with every entry using hmac.compare_digest and
    never stops early, so timing does not reveal which entry (if any) matched.
    """
    if not isinstance(token, str) or not token:
        return None
    digest = hash_token(token)
    found: App | None = None
    for app in load_apps(environ).values():
        if hmac.compare_digest(digest, app.token_sha256) and found is None:
            found = app
    return found


def resolve(app_name: str | None, environ: Mapping[str, str] | None = None) -> App | None:
    """An app by name, including the built-ins `owner` and `anonymous`."""
    if app_name == OWNER_NAME:
        return OWNER
    if app_name == ANONYMOUS_NAME:
        return ANONYMOUS
    if not app_name:
        return None
    return load_apps(environ).get(app_name)


def from_policy(policy: Mapping[str, Any] | None) -> App | None:
    """Rebuild an App from the per-request dict `App.policy()` produced (see muse/auth.py)."""
    if not isinstance(policy, Mapping) or not policy.get("muse_app"):
        return None
    name = str(policy["muse_app"])
    priority = policy.get("muse_priority")
    if priority not in PRIORITIES:
        return None

    def cap(key: str) -> int | None:
        value = policy.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    privacy = policy.get("muse_default_privacy")
    return App(
        name=name,
        priority=str(priority),
        daily_requests=cap("muse_daily_requests"),
        daily_tokens=cap("muse_daily_tokens"),
        allow_paid=policy.get("muse_allow_paid") is True,
        default_privacy=privacy if privacy in PRIVACY_CLASSES else None,
        builtin=name in (OWNER_NAME, ANONYMOUS_NAME),
    )
