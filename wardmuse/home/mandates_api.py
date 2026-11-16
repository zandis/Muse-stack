"""Permissions through the home API (the Permissions screen, ux section 4.6, and the grant card, section 5).

    GET  /v1/mandates                 every permission as a sentence, with a mark per enforcing system
    GET  /v1/mandates/artifacts       the same list (kept for clients written against the earlier name)
    POST /v1/mandates/draft           structured fields in, a draft and its sentence back; nothing is issued
    POST /v1/mandates                 issue a draft. From the person's own device it is issued at once. From a host
                                      bot's device it only ASKS: a `grant_permission` card is made, and the
                                      permission is issued when the person approves that card
    POST /v1/mandates/{id}/stop       stop one everywhere; says how each enforcing system ended up
    GET  /v1/mandates/declined        requests the person declined in the last week (so a helper does not ask again)
    POST /v1/mandates/check           what this device's own permission says about an action (the tools server's
                                      `check_permission`)

Who is "the person". Issuing and stopping need the person to have confirmed. In v1 that is: the call comes
from a device with the `owner` role. It is one function, `person_confirmed`, so a passkey check (a later
package) replaces it without touching a handler.

Nothing here returns a permission token, the root key, or anything read from them but the plain facts.
The one secret that can leave is a NEW app key, once, when the issued permission makes a router app
(the same rule as `POST /v1/apps`).

This module imports nothing from wardmuse.home.api at import time (api.py imports THIS module to build its
route table); handlers import what they need when they run.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

log = logging.getLogger("muse.home")

# (method, path under /v1, scope, handler name). muse/home/api.py turns these into its Route entries.
ROUTE_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/mandates", "mandates.read", "h_list"),
    ("POST", "/mandates", "mandates.request", "h_issue"),
    ("POST", "/mandates/draft", "mandates.request", "h_draft"),
    ("POST", "/mandates/check", "mandates.request", "h_check"),
    ("GET", "/mandates/declined", "mandates.request", "h_declined"),
    ("POST", "/mandates/{id}/stop", "mandates.write", "h_stop"),
    ("GET", "/mandates/artifacts", "mandates.read", "h_list"),
)

DECLINED_WINDOW_S = 7 * 86400
MAX_DAYS = 3660
DEFAULT_DAYS = 30
LANGS = ("en", "zh-TW")
CHECK_ACTIONS = ("read", "list", "search", "write", "delete", "send", "run", "install", "spend", "other")
NEEDS_LIBRARY = ("Permissions need the `biscuit-python` package on the home. Install it there with: "
                 "pip install 'muse-stack[permissions]'")
_request_lock = threading.Lock()


def routes(make: Any) -> tuple[Any, ...]:
    """The Route entries, built with api.py's own constructor `_r(method, path, scope, handler)`."""
    return tuple(make(method, path, scope, globals()[name]) for method, path, scope, name in ROUTE_SPECS)


# ---------------------------------------------------------------- who may issue


def person_confirmed(home: Any, req: Any) -> bool:
    """Did the person themselves confirm this request? v1: it came from a device with the `owner` role.

    The ONE place this is decided. A passkey (WebAuthn) assertion carried by the request replaces the body of
    this function; handlers do not change."""
    return getattr(req.device, "role", "") == "owner"


# ---------------------------------------------------------------- helpers


def _api() -> Any:
    from wardmuse.home import api

    return api


def _compiler(home: Any) -> Any:
    from wardmuse import permissions as P

    if not P.available():
        raise _api().ApiError("unavailable", NEEDS_LIBRARY)
    return home.compiler()


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(text: Any, what: str) -> int:
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise _api().ApiError("invalid_request", f"{what} must be a time with a zone, such as 2026-12-31T00:00:00Z.")
    return int(parsed.timestamp())


def _lang(value: Any) -> str:
    if value is None:
        return "en"
    if value not in LANGS:
        raise _api().ApiError("invalid_request", "lang must be en or zh-TW.")
    return value


