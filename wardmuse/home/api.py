"""The home control API, version 1: route table, handlers, and the `Home` behind them.

This module knows nothing about sockets. `muse/home/server.py` reads a request
off the wire, calls `authenticate` and then `dispatch`, and writes the reply.
The contract is `docs/spec/interfaces/home-api.v1.yaml`; a test walks `ROUTES`
and fails if the two disagree.

Rules every handler keeps:
  * A reply never holds a provider key, a prompt, a queued job's result, or
    exception text. The only secrets that ever leave are a NEW app token, once,
    on create/rotate. The one reply that holds the person's content is a skill
    run's answer (`GET /v1/skills/runs/{id}`), returned to the device that
    started the run or to one that sees everything.
  * The home records what it does (muse/records). A record that cannot be
    written never stops or undoes the action: the reply says `recorded: false`
    and only the type of the failure is logged.
  * Errors have one shape: {"error": {"code", "message", "retry_after"?}}.
    Messages are fixed plain sentences written here; text from an exception or
    from the request is never copied into one.
  * A request body that holds anything key-shaped is refused with a generic 400,
    except on `POST /v1/keys/{provider}`, the one place a key is meant to arrive.
  * Handlers learn who is calling only as a `Device` (see muse/home/auth.py).
  * Every route names the one scope it needs, and `dispatch` checks it against
    the device's role before the handler runs. A route with no scope is refused
    for everyone. A device that does not see everything (a `host`) gets only the
    jobs, cards and skill runs it made itself; anything else answers 404, the
    same as if it did not exist.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from wardmuse.home import API_VERSION
from wardmuse.home.auth import Device, Verifier
from wardmuse.home.cards import CardAlreadyAnswered, CardError, CardExpired, CardNotFound, CardStore
from wardmuse.home.cards import STATUSES as CARD_STATUSES
from wardmuse.home.skills import SkillRefused, SkillRuns
from wardmuse.home.state import HomeState

log = logging.getLogger("muse.home")

PREFIX = f"/v{API_VERSION}"
MAX_BODY_BYTES = 512 * 1024
MAX_PATH_CHARS = 512
MAX_KEY_CHARS = 512
MAX_JSON_DEPTH = 8
MAX_RECEIPTS_PAGE = 200  # records in one reply of GET /v1/receipts
DEFAULT_RECEIPTS_PAGE = 50
MAX_EXPORT_RECORDS = 20_000  # records in one export through the API; more is a job for `muse records export` on the home
MAX_RECORDS_PER_HOUR = 600  # records one device may add with POST /v1/receipts; they are kept for ever
# Action families only the home itself writes; POST /v1/receipts refuses them (record-format.md section 1.3).
HOME_ACTIONS = ("home", "card", "app", "key", "job", "skill", "mandate", "records")
# The one family a device that is not the person's own may report under. So an action that does not start with
# `host.` was done or witnessed by the home itself (or noted by the person's own device), never merely claimed by a bot.
HOST_FAMILY = "host"
# A setting from the key store whose NAME says it is a secret is never copied into the server's working environment.
_SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:$|_)", re.IGNORECASE)

# Every code a client can meet, with the status it comes with. The OpenAPI file lists the same set.
ERROR_CODES: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "card_already_answered": 409,
    "card_expired": 410,
    "length_required": 411,
    "payload_too_large": 413,
    "too_many_requests": 429,
    "internal": 500,
    "not_built": 501,
    "unavailable": 503,
}


class ApiError(Exception):
    """An error reply. `message` must be a fixed plain sentence: no secrets, prompts or exception text."""

    def __init__(self, code: str, message: str, *, retry_after: int | None = None, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = ERROR_CODES[code]
        self.message = message
        self.retry_after = retry_after
        self.headers = dict(headers or {})

    def payload(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            err["retry_after"] = int(self.retry_after)
        return {"error": err}


@dataclass(slots=True)
class Request:
    method: str
    params: dict[str, str]
    query: dict[str, str]
    body: dict[str, Any]
    device: Device


@dataclass(slots=True)
class Response:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str]
    route: str = "-"  # the route template, for the log; never the raw path


# ---------------------------------------------------------------- request checks


def looks_key_shaped(obj: Any) -> bool:
    from wardmuse.catalog import find_secretlike  # the detector the catalogue itself is checked with

    return bool(find_secretlike(obj))


def _depth_ok(obj: Any, left: int = MAX_JSON_DEPTH) -> bool:
    if isinstance(obj, dict):
        return left > 0 and all(_depth_ok(v, left - 1) for v in obj.values())
    if isinstance(obj, list):
        return left > 0 and all(_depth_ok(v, left - 1) for v in obj)
    return True


def parse_body(raw: bytes) -> dict[str, Any]:
    """A JSON object, or {} for an empty body. Anything else is a 400."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ApiError("invalid_request", "The request body is not valid JSON.") from None
    if not isinstance(data, dict) or not _depth_ok(data):
        raise ApiError("invalid_request", "The request body must be a JSON object.")
    return data


def _fields(body: Mapping[str, Any], allowed: Mapping[str, Any], required: tuple[str, ...] = ()) -> None:
    """`allowed` maps a field name to a type or tuple of types. Unknown or mistyped fields are refused."""
    for name in body:
        if name not in allowed:
            raise ApiError("invalid_request", "The request has a field this call does not take.")
    for name in required:
        if name not in body:
            raise ApiError("invalid_request", f"The request needs the field '{name}'.")
    for name, value in body.items():
        kinds = allowed[name]
        if isinstance(value, bool) and bool not in (kinds if isinstance(kinds, tuple) else (kinds,)):
            raise ApiError("invalid_request", f"The field '{name}' has the wrong type.")
        if not isinstance(value, kinds):
            raise ApiError("invalid_request", f"The field '{name}' has the wrong type.")


def _query(req: Request, *allowed: str) -> None:
    if set(req.query) - set(allowed):
        raise ApiError("invalid_request", "The request has a query parameter this call does not take.")


def _no_body(req: Request) -> None:
    if req.body:
        raise ApiError("invalid_request", "This call does not take a request body.")


_NONE = type(None)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- who did it, for a record


def actor_of(device: Device) -> str:
    """`person` for the person's own device, `helper` for anything acting for them. A caller cannot choose."""
    return "person" if device.role == "owner" else "helper"


def mandate_of(device: Device) -> str | None:
    """What a record names as the permission it ran under. A device token is today's stand-in for a
    permission token (muse/home/auth.py), so a helper's records name its device; the person's own name none."""
    return None if device.role == "owner" else f"device:{device.name}"


def _word(name: str) -> str:
    """A device or app name as it may appear in a record's summary: itself, unless it is shaped like a key."""
    from wardmuse.records.format import key_shaped

    return "(a long name)" if key_shaped(name) or len(name) > 64 else name


