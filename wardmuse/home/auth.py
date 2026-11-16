"""Device tokens for the home control API.

Every request to the home carries `Authorization: Bearer <device token>`. In v1
a device token is a random 32-byte secret made by `muse home token add <name>`.
Only its SHA-256 is stored, in `MUSE_HOME/home-tokens.json` (0600), in the same
style as app tokens (muse/apps.py): shown once, compared in constant time,
rotated or removed by name.

The check sits behind one small interface, `Verifier.verify(token) -> Device | None`.
Handlers only ever see the `Device` it returns. That is deliberate: v1's shared
secret is a stand-in for a mandate token (Biscuit, architecture section 2.7)
narrowed from the device's mandate. When mandates exist, a `MandateVerifier`
that checks the token's signature, caveats and revocation replaces
`TokenFileVerifier` here, and no handler or route changes.

Roles. Every device has one role, and a role is a fixed set of scopes. Each
route of the API names the one scope it needs (muse/home/api.py), and the check
is made in one place before any handler runs:

  owner    the person's own app or command line: everything.
  host     the tools server a host bot runs (`muse hosts setup`). Only what that
           server needs: read status and the catalogue, ask the person (create
           cards) and read ITS OWN cards, queue / read / cancel ITS OWN jobs,
           add records, list and run skills and read ITS OWN runs, ASK for a
           permission for itself (a card the person answers) and ask what its
           own permission says. It cannot issue or stop a permission,
           pause or resume, store keys, connect services, manage apps, answer a
           card, read the records, or see another device's jobs, cards or runs.
  viewer   read-only: status, catalogue, cards, jobs, records, permissions, the chore list and the settings.

Asking a question, the kept conversations, changing chores or settings, storing or removing
a key and restarting the router are the person's own (`owner`) only.

A token file written before roles existed has no `role` field; such a device is
an `owner`, which is what every token was until then.

File safety is the same as the app registry: written through a temp file and
os.replace under an advisory lock; a missing or malformed file means "no
devices" (every request is refused) and a malformed file is never overwritten.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from wardmuse import paths

log = logging.getLogger("muse.home")

TOKEN_PREFIX = "sk-muse-dev-"
TOKEN_BYTES = 32
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
FILE_VERSION = 1
MAX_DEVICES = 64
MAX_TOKEN_CHARS = 512  # longer bearer values are refused before hashing

# ---------------------------------------------------------------- roles and scopes

OWNER, HOST, VIEWER = "owner", "host", "viewer"
ROLES = (OWNER, HOST, VIEWER)
DEFAULT_ROLE = OWNER  # also what a token file from before roles means

# Every scope a route can need. A route names exactly one (muse/home/api.py::ROUTES).
SCOPES = (
    "status.read", "catalogue.read",
    "connect.write", "keys.write",
    "jobs.read", "jobs.write",
    "cards.read", "cards.ask", "cards.answer",
    "pause.write",
    "apps.read", "apps.write",
    "receipts.read", "receipts.write", "receipts.export",
    "skills.read", "skills.run",
    "mandates.read", "mandates.request", "mandates.write",
    # The app's screens (muse/home/app_api.py).
    "ask.write", "chores.read", "chores.write", "settings.read", "settings.write", "home.admin",
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    OWNER: frozenset(SCOPES),
    HOST: frozenset({"status.read", "catalogue.read", "cards.read", "cards.ask", "jobs.read", "jobs.write",
                     "receipts.write", "skills.read", "skills.run", "mandates.request"}),
    VIEWER: frozenset({"status.read", "catalogue.read", "cards.read", "jobs.read", "receipts.read", "mandates.read",
                       "chores.read", "settings.read"}),
}

# Roles that see every device's jobs, cards and skill runs. Any other role sees only what it made itself.
SEES_ALL = frozenset({OWNER, VIEWER})


def allows(role: str, scope: str) -> bool:
    """May a device with `role` use a route that needs `scope`? Unknown roles and scopes are refused."""
    return bool(scope) and scope in SCOPES and scope in ROLE_SCOPES.get(role, frozenset())


class TokenError(ValueError):
    """A token-store change that is not valid. The message is safe to show."""


@dataclass(frozen=True, slots=True)
class Device:
    """Who is calling. All a handler ever learns about the caller."""

    name: str
    created_at: str = ""
    rotated_at: str | None = None
    role: str = DEFAULT_ROLE

    def can(self, scope: str) -> bool:
        return allows(self.role, scope)

    @property
    def sees_all(self) -> bool:
        """Does this device see every device's jobs, cards and skill runs, or only its own?"""
        return self.role in SEES_ALL


class Verifier(Protocol):
    """The whole authentication interface. Replace the implementation, not the callers."""

    def verify(self, token: str) -> Device | None:  # pragma: no cover - protocol
        ...