def _refused(exc: Any) -> Any:
    """A draft the permission rules do not accept. The code is one of a fixed set of words, never request text."""
    code = exc.code if isinstance(exc.code, str) and exc.code.replace("_", "").isalpha() else "invalid"
    if code == "library_missing":
        return _api().ApiError("unavailable", NEEDS_LIBRARY)
    return _api().ApiError("invalid_request", f"The permission was not accepted ({code}). Check the holder, actions, resources, "
                                              "limits, dates and the confirmation rule.")


def _fields_of(body: Mapping[str, Any], now: int, zone: str) -> dict[str, Any]:
    """The request's structured fields as the plain dict a draft is stored as (times in seconds)."""
    A = _api()

    def items(name: str, kind: type) -> list[Any]:
        value = body.get(name) or []
        if len(value) > 64 or not all(isinstance(v, kind) for v in value):
            raise A.ApiError("invalid_request", f"'{name}' must be a list of at most 64 entries of the right kind.")
        return list(value)

    resources = []
    for item in items("resources", dict):
        if set(item) != {"kind", "value"} or not all(isinstance(item[k], str) for k in item):
            raise A.ApiError("invalid_request", "Every resource is an object with a kind and a value, both text.")
        resources.append([item["kind"], item["value"]])
    limits = []
    for item in items("limits", dict):
        if set(item) - {"kind", "unit", "decimals", "period", "amount"} or not {"kind", "period", "amount"} <= set(item):
            raise A.ApiError("invalid_request", "Every limit has a kind, a period and an amount, and may have a unit and decimals.")
        amount, decimals = item["amount"], item.get("decimals", 0)
        if any(isinstance(n, bool) or not isinstance(n, int) for n in (amount, decimals)) or not isinstance(item.get("unit", ""), str) \
                or not isinstance(item["kind"], str) or not isinstance(item["period"], str):
            raise A.ApiError("invalid_request", "A limit's amount and decimals are whole numbers; its kind, unit and period are text.")
        limits.append([item["kind"], item.get("unit", ""), decimals, item["period"], amount])
    confirm = body.get("confirm", "each_write")
    if isinstance(confirm, str):
        confirm = [confirm, "", 0, 0]
    elif isinstance(confirm, dict) and set(confirm) <= {"rule", "unit", "decimals", "amount"} and isinstance(confirm.get("rule"), str) \
            and isinstance(confirm.get("unit", ""), str) \
            and all(isinstance(confirm.get(k, 0), int) and not isinstance(confirm.get(k, 0), bool) for k in ("decimals", "amount")):
        confirm = [confirm["rule"], confirm.get("unit", ""), confirm.get("decimals", 0), confirm.get("amount", 0)]
    else:
        raise A.ApiError("invalid_request", "confirm is each_write, never, or an object with rule over_amount, unit, decimals and amount.")
    start = _time(body["not_before"], "not_before") if body.get("not_before") else now
    if body.get("not_after"):
        if "days" in body:
            raise A.ApiError("invalid_request", "Give not_after or days, not both.")
        end = _time(body["not_after"], "not_after")
    else:
        days = body.get("days", DEFAULT_DAYS)
        if not 1 <= days <= MAX_DAYS:
            raise A.ApiError("invalid_request", f"days must be from 1 to {MAX_DAYS}.")
        end = start + days * 86400
    return {
        "holder": body["holder"], "actions": items("actions", str), "routine": items("routine", str), "resources": resources,
        "limits": limits, "purpose_text": body.get("purpose", ""), "purpose_code": body.get("purpose_code", ""),
        "issued_at": now, "not_before": start, "not_after": end, "confirm": confirm, "privacy": body.get("privacy", "private"),
        "period_zone": body.get("period_zone") or zone,
    }


