"""The home control API, version 1: route table, handlers, and the `Home` behind them.

This module knows nothing about sockets. `muse/home/server.py` reads a request
off the wire, calls `authenticate` and then `dispatch`, and writes the reply.
The contract is `docs/spec/interfaces/home-api.v1.yaml`; a test walks `ROUTES`
and fails if the two disagree.

Rules every handler keeps:
  * A reply never holds a provider key, a prompt, a result, or exception text.
    The only secrets that ever leave are a NEW app token, once, on create/rotate.
  * Errors have one shape: {"error": {"code", "message", "retry_after"?}}.
    Messages are fixed plain sentences written here; text from an exception or
    from the request is never copied into one.
  * A request body that holds anything key-shaped is refused with a generic 400,
    except on `POST /v1/keys/{provider}`, the one place a key is meant to arrive.
  * Handlers learn who is calling only as a `Device` (see muse/home/auth.py).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from muse.home import API_VERSION
from muse.home.auth import Device, Verifier
from muse.home.cards import CardAlreadyAnswered, CardError, CardExpired, CardNotFound, CardStore
from muse.home.cards import STATUSES as CARD_STATUSES

log = logging.getLogger("muse.home")

PREFIX = f"/v{API_VERSION}"
MAX_BODY_BYTES = 512 * 1024
MAX_PATH_CHARS = 512
MAX_KEY_CHARS = 512
MAX_JSON_DEPTH = 8

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
    from muse.catalog import find_secretlike  # the detector the catalogue itself is checked with

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
        self._snapshot_fn = snapshot_fn
        self._proxy_running_fn = proxy_running

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self.environ is None else self.environ

    def check_available(self) -> None:
        """Raise ApiError("unavailable") when the home cannot answer. The real home always can."""

    # ---- status

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot_fn is not None:
            return self._snapshot_fn()
        from muse.status import build_snapshot

        return dict(build_snapshot(environ=self.environ))

    def is_paused(self) -> bool:
        from muse import queue

        return queue.is_paused(self.environ)

    def set_paused(self, paused: bool) -> None:
        from muse import queue

        if paused:
            queue.set_paused(self.environ, now=self.clock())
        else:
            queue.clear_paused(self.environ)

    # ---- catalogue and keys

    def load_catalogue(self) -> Any:
        from muse import catalog

        return catalog.load_catalog()

    def connected_ids(self, cat: Any) -> set[str]:
        from muse import connect

        return connect.connected_ids(self.env, cat.providers)

    def save_key(self, name: str, value: str) -> tuple[str, list[str]]:
        """Store one named secret. Returns (backend, warnings); neither ever holds the value."""
        from muse import keys

        result = keys.set_key(name, value, environ=self.environ)
        return result.backend, list(result.warnings)

    def key_configured(self, name: str) -> bool:
        from muse import keys

        return keys.is_configured(name, self.env)

    def proxy_running(self) -> bool:
        if self._proxy_running_fn is not None:
            return bool(self._proxy_running_fn())
        try:
            from muse import status

            return bool(status._reachable(status._proxy_url(self.env)))  # noqa: SLF001 - what the status screen says
        except Exception:
            return False

    def restart_command(self, running: bool) -> str:
        from muse import connect

        return connect.proxy_command(self.env, running)

    # ---- queue

    def queue(self) -> Any:
        from muse import queue

        return queue.Queue(self.environ, clock=self.clock)

    def default_route(self, allow_paid: bool) -> tuple[str | None, str]:
        from muse import queue, rules

        task, _ = rules.load_rules(self.env).default_for(queue.app_for(allow_paid))
        return task, "auto"

    def default_privacy(self) -> str | None:
        return self.env.get("MUSE_DEFAULT_PRIVACY", "").strip().lower() or None


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
    return 200, {"paused": True}


def h_resume(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _no_body(req)
    was = bool(home.is_paused())
    home.set_paused(False)
    return 200, {"paused": False, "was_paused": was}


# ---------------------------------------------------------------- handlers: catalogue, connect, keys


def _steps(provider: Any) -> list[dict[str, Any]]:
    conn = provider.connection
    return [{"id": s.id, "title": dict(s.title), "detail": dict(s.detail)} for s in (conn.steps if conn else ())]


def _provider_entry(provider: Any, connected: bool) -> dict[str, Any]:
    from muse import catalog

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
    from muse import catalog, connect

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
    from muse import keys

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
    from muse import keys

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
    return 200, {
        "provider": provider.id,
        "stored": stored,
        "backend": backend,
        "warnings": warnings,
        "still_needed": missing,
        "restart": {"needed": True, "proxy_running": running, "command": home.restart_command(running)},
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
    from muse import queue

    _query(req, "status")
    statuses = [s for s in req.query.get("status", "").split(",") if s]
    if any(s not in queue.STATUSES for s in statuses):
        raise ApiError("invalid_request", f"status must be one or more of {', '.join(queue.STATUSES)}.")
    jobs = home.queue().list(statuses or None)
    return 200, {"jobs": [j.public() for j in jobs]}  # public(): metadata only, never the prompt or the result


def h_jobs_add(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps, queue

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
    return 201, {"job": job.public()}


def _job_id(req: Request) -> str:
    job_id = req.params["id"]
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ApiError("not_found", "There is no such job.")
    return job_id


def h_job_get(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import queue

    _query(req)
    try:
        job = home.queue().get(_job_id(req))
    except queue.QueueError as exc:
        raise _job_error(exc) from None
    return 200, {"job": job.public()}


def h_job_cancel(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import queue

    _no_body(req)
    try:
        job = home.queue().cancel(_job_id(req))
    except queue.QueueError as exc:
        raise _job_error(exc) from None
    return 200, {"job": job.public()}


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
    cards = home.cards.list(statuses or None)
    return 200, {"cards": cards, "counts": home.cards.counts()}


def h_card_get(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _query(req)
    try:
        return 200, {"card": home.cards.get(req.params["id"])}
    except CardError as exc:
        raise _card_error(exc) from None


# A device that asks (POST /v1/cards) is named on the card as `job_ref: "device:<name>"`. That is how
# the per-device cap is counted and how the home knows not to let the asker answer its own question.
MAX_PENDING_PER_DEVICE = 20
_ask_lock = threading.Lock()


def asked_by(device: Device) -> str:
    return f"device:{device.name}"


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
    return 201, {"card": home.cards.get(card_id)}


def h_card_answer(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    _fields(req.body, {"decision": str, "always": bool}, required=("decision",))
    decision, always = req.body["decision"], req.body.get("always", False)
    if decision not in ("approve", "decline"):
        raise ApiError("invalid_request", "decision must be approve or decline.")
    if always and decision != "approve":
        raise ApiError("invalid_request", "'always' can only be set when approving.")
    try:
        if home.cards.get(req.params["id"])["job_ref"] == asked_by(req.device):
            raise ApiError("forbidden", "A device cannot answer a card it asked for itself. Answer it from another device.")
    except CardError as exc:
        raise _card_error(exc) from None
    try:
        card = home.cards.answer(req.params["id"], decision, req.device.name, always)
    except CardError as exc:
        raise _card_error(exc) from None
    return 200, {"card": card}


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
    from muse import apps

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
    from muse import apps

    name = req.params["name"]
    if not apps.NAME_RE.fullmatch(name):
        raise ApiError("not_found", "There is no app with that name.")
    return name


def h_apps_list(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps

    _query(req)
    registered = apps.load_apps(home.environ)
    return 200, {"apps": [_app_record(registered[name]) for name in sorted(registered)]}


def h_apps_add(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps

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
    return 201, {"app": _app_record(app), "token": token, "token_shown_once": True}


def h_app_update(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps

    name = _app_name(req)
    _fields(req.body, _APP_FIELDS)
    if not req.body:
        raise ApiError("invalid_request", "Send at least one setting to change.")
    _check_app_fields(req.body)
    try:
        app = apps.update_app(name, home.environ, **req.body)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    return 200, {"app": _app_record(app)}


def h_app_remove(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps

    _no_body(req)
    try:
        removed = apps.remove_app(_app_name(req), home.environ)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    if not removed:
        raise ApiError("not_found", "There is no app with that name.")
    return 200, {"removed": True}


def h_app_rotate(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    from muse import apps

    _no_body(req)
    try:
        app, token = apps.rotate_token(_app_name(req), home.environ)
    except apps.AppError as exc:
        raise _app_error(exc) from None
    return 200, {"app": _app_record(app), "token": token, "token_shown_once": True}


# ---------------------------------------------------------------- handlers: planned


def h_not_built(home: Home, req: Request) -> tuple[int, dict[str, Any]]:
    raise ApiError("not_built", "This part of the home is not built yet.")


# ---------------------------------------------------------------- the route table


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    path: str  # template under /v1, e.g. "/v1/jobs/{id}"
    handler: Callable[[Home, Request], tuple[int, dict[str, Any]]]
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


def _r(method: str, path: str, handler: Callable[[Home, Request], tuple[int, dict[str, Any]]], **kw: Any) -> Route:
    return Route(method, PREFIX + path, handler, **kw)


ROUTES: tuple[Route, ...] = (
    _r("GET", "/status", h_status),
    _r("GET", "/catalogue", h_catalogue),
    _r("POST", "/connect/{provider}", h_connect),
    _r("POST", "/keys/{provider}", h_keys, takes_key=True),
    _r("GET", "/jobs", h_jobs_list),
    _r("POST", "/jobs", h_jobs_add),
    _r("GET", "/jobs/{id}", h_job_get),
    _r("DELETE", "/jobs/{id}", h_job_cancel),
    _r("GET", "/cards", h_cards_list),
    _r("POST", "/cards", h_card_ask),
    _r("GET", "/cards/{id}", h_card_get),
    _r("POST", "/cards/{id}/answer", h_card_answer),
    _r("POST", "/pause", h_pause),
    _r("POST", "/resume", h_resume),
    _r("GET", "/apps", h_apps_list),
    _r("POST", "/apps", h_apps_add),
    _r("PATCH", "/apps/{name}", h_app_update),
    _r("DELETE", "/apps/{name}", h_app_remove),
    _r("POST", "/apps/{name}/rotate", h_app_rotate),
    _r("GET", "/receipts", h_not_built, planned=True),
    _r("GET", "/mandates/artifacts", h_not_built, planned=True),
)


def route_table() -> list[tuple[str, str]]:
    """(method, path template) for every route. The OpenAPI test compares this with the file."""
    return [(r.method, r.path) for r in ROUTES]


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