def _who(device: Device) -> dict[str, Any]:
    return {"actor": actor_of(device), "mandate": mandate_of(device)}


# ---------------------------------------------------------------- the home


class Home:
    """What the handlers act on: the real stores under MUSE_HOME.

    The fake home (muse/home/fake.py) is this same class pointed at a
    throw-away MUSE_HOME, with the snapshot, key storage and connection state
    replaced by canned data. So both speak exactly the same API.
    """

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        snapshot_fn: Callable[[], dict] | None = None,
        cards: CardStore | None = None,
        clock: Callable[[], float] = time.time,
        proxy_running: Callable[[], bool] | None = None,
    ) -> None:
        self.environ = environ
        self.clock = clock
        self.cards = cards if cards is not None else CardStore(environ, clock=clock)
        self.book = HomeState(environ, clock=clock)
        self._snapshot_fn = snapshot_fn
        self._proxy_running_fn = proxy_running
        self._verify_cache: tuple[Any, Any] | None = None
        self._record_lock = threading.Lock()
        self._record_times: dict[str, list[float]] = {}
        self.skills = SkillRuns(self)

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self.environ is None else self.environ

    def muse_home(self) -> Any:
        from wardmuse import paths

        return paths.home(self.environ)

    def settings_env(self) -> dict[str, str]:
        """The process environment plus the SETTINGS kept in the home's `.env` file, without the secrets in it.

        `muse status` builds its screen from the merged environment (`keys.export_env`: process, keychain
        and `.env`), which holds every provider key. The server must not carry those around, so this is
        the same merge with the secrets left out:
          * a setting from `.env` such as MUSE_DEFAULT_PRIVACY, MUSE_TZ, MUSE_DAILY_PAID_CAP_USD or a local
            model's address is kept, as `muse status` would see it;
          * a named key (anything in keys.KNOWN_KEYS), a name that says it is a secret, or a value shaped
            like one, is not copied. The keychain holds only named keys, so it is not read at all;
          * a value already in this process's own environment wins, as in `keys.export_env`.
        Whether a key is configured is still answered by asking the key store by name (yes or no, never
        a value). Built afresh on each call and never kept, so the server holds no stored key between
        requests."""
        from wardmuse import keys
        from wardmuse.catalog import looks_secret

        own = self.env
        out = dict(own)
        for name, value in keys._dotenv_items(self.environ).items():  # noqa: SLF001 - the file's lines; secrets are dropped just below
            if str(own.get(name, "")).strip():
                continue  # the process's own value, already in `out`
            if name in keys.KNOWN_KEYS or _SECRET_NAME.search(name) or looks_secret(value):
                continue
            out[name] = value
        return out

    def run_env(self) -> dict[str, str]:
        """What a skill run started by the home gets: the settings, and the skill runner's own two router tokens
        (Muse's own app tokens, kept in the key store). Never a provider key."""
        from wardmuse import keys
        from wardmuse.skills import app as skills_app

        tokens = set(skills_app.TOKEN_VARS.values())
        env = {name: value for name, value in self.settings_env().items() if name not in keys.KNOWN_KEYS or name in tokens}
        merged = keys.export_env(self.environ)  # holds every stored key for a moment: two names are taken, the rest dropped
        for name in tokens:
            if str(merged.get(name, "")).strip():
                env[name] = merged[name]
        return env

    def check_available(self) -> None:
        """Raise ApiError("unavailable") when the home cannot answer. The real home always can."""

    # ---- status

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot_fn is not None:
            return self._snapshot_fn()
        from wardmuse.status import build_snapshot

        # With the settings from `.env` (privacy default, time zone, paid cap, local model addresses), as
        # `muse status` shows them, and without the keys: see settings_env.
        return dict(build_snapshot(environ=self.settings_env()))

    def is_paused(self) -> bool:
        from wardmuse import queue

        return queue.is_paused(self.environ)

    def set_paused(self, paused: bool) -> None:
        from wardmuse import queue

        if paused:
            queue.set_paused(self.environ, now=self.clock())
        else:
            queue.clear_paused(self.environ)

    # ---- catalogue and keys

    def load_catalogue(self) -> Any:
        from wardmuse import catalog

        return catalog.load_catalog()

    def connected_ids(self, cat: Any) -> set[str]:
        from wardmuse import connect

        return connect.connected_ids(self.env, cat.providers)

    def save_key(self, name: str, value: str) -> tuple[str, list[str]]:
        """Store one named secret. Returns (backend, warnings); neither ever holds the value."""
        from wardmuse import keys

        result = keys.set_key(name, value, environ=self.environ)
        return result.backend, list(result.warnings)

    def key_configured(self, name: str) -> bool:
        from wardmuse import keys

        return keys.is_configured(name, self.env)

    def proxy_running(self) -> bool:
        if self._proxy_running_fn is not None:
            return bool(self._proxy_running_fn())
        try:
            from wardmuse import status

            return bool(status._reachable(status._proxy_url(self.env)))  # noqa: SLF001 - what the status screen says
        except Exception:
            return False

    def restart_command(self, running: bool) -> str:
        from wardmuse import connect

        return connect.proxy_command(self.env, running)

    # ---- queue

    def queue(self) -> Any:
        from wardmuse import queue

        return queue.Queue(self.environ, clock=self.clock)

    def default_route(self, allow_paid: bool) -> tuple[str | None, str]:
        from wardmuse import queue, rules

        task, _ = rules.load_rules(self.settings_env()).default_for(queue.app_for(allow_paid))
        return task, "auto"

    def default_privacy(self) -> str | None:
        return envget(self.settings_env(), "MUSE_DEFAULT_PRIVACY", "").strip().lower() or None

    # ---- records (muse/records): what was done in the person's name through this home

    def records(self) -> Any:
        from wardmuse.records.home import HomeRecords

        return HomeRecords(self.environ, clock=self.clock)

    def record(self, action: str, summary: str, **fields: Any) -> dict[str, Any] | None:
        """Append one record of something that has already happened. Never raises.

        Returns the record, or None when it could not be written (no `cryptography`, a full disk, a
        damaged file). The action itself stands either way; the caller reports `recorded: false`.
        Only the type of the failure is logged: never its message, a path or the summary."""
        try:
            return self.records().write(action, summary, **fields)
        except Exception as exc:  # ImportError included: records are optional, the action is not
            log.warning("records: no record was written for %s (%s)", action, type(exc).__name__)
            return None

    def record_pause(self, paused: bool) -> dict[str, Any] | None:
        """The record of a pause or resume: the same function `muse pause` and `muse resume` use."""
        try:
            from wardmuse.records.home import record_pause

            return record_pause(self.environ, paused, clock=self.clock, chain=self.records())
        except Exception as exc:
            log.warning("records: no record was written for home.%s (%s)", "pause" if paused else "resume", type(exc).__name__)
            return None

    def compiler(self) -> Any:
        """The permission compiler over this home (muse/compiler). The fake home hands out one that can be told to fail."""
        from wardmuse.compiler.core import Compiler

        return Compiler(self.environ, clock=self.clock, record=self.record)

    def records_state(self) -> dict[str, Any]:
        """Every well-formed record, whether the chain checks out, and the last seal. Raises what the store raises.

        The check walks the whole chain (one signature per record), so its answer is kept until the
        records file changes on disk."""
        from wardmuse.records import format as fmt
        from wardmuse.records.errors import RecordError
        from wardmuse.records.home import records_path

        chain = self.records()
        try:
            stat = os.stat(records_path(self.environ))
            stamp: Any = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            stamp = None
        records = chain.records()
        cached = self._verify_cache
        if cached is not None and stamp is not None and cached[0] == stamp:
            verify, sealed, device = cached[1]
        else:
            try:
                state = chain.verify()
                verify, cp, device = {"ok": True, "size": state["size"]}, state["checkpoint"], state["device"]
            except RecordError as exc:
                verify, cp, device = {"ok": False, "size": len(records), "kind": exc.kind, "index": exc.index}, None, chain.device()
                try:
                    cp = chain.store.last_checkpoint()
                except RecordError:
                    cp = None
            sealed = None if cp is None else {"size": cp["size"], "root": cp["root"], "time": _iso_ms(cp["time"])}
            self._verify_cache = (stamp, (verify, sealed, device))
        if not verify["ok"]:
            def well_formed(record: Any) -> bool:
                try:
                    fmt.check_record(record)
                except RecordError:
                    return False
                return True

            records = [r for r in records if well_formed(r)]  # show what can be shown
        return {"records": records, "verify": dict(verify), "sealed": sealed, "device": device}

    def allow_record_from(self, device: Device) -> int | None:
        """None when this device may add a record now; otherwise the seconds to wait. Records are kept for
        ever, so one device cannot add more than MAX_RECORDS_PER_HOUR of them in an hour."""
        now = self.clock()
        with self._record_lock:
            times = self._record_times.setdefault(device.name, [])
            times[:] = [t for t in times if t > now - 3600.0]
            if len(times) >= MAX_RECORDS_PER_HOUR:
                return max(1, int(times[0] + 3600.0 - now + 0.999))
            times.append(now)
            if len(self._record_times) > 256:
                for name in [n for n, ts in self._record_times.items() if not ts][:64]:
                    del self._record_times[name]
            return None