def draft_from(fields: Mapping[str, Any], issuer: str) -> Any:
    """The validated Draft for stored fields. Raises muse.permissions.PermissionError when the rules refuse it."""
    from wardmuse import permissions as P

    return P.propose_from_fields(P.Draft(
        issuer=issuer, holder=fields["holder"], actions=tuple(fields["actions"]), routine=tuple(fields["routine"]),
        resources=tuple(P.Resource(k, v) for k, v in fields["resources"]),
        limits=tuple(P.Limit(k, u, d, p, a) for k, u, d, p, a in fields["limits"]),
        purpose_text=fields["purpose_text"], purpose_code=fields["purpose_code"], issued_at=fields["issued_at"],
        not_before=fields["not_before"], not_after=fields["not_after"], confirm=P.Confirm(*fields["confirm"]),
        privacy=fields["privacy"], period_zone=fields["period_zone"]))


def _unsigned(draft: Any) -> Any:
    """A draft in the shape `describe` and `plan` read. It holds no token: nothing was signed."""
    from wardmuse import permissions as P

    return P.Mandate(biscuit=None, id=P.mandate_id(draft), draft=draft)


def _words(mandate: Any, lang: str) -> dict[str, Any]:
    from wardmuse import permissions as P

    text = P.describe(mandate, lang)
    return {"sentence": text.sentence, "lines": list(text.lines)}