def hash_token(token: str) -> str:
    """SHA-256 hex. Tokens are 256-bit random, so no salt or KDF is needed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise TokenError(
            "device name must be 1-40 characters of lower-case letters, digits and '-', "
            "starting and ending with a letter or digit"
        )
    return name


def validate_role(role: Any) -> str:
    if not isinstance(role, str) or role not in ROLES:
        raise TokenError(f"role must be one of {', '.join(ROLES)}")
    return role


def tokens_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "home-tokens.json"


def _utc(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Malformed(Exception):
    pass


def _parse(text: str) -> dict[str, dict[str, Any]]:
    """name -> record. Raises _Malformed for a file that is not ours."""
    try:
        data = json.loads(text) if text.strip() else {"devices": []}
    except ValueError:
        raise _Malformed("not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("devices", []), list):
        raise _Malformed("expected an object with a 'devices' list")
    out: dict[str, dict[str, Any]] = {}
    for item in data.get("devices") or []:
        if not isinstance(item, dict):
            continue
        name, digest = item.get("name"), item.get("token_sha256")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in out:
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        rotated = item.get("rotated_at")
        role = item.get("role", DEFAULT_ROLE)  # absent: a token from before roles, which could do everything
        if not isinstance(role, str) or role not in ROLES:
            continue  # a role this version does not know grants nothing: that device cannot sign in
        out[name] = {
            "name": name, "token_sha256": digest, "created_at": str(item.get("created_at") or ""),
            "rotated_at": rotated if isinstance(rotated, str) else None, "role": role,
        }
    return out


def _load(environ: Mapping[str, str] | None) -> dict[str, dict[str, Any]]:
    """For verification: never raises; anything wrong means no devices."""
    path = tokens_path(environ)
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, _Malformed) as exc:
        log.warning("home tokens: %s is unreadable or malformed (%s); no device can sign in", path.name, type(exc).__name__)
        return {}


def _read_for_update(path: Path) -> dict[str, dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenError(f"cannot read {path}: {type(exc).__name__}") from None
    try:
        return _parse(text)
    except _Malformed as exc:
        raise TokenError(f"{path} is malformed ({exc}); fix or remove it before changing devices") from None


@contextmanager
def _locked(path: Path) -> Iterator[None]:
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


def _write(path: Path, devices: Mapping[str, Mapping[str, Any]]) -> None:
    body = {
        "version": FILE_VERSION,
        "note": "Written by muse. Device tokens are stored only as SHA-256.",
        "devices": [{k: v for k, v in devices[name].items() if v is not None} for name in sorted(devices)],
    }
    fd, tmp = tempfile.mkstemp(prefix=".home-tokens-", suffix=".json", dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):  # pragma: no cover - Windows
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2)
            handle.write("\n")
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


def _device(record: Mapping[str, Any]) -> Device:
    return Device(name=record["name"], created_at=record.get("created_at") or "", rotated_at=record.get("rotated_at"),
                  role=record.get("role") or DEFAULT_ROLE)


# ---------------------------------------------------------------- public API


def add_token(name: str, environ: Mapping[str, str] | None = None, now: float | None = None,
              role: str = DEFAULT_ROLE) -> tuple[Device, str]:
    """Register a device with a role. Returns (device, token); the token is not stored and cannot be shown again."""
    validate_name(name)
    validate_role(role)
    path = tokens_path(environ)
    token = new_token()
    with _locked(path):
        devices = _read_for_update(path)
        if name in devices:
            raise TokenError(f"device '{name}' already exists; rotate its token or remove it first")
        if len(devices) >= MAX_DEVICES:
            raise TokenError(f"there are already {MAX_DEVICES} devices; remove one first")
        devices[name] = {"name": name, "token_sha256": hash_token(token), "created_at": _utc(now), "rotated_at": None,
                         "role": role}
        _write(path, devices)
        return _device(devices[name]), token


def rotate_token(name: str, environ: Mapping[str, str] | None = None, now: float | None = None) -> tuple[Device, str]:
    """Issue a new token for a device; the old one stops working at once."""
    path = tokens_path(environ)
    token = new_token()
    with _locked(path):
        devices = _read_for_update(path)
        if name not in devices:
            raise TokenError(f"no device named '{name}'")
        devices[name] = {**devices[name], "token_sha256": hash_token(token), "rotated_at": _utc(now)}
        _write(path, devices)
        return _device(devices[name]), token


def remove_token(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Forget a device. Its token stops working at the next request. False if it did not exist."""
    path = tokens_path(environ)
    with _locked(path):
        devices = _read_for_update(path)
        if name not in devices:
            return False
        del devices[name]
        _write(path, devices)
    return True


def list_devices(environ: Mapping[str, str] | None = None) -> list[Device]:
    return [_device(r) for _, r in sorted(_load(environ).items())]


def _match(token: Any, records: Mapping[str, Mapping[str, Any]]) -> Device | None:
    """Compare against every entry without stopping early, so timing says nothing about which matched."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return None
    digest = hash_token(token)
    found: Device | None = None
    for record in records.values():
        if hmac.compare_digest(digest, record["token_sha256"]) and found is None:
            found = _device(record)
    return found


class TokenFileVerifier:
    """v1: bearer secrets checked against the hashes in home-tokens.json.

    The file is read on every request (it is tiny), so `muse home token rm`
    takes effect at once without restarting the server.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self.environ = environ

    def verify(self, token: str) -> Device | None:
        return _match(token, _load(self.environ))


class StaticVerifier:
    """Fixed tokens held in memory as hashes. For the fake home and tests."""

    def __init__(self, tokens: Mapping[str, str], roles: Mapping[str, str] | None = None) -> None:
        """`tokens` maps device name -> token; `roles` maps device name -> role (default: owner)."""
        roles = dict(roles or {})
        for role in roles.values():
            validate_role(role)
        self._records = {name: {"name": name, "token_sha256": hash_token(tok), "created_at": "",
                                "role": roles.get(name, DEFAULT_ROLE)} for name, tok in tokens.items()}

    def verify(self, token: str) -> Device | None:
        return _match(token, self._records)