# ---------------------------------------------------------------- handlers: status, pause


def h_status(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _query(req)
    try:
        snap = dict(home.snapshot())
    except ApiError:
        raise
    except Exception as exc:
        log.warning("status: the snapshot could not be built (%s)", type(exc).__name__)
        raise ApiError("unavailable", "The home could not read its own status. Run `muse doctor` on the home to see why.",
                       retry_after=5) from None
    snap["paused"] = bool(home.is_paused())
    snap["api_version"] = API_VERSION
    return 200, snap


def h_pause(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _no_body(req)
    home.set_paused(True)
    return 200, {"paused": True, "recorded": home.record_pause(True) is not None}


def h_resume(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _no_body(req)
    was = bool(home.is_paused())
    home.set_paused(False)
    # As `muse resume`: a resume that changed nothing leaves no record.
    return 200, {"paused": False, "was_paused": was, "recorded": was and home.record_pause(False) is not None}


# ---------------------------------------------------------------- handlers: catalogue, connect, keys


def _steps(provider: Any) -> list[dict[str, Any]]:
    conn = provider.connection
    return [{"id": s.id, "title": dict(s.title), "detail": dict(s.detail)} for s in (conn.steps if conn else ())]


def _provider_entry(provider: Any, connected: bool) -> dict[str, Any]:
    from wardmuse import catalog

    conn = provider.connection
    head = provider.headline()
    return {
        "id": provider.id,
        "name": provider.name,
        "kind": provider.kind,
        "tier": provider.tier,
        "local": provider.local,
        "connected": connected,
        "one_off": provider.one_off,
        "key_env": provider.env,
        "also_env": list(provider.also_env),
        "kind_of_allowance": list(provider.kinds_of_allowance),
        "good_for": list(provider.good_for),
        "gain": None if head is None else {"window": head.window, "limit": head.limit, "unit": head.unit},
        "gain_text": {lang: catalog.gain_text(head, lang, local=provider.local) for lang in catalog.LANGS},
        "connection": None if conn is None else {
            "method": conn.method,
            "key_url": conn.key_url,
            "minutes_estimate": conn.minutes_estimate,
            "card_required": conn.card_required,
            "phone_required": conn.phone_required,
            "usable_on_phone": conn.usable_on_phone,
            "steps": len(conn.steps),
        },
        "checked_on": provider.checked_on,
        "confidence": provider.confidence,
        "source_urls": list(provider.source_urls),
    }


def h_catalogue(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import catalog, connect

    _query(req)
    cat = home.load_catalogue()
    connected = home.connected_ids(cat)
    suggestions = catalog.suggest_next(connected, home.env, providers=cat.providers)
    return 200, {
        "connected": sorted(connected),
        "providers": [_provider_entry(p, p.id in connected) for p in cat.providers],
        "suggestions": [s.to_dict() for s in suggestions],
        **connect.resources_payload(cat),
    }


def _provider(home: Home, provider_id: str) -> Any:
    cat = home.load_catalogue()
    provider = next((p for p in cat.providers if p.id == provider_id), None)
    if provider is None:
        raise ApiError("not_found", "The catalogue has no service with that name.")
    if provider.local or not provider.env:
        raise ApiError("invalid_request", "That service runs on this computer and needs no key. Use `muse connect local` on the home.")
    return provider


def h_connect(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """Start a guided connection: say what the person does next. The server opens no browser."""
    from wardmuse import keys

    _no_body(req)
    provider = _provider(home, req.params["provider"])
    conn = provider.connection
    cat = home.load_catalogue()
    return 200, {
        "provider": provider.id,
        "name": provider.name,
        "connected": provider.id in home.connected_ids(cat),
        "method": conn.method if conn else "paste_key",
        # v1: the home does not run a one-tap listener for a remote client, so every service ends with a pasted key.
        "finish_with": "paste_key",
        "open_url": conn.key_url if conn else None,
        "minutes_estimate": conn.minutes_estimate if conn else None,
        "card_required": conn.card_required if conn else "unknown",
        "phone_required": conn.phone_required if conn else "unknown",
        "steps": _steps(provider),
        "key": {"name": provider.env, "starts_with": keys.expected_prefix(str(provider.env))},
        "also": [{"name": n, "configured": home.key_configured(n)} for n in provider.also_env],
        "next": {"method": "POST", "path": f"{PREFIX}/keys/{provider.id}"},
    }


def h_keys(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """Store a key. The value is never echoed, logged, or kept in memory after this call."""
    from wardmuse import keys

    provider = _provider(home, req.params["provider"])
    _fields(req.body, {"key": str, "also": dict}, required=("key",))
    also = req.body.get("also") or {}
    for name, value in also.items():
        if name not in provider.also_env:
            raise ApiError("invalid_request", "The 'also' object names a setting this service does not use.")
        if not isinstance(value, str):
            raise ApiError("invalid_request", "Every 'also' value must be text.")
    to_store = [(str(provider.env), req.body["key"]), *also.items()]
    cleaned: list[tuple[str, str]] = []
    for name, value in to_store:
        try:
            text = keys.clean_value(value)
        except keys.KeysError:
            raise ApiError("invalid_request", "A value is empty or has a line break in it. Send only the key.") from None
        if len(text) > MAX_KEY_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in text):
            raise ApiError("invalid_request", "That does not look like a key. Send only the key.")
        cleaned.append((name, text))
    stored: list[str] = []
    warnings: list[str] = []
    backend = None
    for name, text in cleaned:
        try:
            backend, warned = home.save_key(name, text)
        except Exception as exc:  # KeysError, a keychain failure, a read-only file: never pass the text on
            log.warning("keys: could not store %s (%s)", name, type(exc).__name__)
            raise ApiError("unavailable", "The home could not store the key. Nothing was changed for this setting.") from None
        stored.append(name)
        warnings.extend(f"{name}: {w}" for w in warned)
    missing = [n for n in provider.also_env if n not in stored and not home.key_configured(n)]
    running = home.proxy_running()
    # The record names the service's id and nothing else: never the key, a part of it, or its hash.
    record = home.record("key.store", f"Stored a key for the service {_word(provider.id)}", egress="none", **_who(req.device))
    return 200, {
        "provider": provider.id,
        "stored": stored,
        "backend": backend,
        "warnings": warnings,
        "still_needed": missing,
        "restart": {"needed": True, "proxy_running": running, "command": home.restart_command(running)},
        "recorded": record is not None,
    }


# ---------------------------------------------------------------- handlers: jobs


def _job_error(exc: Exception) -> ApiError:
    text = str(exc)
    if text.startswith("no job") or "matches more than one" in text:
        return ApiError("not_found", "There is no such job.")
    if "is already" in text:
        return ApiError("conflict", "That job has already finished.")
    return ApiError("invalid_request", "The job was not accepted. Check the alias, task, privacy and limits.")


def h_jobs_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import queue

    _query(req, "status")
    statuses = [s for s in req.query.get("status", "").split(",") if s]
    if any(s not in queue.STATUSES for s in statuses):
        raise ApiError("invalid_request", f"status must be one or more of {', '.join(queue.STATUSES)}.")
    jobs = home.queue().list(statuses or None)
    if not req.device.sees_all:
        mine = home.book.jobs_of(req.device.name)
        jobs = [j for j in jobs if j.id in mine]
    return 200, {"jobs": [j.public() for j in jobs]}  # public(): metadata only, never the prompt or the result


def h_jobs_add(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps, queue

    body = req.body
    _fields(body, {
        "kind": str, "prompt": str, "prompts": list, "task": (str, _NONE), "alias": (str, _NONE), "privacy": (str, _NONE),
        "allow_paid": bool, "max_tokens": int, "max_attempts": int, "deadline": (str, _NONE),
    })
    kind = body.get("kind", "prompt")
    if kind == "prompt":
        prompt = body.get("prompt")
        if "prompts" in body or not isinstance(prompt, str) or not prompt.strip():
            raise ApiError("invalid_request", "A prompt job needs a non-empty 'prompt'.")
        if len(prompt) > queue.MAX_PROMPT_CHARS:
            raise ApiError("invalid_request", f"The prompt is longer than {queue.MAX_PROMPT_CHARS} characters.")
        payload = prompt
    elif kind == "batch":
        items = body.get("prompts")
        if "prompt" in body or not isinstance(items, list) or not items or len(items) > queue.MAX_BATCH_ITEMS:
            raise ApiError("invalid_request", f"A batch job needs 'prompts': a list of 1 to {queue.MAX_BATCH_ITEMS} prompts.")
        if any(not isinstance(i, str) or not i.strip() for i in items) or sum(len(i) for i in items) > queue.MAX_PROMPT_CHARS:
            raise ApiError("invalid_request", f"Every prompt must be non-empty text, {queue.MAX_PROMPT_CHARS} characters in all at most.")
        payload = json.dumps([i.strip() for i in items])
    else:
        raise ApiError("invalid_request", "kind must be prompt or batch.")
    privacy = body.get("privacy") or home.default_privacy()
    if privacy is not None and privacy not in apps.PRIVACY_CLASSES:
        raise ApiError("invalid_request", f"privacy must be one of {', '.join(apps.PRIVACY_CLASSES)}.")
    allow_paid = bool(body.get("allow_paid", False))
    task, alias = body.get("task"), body.get("alias")
    if task:
        task, alias = task.strip().lower(), alias or "auto"
    elif alias:
        task = None
    else:
        task, alias = home.default_route(allow_paid)
    deadline = None
    if body.get("deadline"):
        try:
            parsed = datetime.fromisoformat(body["deadline"].replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None:
            raise ApiError("invalid_request", "deadline must be a time with a zone, such as 2026-11-14T08:30:00Z.")
        deadline = parsed.timestamp()
        if deadline <= home.clock():
            raise ApiError("invalid_request", "The deadline must be in the future.")
    try:
        job = home.queue().add(
            kind=kind, payload=payload, alias=alias, privacy=privacy, deadline=deadline, allow_paid=allow_paid,
            max_attempts=body.get("max_attempts", queue.DEFAULT_MAX_ATTEMPTS),
            max_tokens=body.get("max_tokens", queue.DEFAULT_MAX_TOKENS), task=task,
        )
    except queue.QueueError as exc:
        raise _job_error(exc) from None
    try:
        home.book.set_job_owner(job.id, req.device.name)
    except Exception as exc:
        # Without the owner row a host could never see this job again; say so rather than leave an orphan running.
        log.error("jobs: the owner of a new job could not be written (%s)", type(exc).__name__)
        try:
            home.queue().cancel(job.id)
        except queue.QueueError:
            pass
        raise ApiError("unavailable", "The home could not keep track of the job, so it was not queued.") from None
    # The prompt stays on the home until the job runs; what a run then sends to a service is that run's own record.
    record = home.record("job.queue", "Queued a background job" + (" that may use paid services" if allow_paid else ""),
                         links={"job": job.id}, egress="none", **_who(req.device))
    return 201, {"job": job.public(), "recorded": record is not None}


def _job_id(home: Home, req: Request) -> str:
    """The job id of the path, when it is a job this device may see. Otherwise 404, as if there were none."""
    job_id = req.params["id"]
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ApiError("not_found", "There is no such job.")
    if not req.device.sees_all and home.book.job_owner(job_id) != req.device.name:
        raise ApiError("not_found", "There is no such job.")
    return job_id


def h_job_get(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import queue

    _query(req)
    try:
        job = home.queue().get(_job_id(home, req))
    except queue.QueueError as exc:
        raise _job_error(exc) from None
    return 200, {"job": job.public()}


def h_job_cancel(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import queue

    _no_body(req)
    try:
        job = home.queue().cancel(_job_id(home, req))
    except queue.QueueError as exc:
        raise _job_error(exc) from None
    record = home.record("job.cancel", "Cancelled a background job", links={"job": job.id}, egress="none", **_who(req.device))
    return 200, {"job": job.public(), "recorded": record is not None}


# ---------------------------------------------------------------- handlers: cards


def _card_error(exc: CardError) -> ApiError:
    if isinstance(exc, CardNotFound):
        return ApiError("not_found", "There is no such card.")
    if isinstance(exc, CardAlreadyAnswered):
        return ApiError("card_already_answered", "This card was already answered.")
    if isinstance(exc, CardExpired):
        return ApiError("card_expired", "This card expired before it was answered.")
    return ApiError("invalid_request", "The answer was not accepted. Send a decision of approve or decline.")


def h_cards_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _query(req, "status")
    statuses = [s for s in req.query.get("status", "").split(",") if s]
    if any(s not in CARD_STATUSES for s in statuses):
        raise ApiError("invalid_request", f"status must be one or more of {', '.join(CARD_STATUSES)}.")
    home.skills.sweep()  # cards that expired since the last look: the files that waited on them are thrown away
    if req.device.sees_all:
        return 200, {"cards": home.cards.list(statuses or None), "counts": home.cards.counts()}
    own = [c for c in home.cards.list(None, limit=1000) if _own_card(home, req.device, c)]
    counts = {s: sum(1 for c in own if c["status"] == s) for s in CARD_STATUSES}
    return 200, {"cards": [c for c in own if not statuses or c["status"] in statuses][:200], "counts": counts}


def _card_for(home: Home, req: Request) -> dict[str, Any]:
    """The card of the path, when this device may see it. Otherwise 404, as if there were none."""
    try:
        card = home.cards.get(req.params["id"])
    except CardError as exc:
        raise _card_error(exc) from None
    if not req.device.sees_all and not _own_card(home, req.device, card):
        raise ApiError("not_found", "There is no such card.")
    return card


def h_card_get(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _query(req)
    card = _card_for(home, req)
    if card["status"] != "pending":
        home.skills.sweep()  # e.g. it has just expired: the file that waited on it is thrown away
    return 200, {"card": card}


# A device that asks (POST /v1/cards) is named on the card as `job_ref: "device:<name>"`. That is how
# the per-device cap is counted and how the home knows not to let the asker answer its own question.
MAX_PENDING_PER_DEVICE = 20
_ask_lock = threading.Lock()


def asked_by(device: Device) -> str:
    return f"device:{device.name}"


def _own_card(home: Home, device: Device, card: Mapping[str, Any]) -> bool:
    """Is this a card the device made: one it asked for, or one a skill run it started asked for?"""
    ref = str(card.get("job_ref") or "")
    if ref == asked_by(device):
        return True
    if ref.startswith("skill-run:"):
        run = home.book.get_run(ref[len("skill-run:"):])
        return run is not None and run["device"] == device.name
    return False


# What each kind of card asks, for a record's summary. Never the card's own text: that is content.
CARD_WORDS = {
    "save_file": "save a file", "message": "send a message", "spend": "spend money", "grant_permission": "give a permission",
    "oneoff_credit": "use one-off credit", "send_private": "send private data to a service", "install_skill": "install a skill",
}


def h_card_ask(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """A device asks the person a question: one pending card, made by the store's own rules."""
    body = req.body
    _fields(body, {"kind": str, "title": str, "thing": str, "where": str, "under": str, "cost": (dict, _NONE),
                   "extra": (dict, _NONE), "ttl_s": (int, float)}, required=("kind", "title", "thing"))
    mine = asked_by(req.device)
    with _ask_lock:  # count and create together, so two requests at once cannot both slip under the cap
        waiting = sum(1 for c in home.cards.list("pending", limit=1000) if c["job_ref"] == mine)
        if waiting >= MAX_PENDING_PER_DEVICE:
            raise ApiError("conflict", f"This device already has {MAX_PENDING_PER_DEVICE} cards waiting for an answer. "
                                       "Wait for some to be answered or to expire.")
        try:
            card_id = home.cards.create(
                kind=body["kind"], title=body["title"], thing=body["thing"], where=body.get("where", ""),
                under=body.get("under", ""), cost=body.get("cost"), extra=body.get("extra"), ttl_s=body.get("ttl_s"),
                job_ref=mine,
            )
        except CardError:  # the store's sentence may repeat part of the request; this reply never does
            raise ApiError("invalid_request", "The card was not accepted. Check its kind, text lengths, cost and the "
                                              "extra fields for that kind (card.schema.json).") from None
    record = home.record("card.create", f"Asked whether to {CARD_WORDS.get(body['kind'], 'do something')}",
                         links={"card": card_id}, egress="none", **_who(req.device))
    return 201, {"card": home.cards.get(card_id), "recorded": record is not None}


def h_card_answer(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _fields(req.body, {"decision": str, "always": bool}, required=("decision",))
    decision, always = req.body["decision"], req.body.get("always", False)
    if decision not in ("approve", "decline"):
        raise ApiError("invalid_request", "decision must be approve or decline.")
    if always and decision != "approve":
        raise ApiError("invalid_request", "'always' can only be set when approving.")
    if _card_for(home, req)["job_ref"] == asked_by(req.device):
        raise ApiError("forbidden", "A device cannot answer a card it asked for itself. Answer it from another device.")
    try:
        card = home.cards.answer(req.params["id"], decision, req.device.name, always)
    except CardError as exc:
        home.skills.sweep()  # a card that ran out of time: what waited on it is thrown away now
        raise _card_error(exc) from None
    said = ("Approved" + (", and always from now on" if always else "")) if decision == "approve" else "Declined"
    record = home.record("card.answer", f"{said}: {CARD_WORDS.get(card['kind'], 'a question')} (answered on {_word(req.device.name)})",
                         outcome="ok" if decision == "approve" else "declined", links={"card": card["id"]}, egress="none",
                         **_who(req.device))
    # The first card -> action path: a file a skill proposed is written now, if this card stood for one and
    # was approved; thrown away if it was declined. A card that stands for nothing else is only answered.
    write = home.skills.settle_card(card)
    reply: dict[str, Any] = {"card": card, "recorded": record is not None}
    if write is not None:
        reply["write"] = write
    return 200, reply


# ---------------------------------------------------------------- handlers: apps

_APP_FIELDS = {
    "priority": str, "daily_requests": (int, _NONE), "daily_tokens": (int, _NONE), "allow_paid": (bool, _NONE),
    "default_privacy": (str, _NONE), "note": (str, _NONE),
}


def _app_record(app: Any) -> dict[str, Any]:
    return {
        "name": app.name, "priority": app.priority, "daily_requests": app.daily_requests, "daily_tokens": app.daily_tokens,
        "allow_paid": bool(app.allow_paid), "default_privacy": app.default_privacy, "note": app.note,
        "created_at": app.created_at or None,
    }  # never token_sha256


def _check_app_fields(body: Mapping[str, Any]) -> None:
    from wardmuse import apps

    if "priority" in body and body["priority"] not in apps.PRIORITIES:
        raise ApiError("invalid_request", f"priority must be one of {', '.join(apps.PRIORITIES)}.")
    for cap in ("daily_requests", "daily_tokens"):
        if body.get(cap) is not None and body[cap] < 0:
            raise ApiError("invalid_request", f"{cap} cannot be negative.")
    if body.get("default_privacy") is not None and body["default_privacy"] not in apps.PRIVACY_CLASSES:
        raise ApiError("invalid_request", f"default_privacy must be one of {', '.join(apps.PRIVACY_CLASSES)}.")
    note = body.get("note")
    if note is not None and (len(note) > 200 or "\n" in note or "\r" in note):
        raise ApiError("invalid_request", "note must be one line of at most 200 characters.")


def _app_error(exc: Exception) -> ApiError:
    text = str(exc)
    if "already exists" in text:
        return ApiError("conflict", "An app with that name already exists.")
    if text.startswith("no app named"):
        return ApiError("not_found", "There is no app with that name.")
    if "is reserved" in text:
        return ApiError("invalid_request", "That name is reserved. Choose another.")
    if "malformed" in text or text.startswith("cannot read"):
        log.warning("apps: the registry could not be read")
        return ApiError("unavailable", "The app list on the home could not be read. Run `muse doctor` on the home.")
    return ApiError("invalid_request", "The app settings were not accepted.")


def _app_name(req: Request) -> str:
    from wardmuse import apps

    name = req.params["name"]
    if not apps.NAME_RE.fullmatch(name):
        raise ApiError("not_found", "There is no app with that name.")
    return name


def h_apps_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps

    _query(req)
    registered = apps.load_apps(home.environ)
    return 200, {"apps": [_app_record(registered[name]) for name in sorted(registered)]}


def h_apps_add(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps

    body = req.body
    _fields(body, {"name": str, **_APP_FIELDS}, required=("name", "priority"))
    if not apps.NAME_RE.fullmatch(body["name"]):
        raise ApiError("invalid_request", "An app name is 1-40 lower-case letters, digits and '-', starting and ending with a letter or digit.")
    _check_app_fields(body)
    try:
        app, token = apps.add_app(
            body["name"], body["priority"], daily_requests=body.get("daily_requests"), daily_tokens=body.get("daily_tokens"),
            allow_paid=body.get("allow_paid"), default_privacy=body.get("default_privacy"), note=body.get("note"),
            environ=home.environ, now=home.clock(),
        )
    except apps.AppError as exc:
        raise _app_error(exc) from None
    return 201, {"app": _app_record(app), "token": token, "token_shown_once": True,
                 "recorded": _record_app(home, req, "app.create", "Added the app", app.name)}


def h_app_update(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps

    name = _app_name(req)
    _fields(req.body, _APP_FIELDS)
    if not req.body:
        raise ApiError("invalid_request", "Send at least one setting to change.")
    _check_app_fields(req.body)
    try:
        app = apps.update_app(name, home.environ, **req.body)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    return 200, {"app": _app_record(app), "recorded": _record_app(home, req, "app.update", "Changed the settings of the app", name)}


def h_app_remove(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps

    _no_body(req)
    name = _app_name(req)
    try:
        removed = apps.remove_app(name, home.environ)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    if not removed:
        raise ApiError("not_found", "There is no app with that name.")
    return 200, {"removed": True, "recorded": _record_app(home, req, "app.remove", "Removed the app", name)}


def h_app_rotate(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from wardmuse import apps

    _no_body(req)
    try:
        app, token = apps.rotate_token(_app_name(req), home.environ)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    return 200, {"app": _app_record(app), "token": token, "token_shown_once": True,
                 "recorded": _record_app(home, req, "app.rotate", "Replaced the token of the app", app.name)}


def _record_app(home: Home, req: Request, action: str, words: str, name: str) -> bool:
    """The record of a change to an app: its name, never its token or the token's hash."""
    return home.record(action, f"{words} {_word(name)}", egress="none", **_who(req.device)) is not None


# ---------------------------------------------------------------- handlers: records


_ACTION_FILTER = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,3}\Z")
_MANDATE_FILTER = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")  # the record format's alphabet for `mandate`


def _records_unavailable(exc: Exception) -> ApiError:
    log.warning("records: the records could not be used (%s)", type(exc).__name__)
    if isinstance(exc, ImportError):
        return ApiError("unavailable", "Records need the `cryptography` package on the home. Install it there with: "
                                       "pip install 'muse-stack[records]'")
    return ApiError("unavailable", "The home could not use its records file. Run `muse records verify` on the home.")


def _number(text: str, what: str, low: int, high: int) -> int:
    if not text.isascii() or not text.isdigit() or len(text) > 16 or not low <= int(text) <= high:
        raise ApiError("invalid_request", f"{what} must be a whole number from {low} to {high}.")
    return int(text)


def h_receipts_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """A page of the home's records, newest first, with whether the chain checks out and the last seal."""
    from wardmuse.records.commands import make_filter

    _query(req, "since", "action", "limit", "before", "left_device", "mandate")
    q = req.query
    limit = _number(q["limit"], "limit", 1, MAX_RECEIPTS_PAGE) if "limit" in q else DEFAULT_RECEIPTS_PAGE
    before = _number(q["before"], "before", 0, 2**53 - 1) if "before" in q else None
    since_ms = None
    if "since" in q:
        try:
            parsed = datetime.fromisoformat(q["since"].replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None or parsed.timestamp() < 0:
            raise ApiError("invalid_request", "since must be a time with a zone, such as 2026-11-01T00:00:00Z.")
        since_ms = int(parsed.timestamp() * 1000)
    action = q.get("action")
    if action is not None and not _ACTION_FILTER.match(action):
        raise ApiError("invalid_request", "action must be an action name such as files.write, or a family such as files.")
    if q.get("left_device", "false") not in ("true", "false"):
        raise ApiError("invalid_request", "left_device must be true or false.")
    left = q.get("left_device") == "true"
    mandate = q.get("mandate")
    if mandate is not None and not _MANDATE_FILTER.match(mandate):
        raise ApiError("invalid_request", "mandate must be the id of a permission, as the record format writes it.")
    try:
        state = home.records_state()
    except Exception as exc:
        raise _records_unavailable(exc) from None
    keep = make_filter(since_ms, action, False, left)
    matching = [r for r in reversed(state["records"]) if (before is None or r["seq"] < before) and (keep is None or keep(r))
                and (mandate is None or r.get("mandate") == mandate)]
    page = matching[:limit]
    return 200, {
        "records": page,
        "order": "newest_first",
        "next_before": page[-1]["seq"] if len(matching) > len(page) else None,
        "verify": state["verify"],
        "sealed": state["sealed"],
        "device": state["device"],
    }


def h_receipts_add(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """A device reports something it did. Who did it (`actor`, `mandate`) and when come from the home, never the caller."""
    from wardmuse.records import format as fmt
    from wardmuse.records.errors import RecordError

    body = req.body
    _fields(body, {"action": str, "summary": str, "outcome": str, "cost": (dict, _NONE), "links": dict, "egress": str,
                   "resource": (str, _NONE)}, required=("action", "summary"))
    family = body["action"].split(".")[0]
    if req.device.role == "owner":
        if family in HOME_ACTIONS or family == HOST_FAMILY:
            raise ApiError("invalid_request", "That action name is one only the home itself writes, or one kept for host "
                                              "bots. Use a name of your own, such as note.add.")
    elif family != HOST_FAMILY:
        # A helper's own report is always filed under `host.`, so it can never pass for something the home did or saw.
        raise ApiError("invalid_request", "A device that is not the person's own reports under names that start with "
                                          "host., such as host.files.write or host.message.send.")
    wait = home.allow_record_from(req.device)
    if wait is not None:
        raise ApiError("too_many_requests", "This device has added a great many records in the last hour. Wait before adding more.",
                       retry_after=wait)
    fields: dict[str, Any] = {"outcome": body.get("outcome", "ok"), "cost": body.get("cost"), "resource": body.get("resource"),
                              "egress": body.get("egress"), **_who(req.device)}
    if "links" in body:
        fields["links"] = body["links"]
    try:
        record = home.records().write(body["action"], body["summary"], **fields)
    except RecordError as exc:
        if exc.kind == "summary":
            raise ApiError("invalid_request", f"The summary was not accepted. It must be 1 to {fmt.MAX_SUMMARY_CHARS} characters "
                                              "on one line, with nothing shaped like a key in it.") from None
        if exc.kind == "malformed":
            raise ApiError("invalid_request", "The record was not accepted. Check the action name (dotted, lower-case), the "
                                              "outcome, the cost, the links and egress (record-format.md).") from None
        raise _records_unavailable(exc) from None
    except Exception as exc:
        raise _records_unavailable(exc) from None
    return 201, {"record": record, "hash": fmt.record_hash(record), "recorded": True}


def h_receipts_export(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """Seal, then hand over every record as one file anyone can check (record-format.md section 8).

    Never hands out a file that does not check: a chain that does not verify is refused, and the file is checked
    again before it leaves. The person chooses whether the summaries' text goes with it."""
    from wardmuse.records.errors import RecordError
    from wardmuse.records.seal import verify_export

    _fields(req.body, {"redact_summaries": bool})
    redact = bool(req.body.get("redact_summaries", False))
    try:
        count = len(home.records().records())
        if count == 0:
            raise ApiError("conflict", "There are no records to export yet.")
        if count > MAX_EXPORT_RECORDS:
            raise ApiError("invalid_request", f"There are more than {MAX_EXPORT_RECORDS:,} records. Export them on the home with "
                                              "`muse records export`.")
        obj = home.records().export(redact_summaries=redact)
        report = verify_export(obj)
    except ApiError:
        raise
    except RecordError as exc:
        log.warning("records: an export was refused because the records do not check out (%s)", exc.kind)
        raise ApiError("conflict", "The records do not check out, so nothing was exported. Run `muse records verify` "
                                   "on the home.") from None
    except Exception as exc:
        raise _records_unavailable(exc) from None
    day = datetime.fromtimestamp(home.clock(), tz=timezone.utc).strftime("%Y-%m-%d")
    record = home.record("records.export", "Exported the records" + (" without their summaries" if redact else ""),
                         egress="none", **_who(req.device))
    return 200, {"export": obj, "file_name": f"muse-records-{day}.json", "count": report["count"], "size": report["size"],
                 "redacted": report["redacted"], "device": report["device"], "recorded": record is not None}


# ---------------------------------------------------------------- handlers: skills


def h_skills_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """The skills on the home, what each asks for in plain words, and the folder names a run may be given."""
    from wardmuse.home import folders as folder_list
    from wardmuse.home import skills as skills_mod

    _query(req)
    try:
        listed = skills_mod.listing(home.settings_env())
    except Exception as exc:
        log.warning("skills: the skills could not be listed (%s)", type(exc).__name__)
        raise ApiError("unavailable", "The home could not read its skills. Run `muse skills list` on the home to see why.") from None
    names = []
    for entry in sorted(folder_list.load(home.environ).values(), key=lambda e: e["name"]):
        item: dict[str, Any] = {"name": entry["name"], "output": entry["output"]}
        if req.device.role == "owner":
            item["path"] = entry["path"]  # the person's own app shows where; a host bot learns the name only
        names.append(item)
    return 200, {**listed, "folders": names}


def h_skill_run(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    """Start a run. Answers at once with the run's id; `GET /v1/skills/runs/{id}` says how it went."""
    _fields(req.body, {"inputs": dict, "folders": list, "out": (str, _NONE), "budget": dict, "privacy": (str, _NONE),
                       "task": (str, _NONE)})
    if any(not isinstance(name, str) for name in req.body.get("folders") or []):
        raise ApiError("invalid_request", "folders must be a list of folder names.")
    try:
        run = home.skills.start(req.device, req.params["name"], req.body)
    except SkillRefused as refused:
        raise ApiError(refused.code, refused.message) from None
    return 202, {"run": run}


def h_skill_run_get(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _query(req)
    run_id = req.params["id"]
    run = home.skills.get(req.device, run_id) if re.fullmatch(r"[0-9a-f]{16}", run_id) else None
    if run is None:
        raise ApiError("not_found", "There is no such run.")
    return 200, {"run": run}


# ---------------------------------------------------------------- handlers: planned


def h_not_built(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    raise ApiError("not_built", "This part of the home is not built yet.")


# ---------------------------------------------------------------- the route table


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str  # template under /v1, e.g. "/v1/jobs/{id}"
    handler: Callable[[Home, Request], tuple[int, dict[str, Any]]]
    scope: str = ""  # one of muse.home.auth.SCOPES; a route without one is refused for every device
    takes_key: bool = False  # the one route whose body may hold a key
    planned: bool = False  # answers 501 until its module exists

    @property
    def pattern(self) -> re.Pattern[str]:
        return _compile(self.path)


_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
_PATTERNS: dict[str, re.Pattern[str]] = {}


def _compile(template: str) -> re.Pattern[str]:
    if template not in _PATTERNS:
        parts = [f"(?P<{p[1:-1]}>{_SEGMENT})" if p.startswith("{") else re.escape(p) for p in template.split("/")]
        _PATTERNS[template] = re.compile("/".join(parts))
    return _PATTERNS[template]


def _r(method: str, path: str, scope: str, handler: Callable[[Home, Request], tuple[int, dict[str, Any]]], **kw: Any) -> Route:
    return Route(method, PREFIX + path, handler, scope, **kw)


from wardmuse.home import mandates_api as _mandates  # noqa: E402 - permissions (C7); it imports nothing from this module at import time
from wardmuse.home import app_api as _app  # noqa: E402 - the app's routes (Ask, Chores, Services, Settings) live beside this file

# Method, path, the scope a device needs (muse/home/auth.py: which roles hold it), the handler.
ROUTES: tuple[Route, ...] = (
    _r("GET", "/status", "status.read", _app.h_status),  # h_status plus money in the person's currency and titles
    _r("GET", "/catalogue", "catalogue.read", h_catalogue),
    _r("POST", "/connect/{provider}", "connect.write", h_connect),
    _r("POST", "/keys/{provider}", "keys.write", _app.h_keys, takes_key=True),  # h_keys plus the default daily limit
    _r("GET", "/jobs", "jobs.read", h_jobs_list),
    _r("POST", "/jobs", "jobs.write", h_jobs_add),
    _r("GET", "/jobs/{id}", "jobs.read", h_job_get),
    _r("DELETE", "/jobs/{id}", "jobs.write", h_job_cancel),
    _r("GET", "/cards", "cards.read", h_cards_list),
    _r("POST", "/cards", "cards.ask", h_card_ask),
    _r("GET", "/cards/{id}", "cards.read", h_card_get),
    _r("POST", "/cards/{id}/answer", "cards.answer", h_card_answer),
    _r("POST", "/pause", "pause.write", h_pause),
    _r("POST", "/resume", "pause.write", h_resume),
    _r("GET", "/apps", "apps.read", h_apps_list),
    _r("POST", "/apps", "apps.write", h_apps_add),
    _r("PATCH", "/apps/{name}", "apps.write", h_app_update),
    _r("DELETE", "/apps/{name}", "apps.write", h_app_remove),
    _r("POST", "/apps/{name}/rotate", "apps.write", h_app_rotate),
    _r("GET", "/receipts", "receipts.read", h_receipts_list),
    _r("POST", "/receipts", "receipts.write", h_receipts_add),
    _r("POST", "/receipts/export", "receipts.export", h_receipts_export),
    _r("GET", "/skills", "skills.read", h_skills_list),
    _r("POST", "/skills/{name}/run", "skills.run", h_skill_run),
    _r("GET", "/skills/runs/{id}", "skills.read", h_skill_run_get),
    *_mandates.routes(_r),  # /mandates...; the last one is GET /mandates/artifacts, now the same list as GET /mandates
    *(_r(method, path, scope, handler, **extra) for method, path, scope, handler, extra in _app.ROUTE_SPECS),
)


def route_table() -> list[tuple[str, str]]:
    """(method, path template) for every route. The OpenAPI test compares this with the file."""
    return [(r.method, r.path) for r in ROUTES]


def route_roles(route: Route) -> list[str]:
    """The roles that may use a route, in the order of muse.home.auth.ROLES. The OpenAPI file states the same."""
    from wardmuse.home import auth

    return [role for role in auth.ROLES if auth.allows(role, route.scope)]


def check_scope(route: Route, device: Device) -> None:
    """The one place a role is checked. Deny by default: no scope on the route, or a role without it, is 403."""
    from wardmuse.home import auth

    if not auth.allows(getattr(device, "role", ""), route.scope):
        raise ApiError("forbidden", "This device is not allowed to make this call. The person can do it from their own "
                                    "app or the command line.")


def find_route(method: str, path: str) -> tuple[Route, dict[str, str]]:
    """The route for a method and path, or ApiError 404/405."""
    allowed: list[str] = []
    for route in ROUTES:
        match = route.pattern.fullmatch(path)
        if not match:
            continue
        if route.method == method:
            return route, match.groupdict()
        allowed.append(route.method)
    if allowed:
        raise ApiError("method_not_allowed", "That call does not take this method.", headers={"Allow": ", ".join(sorted(set(allowed)))})
    raise ApiError("not_found", "There is no such call.")


# ---------------------------------------------------------------- entry points for the transport


def bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value or " " in value:
        return None
    return value


def authenticate(verifier: Verifier, authorization: str | None) -> Device:
    """The calling device, or ApiError 401. The same answer for a missing, malformed or unknown token."""
    token = bearer_token(authorization)
    device = verifier.verify(token) if token else None
    if device is None:
        raise ApiError("unauthorized", "This device is not signed in to the home.", headers={"WWW-Authenticate": "Bearer"})
    return device


def error_response(err: ApiError, route: str = "-") -> Response:
    headers = dict(err.headers)
    if err.retry_after is not None:
        headers["Retry-After"] = str(int(err.retry_after))
    return Response(err.status, err.payload(), headers, route)


def dispatch(home: Home, device: Device, method: str, target: str, raw_body: bytes = b"") -> Response:
    """Run one authenticated request. Never raises; every failure becomes an error reply."""
    template = "-"
    try:
        if len(target) > MAX_PATH_CHARS:
            raise ApiError("not_found", "There is no such call.")
        parts = urlsplit(target)
        route, params = find_route(method, parts.path)
        template = route.path
        check_scope(route, device)
        home.check_available()
        if len(raw_body) > MAX_BODY_BYTES:
            raise ApiError("payload_too_large", "The request body is too large.")
        if method in ("GET", "DELETE") and raw_body.strip():
            raise ApiError("invalid_request", "This call does not take a request body.")
        body = parse_body(raw_body)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise ApiError("invalid_request", "A query parameter is given more than once.")
        if looks_key_shaped(query) or (not route.takes_key and looks_key_shaped(body)):
            # Generic on purpose: the reply must not say which field, or repeat any of it.
            raise ApiError("invalid_request", "The request was refused because part of it looks like a key or token. Keys go only to the keys call.")
        status, payload = route.handler(home, Request(method, params, query, body, device))
        return Response(status, payload, {}, template)
    except ApiError as err:
        return error_response(err, template)
    except Exception as exc:  # a bug or a broken store: say nothing about it to the caller
        log.error("%s %s failed (%s)", method, template, type(exc).__name__)
        return error_response(ApiError("internal", "The home hit a problem it did not expect."), template)