def fingerprint(fields: Mapping[str, Any]) -> str:
    """What was asked for, whatever its dates: the holder, the actions and the resources."""
    basis = json.dumps([fields["holder"], sorted(fields["actions"]), sorted(map(list, fields["resources"]))], sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def facts(draft: Any) -> dict[str, Any]:
    """What a permission allows, as plain values, so an app can write its own sentence in the person's words."""
    return {
        "actions": list(draft.actions),
        "resources": [{"kind": r.kind, "value": r.value} for r in draft.resources],
        "limits": [{"kind": l.kind, "unit": l.unit, "decimals": l.decimals, "period": l.period, "amount": l.amount}
                   for l in draft.limits],
        "privacy": draft.privacy,
    }


def entry(compiler: Any, mandate: Any, lang: str) -> dict[str, Any]:
    """One permission as the Permissions screen shows it. No token, no key."""
    return {
        "id": mandate.id, "name": mandate.id[3:15], "holder": mandate.draft.holder, "state": compiler.state_of(mandate),
        **_words(mandate, lang), **facts(mandate.draft), "issued_at": _iso(mandate.draft.issued_at),
        "not_before": _iso(mandate.not_before()), "not_after": _iso(mandate.not_after()), "targets": compiler.status(mandate.id),
    }


def _issued(compiler: Any, mandate: Any, applied: Any, lang: str) -> dict[str, Any]:
    out: dict[str, Any] = {"mandate": entry(compiler, mandate, lang),
                           "recorded": applied.targets.get("records", {}).get("state") == "active"}
    if applied.app_name:
        out["app"] = applied.app_name
    if applied.app_token:
        out["token"], out["token_shown_once"] = applied.app_token, True
    return out


# ---------------------------------------------------------------- handlers


def h_list(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    A = _api()
    A._query(req, "lang")  # noqa: SLF001
    lang = _lang(req.query.get("lang"))
    compiler = _compiler(home)
    settle_ended(home)
    listed = [entry(compiler, m, lang) for m in reversed(compiler.mandates())]
    return 200, {"mandates": listed, "enforcement": enforcement(home)}


def enforcement(home: Any) -> dict[str, Any]:
    """What is enforced now and under which setting, for the screen's small print."""
    from wardmuse.compiler import guard

    try:
        mode = guard.normalise_mode(home.settings_env().get(guard.ENV_MODE))
    except ValueError:
        mode = guard.DEFAULT_MODE
    return {"skills": mode, "skills_not_shipped_with_muse": "enforce", "router": "enforce",
            "not_built": ["payments", "connectors", "health"]}


def h_draft(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    A = _api()
    body = req.body
    A._fields(body, {  # noqa: SLF001
        "holder": str, "actions": list, "resources": list, "limits": list, "routine": list, "purpose": str, "purpose_code": str,
        "days": int, "not_before": str, "not_after": str, "confirm": (str, dict), "privacy": str, "period_zone": str, "lang": str,
    }, required=("holder", "actions", "resources"))
    lang = _lang(body.get("lang"))
    compiler = _compiler(home)
    from wardmuse import permissions as P
    from wardmuse.compiler.hosts import holder_for
    from wardmuse.compiler import table as table_mod
    from wardmuse.compiler.plan import plan

    if not person_confirmed(home, req) and body["holder"] != holder_for(req.device.name):
        raise A.ApiError("forbidden", "A device that is not the person's own can ask for a permission only for itself.")
    zone = str(envget(home.settings_env(), "MUSE_TZ", "") or "").strip() or "UTC"
    fields = _fields_of(body, compiler.now(), zone)
    try:
        draft = draft_from(fields, compiler.ensure_root())
    except P.PermissionError as exc:
        raise _refused(exc) from None
    shown = _unsigned(draft)
    draft_id = compiler.table.add_draft(req.device.name, fields)
    targets = [{"target": s.target, "state": "not_built" if s.status == "not_built" else "planned",
                "reason": s.reason if s.status != "planned" else ""} for s in plan(shown)]
    seen: dict[str, dict[str, Any]] = {}
    for t in targets:
        seen.setdefault(t["target"], t)
    return 201, {"draft": {"id": draft_id, "holder": draft.holder, **_words(shown, lang), "not_before": _iso(draft.not_before),
                           "not_after": _iso(draft.not_after), "targets": list(seen.values()), "issued": False,
                           "valid_for_s": int(table_mod.DRAFT_TTL_S)}}


def h_issue(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    A = _api()
    A._fields(req.body, {"draft": str, "lang": str}, required=("draft",))  # noqa: SLF001
    lang = _lang(req.body.get("lang"))
    compiler = _compiler(home)
    from wardmuse import permissions as P

    confirmed = person_confirmed(home, req)
    fields = compiler.table.take_draft(req.body["draft"], req.device.name, remove=False)
    if fields is None:
        raise A.ApiError("not_found", "There is no such draft, or it is more than an hour old. Make it again.")
    try:
        draft = draft_from(fields, compiler.ensure_root())
    except P.PermissionError as exc:
        raise _refused(exc) from None
    if confirmed:
        try:
            mandate, applied = compiler.issue(draft, actor="person")
        except P.PermissionError as exc:
            log.warning("mandates: a permission could not be issued (%s)", exc.code)
            raise A.ApiError("unavailable", "The home could not sign the permission. Run `muse permissions status` on the home.") from None
        compiler.table.take_draft(req.body["draft"], req.device.name, remove=True)
        return 201, _issued(compiler, mandate, applied, lang)
    return _request(home, req, compiler, fields, draft)


def _request(home: Any, req: Any, compiler: Any, fields: Mapping[str, Any], draft: Any) -> tuple[int, dict[str, Any]]:
    """A device that is not the person's asks: one `grant_permission` card. Nothing is issued here."""
    A = _api()
    from wardmuse.home.cards import CardError

    settle_ended(home)
    mark = fingerprint(fields)
    mine = A.asked_by(req.device)
    shown = _unsigned(draft)
    words = _words(shown, "en")
    with _request_lock:
        since = home.clock() - DECLINED_WINDOW_S
        for old in compiler.table.requests(device=req.device.name, since=since):
            if old["fingerprint"] != mark:
                continue
            if old["state"] == "declined":
                raise A.ApiError("conflict", "The person declined this same request in the last week. Do not ask again yet.")
            if old["state"] in ("pending", "settling"):
                raise A.ApiError("conflict", "This same request is already waiting for the person's answer.")
        waiting = sum(1 for c in home.cards.list("pending", limit=1000) if c["job_ref"] == mine)
        if waiting >= A.MAX_PENDING_PER_DEVICE:
            raise A.ApiError("conflict", f"This device already has {A.MAX_PENDING_PER_DEVICE} cards waiting for an answer. "
                                         "Wait for some to be answered or to expire.")
        try:
            card_id = home.cards.create(
                kind="grant_permission", title="Give a permission", thing=words["sentence"][:200],
                under=f"Asked for by the device {A._word(req.device.name)}",  # noqa: SLF001
                extra={"sentence": words["sentence"][:500], "limits": [line[:200] for line in words["lines"]][:20],
                       "ends_at": _iso(draft.not_after)},
                job_ref=mine)
        except CardError:
            raise A.ApiError("invalid_request", "The request could not be put to the person as a card (its text is too long, "
                                                "or holds something shaped like a key).") from None
        compiler.table.add_request(card_id, req.device.name, mark, fields)
    record = home.record("card.create", "Asked whether to give a permission", links={"card": card_id}, egress="none",
                         **A._who(req.device))  # noqa: SLF001
    return 202, {"requested": True, "issued": False, "card": home.cards.get(card_id), "recorded": record is not None}


def settle_card(home: Any, card: Mapping[str, Any]) -> dict[str, Any] | None:
    """Act on an answered or expired `grant_permission` card that stands for a request. None when it stands for none.

    Approved: the permission is issued and compiled, exactly as if the person had issued the draft themselves.
    Returns {"state": issued | declined | expired | failed, ...}; with `issued`, the same members as `POST /v1/mandates`
    (a new app key, if one was made, is in `token`, once). Safe to call any number of times: the request is claimed first."""
    from wardmuse import permissions as P

    status = card.get("status")
    if status == "pending" or card.get("kind") != "grant_permission":
        return None
    try:
        compiler = _compiler(home)
    except Exception:
        compiler = None
    from wardmuse.compiler.table import Table

    table = compiler.table if compiler is not None else Table(home.environ, home.clock)
    seen = table.request(card["id"])
    if seen is None:
        return None
    claimed = table.claim_request(card["id"])
    if claimed is None:
        return {"state": seen["state"], "mandate": seen["mandate_id"]}
    if status != "approved":
        state = "declined" if status == "declined" else "expired"
        table.settle_request(card["id"], state)
        return {"state": state, "mandate": None}
    if compiler is None:
        table.settle_request(card["id"], "failed")
        return {"state": "failed", "mandate": None, "reason": NEEDS_LIBRARY}
    try:
        draft = draft_from(claimed["fields"], compiler.ensure_root())
        mandate, applied = compiler.issue(draft, actor="person")
    except Exception as exc:
        log.warning("mandates: an approved request could not be issued (%s)", exc.code if isinstance(exc, P.PermissionError) else type(exc).__name__)
        table.settle_request(card["id"], "failed")
        return {"state": "failed", "mandate": None,
                "reason": "The permission could not be issued although it was approved. Make the request again."}
    table.settle_request(card["id"], "issued", mandate.id)
    return {"state": "issued", **_issued(compiler, mandate, applied, "en")}


def settle_ended(home: Any) -> int:
    """Requests whose card ended some other way than an answer through this API (it expired). Never raises."""
    from wardmuse.compiler.table import Table
    from wardmuse.home.cards import CardError

    settled = 0
    try:
        for waiting in Table(home.environ, home.clock).requests(state="pending"):
            try:
                card = home.cards.get(waiting["card_id"])
            except CardError:
                card = {"id": waiting["card_id"], "status": "expired", "kind": "grant_permission"}
            if card["status"] != "pending" and settle_card(home, card) is not None:
                settled += 1
    except Exception as exc:
        log.warning("mandates: waiting requests could not be looked at (%s)", type(exc).__name__)
    return settled


def h_declined(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from wardmuse.compiler.table import Table

    _api()._query(req, "lang")  # noqa: SLF001
    lang = _lang(req.query.get("lang"))
    settle_ended(home)
    device = None if req.device.sees_all else req.device.name
    rows = Table(home.environ, home.clock).requests(state="declined", device=device, since=home.clock() - DECLINED_WINDOW_S)
    return 200, {"window_days": DECLINED_WINDOW_S // 86400, "declined": [{
        "card_id": r["card_id"], "device": r["device"], "holder": r["fields"]["holder"], "actions": sorted(r["fields"]["actions"]),
        "resources": [{"kind": k, "value": v} for k, v in sorted(map(tuple, r["fields"]["resources"]))],
        "sentence": _declined_sentence(home, r["fields"], lang),
        "declined_at": _iso(int(r["settled_at"] or r["created_at"])),
        "ask_again_after": _iso(int((r["settled_at"] or r["created_at"]) + DECLINED_WINDOW_S)),
    } for r in rows]}


def _declined_sentence(home: Any, fields: Mapping[str, Any], lang: str) -> str | None:
    """The sentence the person was shown when they declined; None when the home cannot write it (no permissions library)."""
    from wardmuse import permissions as P

    if not P.available():
        return None
    try:
        return _words(_unsigned(draft_from(fields, _compiler(home).ensure_root())), lang)["sentence"]
    except Exception as exc:
        log.warning("mandates: a declined request could not be described (%s)", type(exc).__name__)
        return None


def h_stop(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    A = _api()
    A._no_body(req)  # noqa: SLF001
    if not person_confirmed(home, req):
        raise A.ApiError("forbidden", "Only the person can stop a permission, from their own app or the command line.")
    compiler = _compiler(home)
    from wardmuse import permissions as P
    from wardmuse.compiler.core import CompileError

    mandate = compiler.find(req.params["id"])
    if mandate is None:
        raise A.ApiError("not_found", "There is no such permission.")
    was = compiler.state_of(mandate)
    try:
        targets = compiler.revoke(mandate.id, actor="person")
    except CompileError:
        raise A.ApiError("not_found", "There is no such permission.") from None
    except P.PermissionError as exc:
        log.warning("mandates: a permission could not be stopped (%s)", exc.code)
        raise A.ApiError("unavailable", "The home could not sign the stop. Run `muse permissions status` on the home.") from None
    done = all(t["state"] in ("revoked", "not_built") for t in targets.values())
    return 200, {"stopped": True, "was": was, "id": mandate.id, "name": mandate.id[3:15], "targets": targets, "complete": done,
                 "recorded": targets.get("records", {}).get("state") == "revoked"}


def h_check(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """What the calling device's own permission says about an action. `has_mandate: false` when it has none."""
    A = _api()
    A._fields(req.body, {"action": str, "resource": (str, A._NONE), "amount": (dict, A._NONE)}, required=("action",))  # noqa: SLF001
    if req.body["action"] not in CHECK_ACTIONS:
        raise A.ApiError("invalid_request", f"action must be one of {', '.join(CHECK_ACTIONS)}.")
    from wardmuse import permissions as P
    from wardmuse.compiler import guard
    from wardmuse.compiler.hosts import check_for_device

    if not P.available():
        return 200, {"has_mandate": False, "decision": "ask", "reason": "", "reason_words": "", "mandate": None}
    compiler = _compiler(home)
    try:
        got = check_for_device(compiler, req.device.name, req.body["action"], req.body.get("resource"), req.body.get("amount"))
    except P.PermissionError as exc:
        log.warning("mandates: a device's permission could not be checked (%s)", exc.code)
        raise A.ApiError("unavailable", "The home could not read its permissions. Run `muse permissions status` on the home.") from None
    words = {"not_covered": "this device's permission says nothing about that, so ask the person",
             "needs_confirmation": "the permission says to ask the person first", "": ""}
    got["reason_words"] = words.get(got["reason"], guard.reason_words(got["reason"]))
    return 200, got
