"""The tools a host bot gets, and what each one asks the home.

This module knows nothing about the protocol or the SDK. A tool is a name, a
description written for a model to read, a JSON Schema for its input, and a
plain function `(ctx, args) -> dict`. `call()` is the one entry point: it
validates, runs, bounds and scrubs, and always returns a result, never raises.

Rules every tool keeps:
  * Input is checked against its schema here (the SDK does not), every string
    has a length limit, and anything shaped like a key or token is refused
    with one generic sentence that repeats none of it.
  * The home is reached only through `ctx.home` (the home control API client).
  * A result is a small JSON object of plain data. Every string in it is cut
    to a limit, and a final pass blanks anything key-shaped. It never holds a
    key, a token, a prompt, another job's text, or exception text.
  * An error is `{"error": {"code", "message"}}` with a fixed sentence from here.
  * A result that touches approval or permission carries `scope_note`.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from muse.mcp import SCOPE_NOTE, permission, skills

MAX_TEXT = 300  # any one string in a result
MAX_ANSWER_CHARS = 4000  # except a skill's answer, which is what the host asked for
MAX_RESULT_CHARS = 6000  # a whole result, as JSON (some hosts cut tool results at about 8,000 characters)
MAX_ARGS_CHARS = 60_000
MAX_PROMPT_CHARS = 20_000
MAX_PROMPTS_TOTAL = 50_000
MAX_SERVICES = 30

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
POLL_S = 0.5
DEFAULT_VALID_FOR_S = 15 * 60  # how long a host's question stays answerable (a payment: the home's 5 minutes)
MAX_VALID_FOR_S = 24 * 3600
PAID_APPROVAL_MAX_AGE_S = 3600

ASK_KINDS = ("save_file", "message", "spend", "send_private")
CARD_ID = r"^[0-9a-f]{16}$"
JOB_ID = r"^[0-9a-f]{12}$"
TASK = r"^[a-z0-9][a-z0-9 _-]{0,63}$"
AMOUNT = r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?$"
CURRENCY = r"^[A-Z][A-Z0-9]{2,7}$"

SECRET_REFUSED = "The request was refused because part of it looks like a key or token. Muse tools never take one."


class ToolError(Exception):
    """A tool failure with a fixed, safe sentence. `message` never holds input, a secret or exception text."""

    def __init__(self, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    def payload(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            err["retry_after"] = int(self.retry_after)
        return {"error": err}


# ---------------------------------------------------------------- the home, as a tool sees it


class HomeLink:
    """The home control API with every failure turned into a ToolError that says what to do."""

    def __init__(self, client: Any) -> None:
        self.client = client  # muse.home.client.HomeClient, or anything with .call(method, path, body)

    def call(self, method: str, path: str, body: Any = None, *, also_ok: Sequence[str] = ()) -> tuple[str, dict[str, Any]]:
        """("ok", payload) or (an error code listed in `also_ok`, {}). Anything else raises ToolError."""
        try:
            status, payload = self.client.call(method, path, body)
        except (OSError, http.client.HTTPException, ValueError):
            raise ToolError("home_unreachable", "Muse's home is not running or cannot be reached. Tell the person to start it "
                                                "with `muse home serve`, then try again.") from None
        if status in (200, 201, 202) and isinstance(payload, dict):
            return "ok", payload
        err = payload.get("error") if isinstance(payload, dict) else None
        code = err.get("code") if isinstance(err, dict) and isinstance(err.get("code"), str) else "internal"
        if code in also_ok:
            return code, {}
        retry = err.get("retry_after") if isinstance(err, dict) and isinstance(err.get("retry_after"), int) else None
        raise _home_error(code, retry)


def _home_error(code: str, retry_after: int | None) -> ToolError:
    if code == "unauthorized":
        return ToolError("unauthorized", "Muse's home did not accept this server's device token. The person needs to issue a "
                                         "new one with `muse home token add` and put it in the token file.")
    if code == "forbidden":
        return ToolError("forbidden", "Muse's home refused this. A host bot may not do it.")
    if code == "unavailable":
        return ToolError("home_unavailable", "Muse's home cannot answer right now. Try again shortly.", retry_after or 5)
    if code == "not_found":
        return ToolError("not_found", "Muse has nothing with that id.")
    if code == "not_built":
        return ToolError("not_built", "This part of Muse is not built yet.")
    if code == "conflict":
        return ToolError("conflict", "Muse refused this because of what is already there. For approvals: too many questions "
                                     "are waiting for the person; wait for answers before asking more.")
    if code == "invalid_request":
        return ToolError("invalid_input", "Muse's home did not accept the request. Check the values against this tool's description.")
    if code == "too_many_requests":
        return ToolError("too_many", "Muse's home is being asked too often. Wait before trying again.", retry_after or 60)
    return ToolError("home_error", "Muse's home hit a problem. Nothing was done.")


@dataclass
class Context:
    home: HomeLink
    allowed_read: tuple[str, ...] = ()
    stop: threading.Event = field(default_factory=threading.Event)  # set at shutdown so a waiting tool lets go
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], float] = time.time


# ---------------------------------------------------------------- input checking (the JSON Schema subset used below)


def _invalid(where: str, what: str) -> ToolError:
    return ToolError("invalid_input", f"The input is not valid: {where} {what}.")


def check(value: Any, schema: Mapping[str, Any], where: str = "the input") -> None:
    """Raise ToolError unless `value` fits `schema`. Messages name fields and rules, never values."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise _invalid(where, "must be an object")
        props = schema.get("properties", {})
        for name in value:
            if name not in props:
                raise _invalid(where, "has a field this tool does not take")
        for name in schema.get("required", ()):
            if name not in value:
                raise _invalid(f"'{name}'", "is required")
        for name, sub in props.items():
            if name in value:
                check(value[name], sub, f"'{name}'")
    elif kind == "string":
        if not isinstance(value, str):
            raise _invalid(where, "must be text")
        if len(value) < schema.get("minLength", 0) or (schema.get("minLength", 0) and not value.strip()):
            raise _invalid(where, "cannot be empty")
        if len(value) > schema["maxLength"]:
            raise _invalid(where, f"is longer than {schema['maxLength']} characters")
        if "\x00" in value:
            raise _invalid(where, "has a character that is not allowed")
        if "pattern" in schema and not re.fullmatch(schema["pattern"].strip("^$"), value):
            raise _invalid(where, "is not in the form this tool expects")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid(where, "must be a whole number")
        if value < schema["minimum"] or value > schema["maximum"]:
            raise _invalid(where, f"must be between {schema['minimum']} and {schema['maximum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise _invalid(where, "must be true or false")
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise _invalid(where, f"must be a list of {schema.get('minItems', 0)} to {schema['maxItems']} items")
        for item in value:
            check(item, schema["items"], f"an item of {where}")
    else:  # pragma: no cover - a schema in this file is wrong
        raise AssertionError(f"unsupported schema type {kind!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise _invalid(where, f"must be one of {', '.join(map(str, schema['enum']))}")


def looks_key_shaped(obj: Any) -> bool:
    from muse.catalog import find_secretlike  # the detector the home and the catalogue use

    return bool(find_secretlike(obj))


def _obj(properties: dict[str, Any] | None = None, required: Sequence[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def _str(limit: int, description: str, **more: Any) -> dict[str, Any]:
    return {"type": "string", "maxLength": limit, "description": description, **more}


_MONEY = _obj({"amount": _str(22, 'A decimal as text, such as "30" or "0.25".', minLength=1, pattern=AMOUNT),
               "currency": _str(8, "A currency code such as TWD or USD.", minLength=1, pattern=CURRENCY)}, ("amount", "currency"))
_MONEY["description"] = 'An amount and a currency, such as {"amount": "30", "currency": "TWD"}.'


# ---------------------------------------------------------------- output bounding


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Answer(str):
    """A skill's answer: the one string in a result that may be longer than MAX_TEXT (cut to MAX_ANSWER_CHARS)."""


def _tidy(obj: Any) -> Any:
    """Every string cut to the limit; anything key-shaped blanked. The last thing done to a result."""
    from muse.catalog import looks_secret

    if isinstance(obj, str):
        if looks_secret(obj):
            return "[removed]"
        return _clip(obj, MAX_ANSWER_CHARS) if isinstance(obj, _Answer) else _clip(obj)
    if isinstance(obj, dict):
        return {str(k): _tidy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_tidy(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return None  # nothing but plain data ever leaves


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


# ---------------------------------------------------------------- allowance_status, pause_status, suggest_service


def _service(p: Mapping[str, Any]) -> dict[str, Any]:
    limits = []
    for lim in p.get("limits") or []:
        if not isinstance(lim, Mapping):
            continue
        limits.append({
            "window": _text(lim.get("window")), "counts": _text(lim.get("metric")), "left": lim.get("remaining"),
            "of": lim.get("limit"), "refills_at": _text(lim.get("reset_at")),
            "estimate": lim.get("source") != "header",  # only a number the provider itself reported is not an estimate
        })
    return {"id": _text(p.get("id")), "name": _text(p.get("name")), "local": p.get("kind") == "local",
            "state": _text(p.get("state")), "blocked_until": _text(p.get("blocked_until")), "limits": limits[:6]}


def t_allowance_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _, snap = ctx.home.call("GET", "/v1/status")
    services = [_service(p) for p in snap.get("providers") or [] if isinstance(p, Mapping) and p.get("configured")]
    now = ctx.clock()
    refills = sorted(
        ts for s in services for lim in s["limits"]
        if lim["window"] != "minute" and (ts := _parse_iso(lim["refills_at"])) is not None and ts > now
    )
    paid = snap.get("paid") if isinstance(snap.get("paid"), Mapping) else {}
    cap, spent = paid.get("cap_usd"), paid.get("spent_usd")
    numbers = isinstance(cap, (int, float)) and isinstance(spent, (int, float))
    queue = snap.get("queue") if isinstance(snap.get("queue"), Mapping) else {}
    return {
        "paused": bool(snap.get("paused")),
        "services_connected": len(services),
        "services": services[:MAX_SERVICES],
        "next_refill_at": _iso(refills[0]) if refills else None,
        "paid_today": {
            "spent_usd": spent if numbers else None, "cap_usd": cap if numbers else None,
            "left_usd": round(max(0.0, cap - spent), 6) if numbers else None,
            "resets_at": _text(paid.get("resets_at")),
            "calls_with_unknown_cost": paid.get("unknown_cost_calls") if isinstance(paid.get("unknown_cost_calls"), int) else None,
            "estimate": True,
        },
        "queue_waiting": queue.get("waiting") if isinstance(queue.get("waiting"), int) else None,
        "as_of": _text(snap.get("generated_at")),
        "about_estimates": "A limit marked estimate is Muse's own count against the published allowance, not a number the "
                           "service reported. Paid spend is always Muse's estimate from published prices.",
    }


def t_pause_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _, snap = ctx.home.call("GET", "/v1/status")
    paused = bool(snap.get("paused"))
    return {
        "paused": paused,
        "meaning": "Everything is paused: background work sends nothing. Do not start new work through Muse; tell the person."
        if paused else "Not paused.",
        "who_can_change_it": "Only the person, with `muse pause` and `muse resume` or the Muse app. A host bot cannot.",
        "scope_note": SCOPE_NOTE,
    }


def t_suggest_service(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _, cat = ctx.home.call("GET", "/v1/catalogue")
    found = [s for s in cat.get("suggestions") or [] if isinstance(s, Mapping) and isinstance(s.get("id"), str)]
    connected = cat.get("connected") if isinstance(cat.get("connected"), list) else []
    if not found:
        return {"suggestion": None, "connected": len(connected),
                "reason": "Muse has no further service to suggest: everything in its catalogue that fits is connected."}
    best = found[0]
    words = best.get("text", {}).get("en", {}) if isinstance(best.get("text"), Mapping) else {}
    words = words if isinstance(words, Mapping) else {}
    return {
        "suggestion": {
            "id": best["id"], "name": _text(best.get("name")), "why": _text(words.get("gain")),
            "effort": _text(words.get("meta")), "catch": _text(words.get("friction")),
            "minutes_estimate": best.get("minutes_estimate") if isinstance(best.get("minutes_estimate"), int) else None,
            "card_required": _text(best.get("card_required")), "phone_required": _text(best.get("phone_required")),
        },
        "how": f"The person connects it themselves with `muse connect {best['id']}` or in the Muse app. "
               "A host bot cannot connect a service and must never ask for or handle a key.",
        "also_possible": [s["id"] for s in found[1:4]],
        "connected": len(connected),
    }


# ---------------------------------------------------------------- ask_approval, check_approval

_FINAL_WORDS = {
    "approved": "The person approved. You may do exactly what the card described, once.",
    "declined": "The person said no. Do not do this action, and do not ask again unless they bring it up.",
    "expired": "Nobody answered in time. Treat that as no: do not do this action.",
    "pending": "Still waiting for the person. Do not do the action yet. Call check_approval with this card_id later.",
}


def _approval(card: Mapping[str, Any]) -> dict[str, Any]:
    status = card.get("status") if card.get("status") in _FINAL_WORDS else "pending"
    out: dict[str, Any] = {"decision": status, "card_id": _text(card.get("id")), "what_to_do": _FINAL_WORDS[status]}
    if status == "approved":
        out["always"] = bool(card.get("always"))
        if out["always"]:
            out["about_always"] = "The person chose 'always allow'. Muse records that but does not act on it yet: ask again next time."
    if status == "pending":
        out["expires_at"] = _text(card.get("expires_at"))
    out["scope_note"] = SCOPE_NOTE
    return out


def _card(ctx: Context, card_id: str) -> Mapping[str, Any]:
    _, payload = ctx.home.call("GET", f"/v1/cards/{card_id}")
    card = payload.get("card")
    if not isinstance(card, Mapping):
        raise ToolError("home_error", "Muse's home hit a problem. Nothing was done.")
    return card


def t_ask_approval(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    kind = args["kind"]
    if kind == "spend" and "cost" not in args:
        raise _invalid("'cost'", "is required when kind is spend")
    extra = args.get("extra") or {}
    if len(json.dumps(extra)) > 4000:
        raise _invalid("'extra'", "is too large")
    body: dict[str, Any] = {
        "kind": kind, "title": args["title"], "thing": args["thing"], "where": args.get("where", ""),
        "under": "Asked by a host bot through Muse's tools", "extra": extra,
    }
    if "cost" in args:
        body["cost"] = args["cost"]
    if "valid_for_s" in args:
        body["ttl_s"] = args["valid_for_s"]
    elif kind != "spend":
        body["ttl_s"] = DEFAULT_VALID_FOR_S
    try:
        _, payload = ctx.home.call("POST", "/v1/cards", body)
    except ToolError as err:
        if err.code == "invalid_input":
            raise ToolError("invalid_input", "Muse did not accept the card. Check the text lengths, the cost, and the 'extra' "
                                             "fields this kind needs (see this tool's description).") from None
        raise
    card = payload.get("card")
    if not isinstance(card, Mapping) or not isinstance(card.get("id"), str) or not re.fullmatch(CARD_ID, card["id"]):
        raise ToolError("home_error", "Muse's home hit a problem. Nothing was done.")
    deadline = ctx.monotonic() + args.get("timeout_s", DEFAULT_TIMEOUT_S)
    while card.get("status") == "pending":
        left = deadline - ctx.monotonic()
        if left <= 0 or ctx.stop.wait(min(POLL_S, left)):
            break
        try:
            card = _card(ctx, card["id"])
        except ToolError as err:
            if err.code not in ("home_unreachable", "home_unavailable"):
                raise  # the home may come back while we wait; anything else is final
    return _approval(card)


def t_check_approval(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    return _approval(_card(ctx, args["card_id"]))


# ---------------------------------------------------------------- check_permission, record_action


def t_check_permission(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    verdict = permission.decide(args["action"], args.get("resource"), args.get("amount"), allowed_read=ctx.allowed_read)
    return {
        "decision": verdict["decision"], "reason": verdict["reason"], "enforced": False,
        "what_to_do": "Go ahead." if verdict["decision"] == "allow" else "Call ask_approval and wait for the person before doing this.",
        "advisory": "This answer is advice only until Muse permissions are built. Muse does not block anything on the strength of it.",
        "scope_note": SCOPE_NOTE,
    }


_RECORD_ACTION = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}")
# Action families only the home writes (muse.home.api.HOME_ACTIONS); a host's report cannot use them.
HOME_ACTIONS = ("home", "card", "app", "key", "job", "skill", "mandate", "records")
_RECORD_OUTCOMES = {"done": "ok", "failed": "failed", "declined": "declined", "skipped": "declined"}
RECORD_NOTE = ("This is this host's own report: Muse wrote down what it was told and cannot check it. It is marked as done by "
               "a helper under this host's device, never as done by the person.")


def record_action_name(text: str) -> str:
    """The dotted action name a record carries. A name already in that form is kept (unless it is one of the
    home's own families); anything else, such as 'sent an email', becomes `host.sent_an_email`."""
    text = text.strip().lower()
    if _RECORD_ACTION.fullmatch(text) and text.split(".")[0] not in HOME_ACTIONS:
        return text
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:32].strip("_")
    if not slug or not slug[0].isalpha():
        slug = ("x_" + slug)[:32].strip("_")
    return f"host.{slug}"


def _minor_units(cost: Mapping[str, Any]) -> dict[str, Any]:
    """{"amount": "30", "currency": "TWD"} as the record's {"currency": "TWD", "minor": 3000}."""
    from decimal import Decimal, InvalidOperation

    from muse.records.commands import MINOR_DIGITS  # a table of decimal places; nothing secret, no signing code

    digits = MINOR_DIGITS.get(cost["currency"])
    if digits is None:
        raise ToolError("invalid_input", "Muse does not know that currency's smallest unit, so it cannot record the cost. "
                                         "Leave 'cost' out and say the amount in the summary.")
    try:
        minor = Decimal(cost["amount"]).scaleb(digits)
    except InvalidOperation:
        raise _invalid("'cost'", "is not an amount") from None
    if minor != minor.to_integral_value() or not 0 <= minor < 2**53:
        raise _invalid("'cost'", "has more decimal places than that currency has, or is too large")
    return {"currency": cost["currency"], "minor": int(minor)}


def t_record_action(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    action = record_action_name(args["action"])
    body: dict[str, Any] = {"action": action, "summary": args["summary"], "outcome": _RECORD_OUTCOMES[args["outcome"]]}
    if "cost" in args:
        body["cost"] = _minor_units(args["cost"])
    if "sent_to" in args:
        body["egress"] = args["sent_to"]
    try:
        outcome, payload = ctx.home.call("POST", "/v1/receipts", body,
                                         also_ok=("not_built", "method_not_allowed", "not_found", "unavailable"))
    except ToolError as err:
        if err.code == "invalid_input":
            raise ToolError("invalid_input", "Muse did not accept the record. The summary must be one line of at most 200 "
                                             "characters with no key, password or long code in it.") from None
        raise
    record = payload.get("record") if outcome == "ok" else None
    if not isinstance(record, Mapping) or not isinstance(record.get("seq"), int):
        return {"recorded": False, "reason": "this home cannot write records right now",
                "what_to_do": "Carry on. Nothing was written anywhere; do not tell the person it was recorded.",
                "scope_note": SCOPE_NOTE}
    return {"recorded": True, "seq": record["seq"], "hash": _text(payload.get("hash")), "action": action,
            "recorded_as": _text(record.get("actor")), "about": RECORD_NOTE, "scope_note": SCOPE_NOTE}


# ---------------------------------------------------------------- queue_job, job_status


def _job(job: Mapping[str, Any]) -> dict[str, Any]:
    done = job.get("status") == "done"
    return {
        "id": _text(job.get("id")), "status": _text(job.get("status")), "kind": _text(job.get("kind")),
        "task": _text(job.get("task")), "by": _text(job.get("deadline")), "allow_paid": bool(job.get("allow_paid")),
        "attempts": job.get("attempts") if isinstance(job.get("attempts"), int) else None,
        "times_held_back": job.get("deferrals") if isinstance(job.get("deferrals"), int) else None,
        "next_try_at": _text(job.get("next_attempt_at")), "why_waiting": _text(job.get("last_reason")),
        "finished_at": _text(job.get("finished_at")), "service": _text(job.get("provider")) if done else None,
        "cost_usd": job.get("cost_usd") if done and isinstance(job.get("cost_usd"), (int, float)) else None,
        "result_file": _text(job.get("out_path")) if done else None,
    }


def _check_paid_approval(ctx: Context, card_id: str | None) -> None:
    refused = ToolError("approval_needed", "A job that may use paid services needs the person's approval first. Call "
                                           "ask_approval with kind spend, wait for 'approved', then pass that card_id "
                                           "as approval_card_id within the hour.")
    if card_id is None:
        raise refused
    try:
        card = _card(ctx, card_id)
    except ToolError as err:
        if err.code == "not_found":
            raise refused from None
        raise
    answered = _parse_iso(card.get("answered_at"))
    asked_through_tools = isinstance(card.get("job_ref"), str) and card["job_ref"].startswith("device:")
    fresh = answered is not None and ctx.clock() - answered <= PAID_APPROVAL_MAX_AGE_S
    if not (card.get("status") == "approved" and card.get("kind") == "spend" and asked_through_tools and fresh):
        raise refused


def t_queue_job(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    if ("prompt" in args) == ("prompts" in args):
        raise _invalid("the input", "needs exactly one of 'prompt' or 'prompts'")
    body: dict[str, Any] = {"allow_paid": bool(args.get("allow_paid", False))}
    if "prompt" in args:
        body.update(kind="prompt", prompt=args["prompt"])
    else:
        if sum(len(p) for p in args["prompts"]) > MAX_PROMPTS_TOTAL:
            raise _invalid("'prompts'", f"is longer than {MAX_PROMPTS_TOTAL} characters in all")
        body.update(kind="batch", prompts=args["prompts"])
    if "task" in args:
        body["task"] = args["task"]
    if "by" in args:
        when = _parse_iso(args["by"])
        if when is None or when <= ctx.clock():
            raise _invalid("'by'", "must be a future time with a zone, such as 2026-11-14T08:30:00Z")
        body["deadline"] = args["by"]
    if body["allow_paid"]:
        _check_paid_approval(ctx, args.get("approval_card_id"))
    try:
        _, payload = ctx.home.call("POST", "/v1/jobs", body)
    except ToolError as err:
        if err.code == "invalid_input":
            raise ToolError("invalid_input", "Muse did not accept the job. Check the task name (light, everyday, heavy) "
                                             "and the 'by' time.") from None
        raise
    job = payload.get("job") if isinstance(payload.get("job"), Mapping) else {}
    return {
        "job": _job(job),
        "what_happens": "It runs in the background on free allowance when the person is not busy, and is held while Muse is "
                        "paused. Call job_status with the id to see progress. The answer is written to a file on the "
                        "person's computer (result_file, once done); it is not returned through this tool.",
    }


def t_job_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    _, payload = ctx.home.call("GET", f"/v1/jobs/{args['id']}")
    job = payload.get("job") if isinstance(payload.get("job"), Mapping) else {}
    return {"job": _job(job)}


# ---------------------------------------------------------------- list_skills, run_skill


def _skills_not_built() -> dict[str, Any]:
    return {"built": False, "reason": skills.why_not_built(),
            "what_to_do": "Do the work with your own tools, or tell the person this is not available yet."}


MAX_SKILLS_LISTED = 25
RUN_ID = r"^[0-9a-f]{16}$"
FOLDER_NAME = r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$"

_RUN_WORDS = {
    "running": "Still running on the person's home. Call skill_status with this run_id later.",
    "done": "Finished, and its answer passed the skill's own check.",
    "partial": "Finished, but something could not be done; the notes say what. Tell the person what is missing.",
    "needs_choice": "Muse could not finish this on free allowance. The choices it was offered are listed; put them to the "
                    "person. Do not treat the answer as checked.",
    "failed": "It did not finish; the notes say why. Nothing was written.",
}
_FILE_WORDS = {
    "pending": "waiting for the person to approve it in Muse; not written", "applying": "being written", "written": "written",
    "refused": "approved, but could not be written", "discarded": "not written: the person said no, or did not answer in time",
}


def t_list_skills(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    backend = skills.backend()
    if backend is None:
        return {**_skills_not_built(), "skills": []}
    listed = backend.list()
    listed = listed if isinstance(listed, Mapping) else {}
    found = [s for s in listed.get("skills") or [] if isinstance(s, Mapping) and isinstance(s.get("name"), str)]
    folders = [f for f in listed.get("folders") or [] if isinstance(f, Mapping) and isinstance(f.get("name"), str)]
    return {
        "built": True,
        "skills": [{"name": s["name"], "summary": _clip(s["description"], 160) if isinstance(s.get("description"), str) else None,
                    "can_run": bool(s.get("approved")), "reads_folders": bool(s.get("reads_folders")),
                    "proposes_files": bool(s.get("writes_files"))} for s in found[:MAX_SKILLS_LISTED]],
        "more_skills": max(0, len(found) - MAX_SKILLS_LISTED),
        "folders": [{"name": f["name"], "new_files_allowed": bool(f.get("output"))} for f in folders[:20]],
        "about_folders": "A skill that reads folders can be given only these, by name. The person chooses them on the home "
                         "with `muse skills folders add`; a host bot cannot add one or name any other path.",
    }


def _answer(output: Any) -> _Answer | None:
    if output is None:
        return None
    if isinstance(output, Mapping) and isinstance(output.get("text"), str):
        return _Answer(output["text"])
    if isinstance(output, str):
        return _Answer(output)
    try:
        return _Answer(json.dumps(output, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return None


def _run(run: Any) -> dict[str, Any]:
    run = run if isinstance(run, Mapping) else {}
    status = run.get("status") if run.get("status") in _RUN_WORDS else "failed"
    run_id = run.get("id") if isinstance(run.get("id"), str) and re.fullmatch(RUN_ID.strip("^$"), run["id"]) else None
    out: dict[str, Any] = {"built": True, "ran": status != "running", "run_id": run_id, "skill": _text(run.get("skill")),
                           "status": status, "what_to_do": _RUN_WORDS[status]}
    if status != "running":
        result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
        check = result.get("check") if isinstance(result.get("check"), Mapping) else {}
        out["checked"] = check.get("status") == "passed"
        out["notes"] = [_clip(n) for n in result.get("notes") or [] if isinstance(n, str)][:5]
        if status == "needs_choice":
            out["choices"] = [c for c in result.get("options") or [] if isinstance(c, Mapping)][:5]
        files = [w for w in run.get("writes") or [] if isinstance(w, Mapping)]
        if files:
            out["files"] = [{"file": _clip(w["file"], 80) if isinstance(w.get("file"), str) else None,
                             "state": _FILE_WORDS.get(w.get("state"), "unknown"), "card_id": _text(w.get("card_id"))}
                            for w in files[:10]]
            out["more_files"] = max(0, len(files) - 10)
            out["about_files"] = ("A skill only proposes files. Muse writes one only after the person approves its card in Muse; "
                                  "check_approval with the card_id shows the answer. Do not write the file yourself instead.")
        # The answer gets what is left of the result's size, at most MAX_ANSWER_CHARS, so a long answer is cut, not refused.
        text = _answer(result.get("output"))
        whole = len(text) if text is not None else 0
        room = MAX_RESULT_CHARS - len(json.dumps(_tidy(out), ensure_ascii=False)) - 400
        if text is not None:
            text = _Answer(text[:MAX_ANSWER_CHARS])
            while text and len(json.dumps(text, ensure_ascii=False)) > room:
                text = _Answer(text[: max(0, int(len(text) * 0.9) - 1)])
        out["answer"] = text
        out["answer_cut_short"] = text is not None and len(text) < whole
    out["scope_note"] = SCOPE_NOTE
    return out


def _skill_error(err: ToolError) -> ToolError:
    if err.code == "conflict":
        return ToolError("conflict", "Muse cannot run that skill now: it is not approved on the person's home, or another "
                                     "skill is still running. Wait, or tell the person.")
    if err.code == "invalid_input":
        return ToolError("invalid_input", "Muse did not accept the run. Check the inputs, and use only folder names that "
                                          "list_skills shows.")
    if err.code == "not_found":
        return ToolError("not_found", "Muse has no skill, or no run, with that name or id. Call list_skills.")
    return err


def t_run_skill(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    backend = skills.backend()
    if backend is None:
        return {**_skills_not_built(), "ran": False}
    try:
        run = backend.run(args["name"], args.get("inputs") or {}, args.get("folders") or (), args.get("out"))
    except ToolError as err:
        raise _skill_error(err) from None
    run_id = run.get("id") if isinstance(run, Mapping) else None
    if not isinstance(run_id, str) or not re.fullmatch(RUN_ID.strip("^$"), run_id):
        raise ToolError("home_error", "Muse's home hit a problem. Nothing was done.")
    deadline = ctx.monotonic() + args.get("timeout_s", DEFAULT_TIMEOUT_S)
    while run.get("status") == "running":
        left = deadline - ctx.monotonic()
        if left <= 0 or ctx.stop.wait(min(POLL_S, left)):
            break
        try:
            run = backend.status(run_id)
        except ToolError as err:
            if err.code not in ("home_unreachable", "home_unavailable"):
                raise  # the home may come back while we wait; anything else is final
    return _run(run)


def t_skill_status(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    backend = skills.backend()
    if backend is None:
        return {**_skills_not_built(), "ran": False}
    try:
        return _run(backend.status(args["run_id"]))
    except ToolError as err:
        raise _skill_error(err) from None


# ---------------------------------------------------------------- the table


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[Context, dict[str, Any]], dict[str, Any]]
    read_only: bool


_EXTRA = {"type": "object", "description": "The extra line this kind of card needs; see the tool description."}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "allowance_status", "What is left",
        "What is left to use through Muse right now: for each connected AI service, how much of its free allowance is left "
        "and when it refills; the next refill time; today's paid spend against the person's daily cap; and whether "
        "everything is paused. Call it before starting a large piece of work, or after a model call was turned away, to "
        "decide whether to go on, wait, or use queue_job. Numbers marked estimate are Muse's own count. Read-only.",
        _obj(), t_allowance_status, True,
    ),
    Tool(
        "pause_status", "Is everything paused",
        "Whether the person has paused Muse. While paused, background work sends nothing, so do not start new work through "
        "Muse and say so to the person. This tool only reads: a host bot cannot pause or resume Muse. Read-only.",
        _obj(), t_pause_status, True,
    ),
    Tool(
        "suggest_service", "The next service worth connecting",
        "The one AI service most worth connecting next and why: what it adds, how long it takes, and any catch (a card or a "
        "phone number). Use it when allowances keep running out, and pass the suggestion to the person. Only the person "
        "can connect a service; never ask for, read or handle a key. Read-only.",
        _obj(), t_suggest_service, True,
    ),
    Tool(
        "ask_approval", "Ask the person first",
        "Ask the person before doing something that writes, sends, or spends. This puts a question (a card) in front of the "
        "person in Muse and waits for the answer. Returns decision = approved, declined, expired, or pending. Only "
        "'approved' means go ahead, and only for exactly what the card said. On 'pending' (the wait ran out) keep the "
        "card_id and call check_approval later; do not do the action meanwhile.\n"
        "kind and the 'extra' it needs: save_file (write or change a file; extra optional {size_bytes}); message (send "
        "something in the person's name; extra {preview, to?, draft?}); spend (pay money; needs cost, and extra {payee, "
        "limit_left: {amount, currency}}); send_private (send private data to an outside service; extra {badge, "
        "services: [names]}).\n"
        "Write title, thing and where in plain words the person will understand; never put a key, token or password in "
        "them. " + SCOPE_NOTE + " So this protects only if you ask before acting.",
        _obj({
            "kind": {"type": "string", "maxLength": 20, "enum": list(ASK_KINDS), "description": "What sort of action this is."},
            "title": _str(80, "A short heading, such as 'Save a file'.", minLength=1),
            "thing": _str(200, "Exactly what: the file name, the message's subject, the payee and amount.", minLength=1),
            "where": _str(200, "Where it goes or happens: a folder, a recipient, a service."),
            "cost": _MONEY,
            "extra": _EXTRA,
            "timeout_s": {"type": "integer", "minimum": 0, "maximum": MAX_TIMEOUT_S,
                          "description": f"How long to wait for the answer, in seconds. Default {DEFAULT_TIMEOUT_S}, at most {MAX_TIMEOUT_S}."},
            "valid_for_s": {"type": "integer", "minimum": 1, "maximum": MAX_VALID_FOR_S,
                            "description": "How long the person can still answer, in seconds. Default 15 minutes (5 for spend)."},
        }, ("kind", "title", "thing")),
        t_ask_approval, False,
    ),
    Tool(
        "check_approval", "Look up an answer",
        "The current answer to a question asked with ask_approval: approved, declined, expired, or still pending. Does not "
        "wait. Use it after ask_approval returned 'pending'. " + SCOPE_NOTE + " Read-only.",
        _obj({"card_id": _str(16, "The card_id that ask_approval returned.", minLength=16, pattern=CARD_ID)}, ("card_id",)),
        t_check_approval, True,
    ),
    Tool(
        "check_permission", "May I do this without asking",
        "Whether an action may go ahead without asking the person. ADVISORY ONLY: Muse permissions are not built yet, so "
        "this is an interim rule and Muse enforces nothing on the strength of it. It answers 'allow' only for a read-only "
        "action (read, list, search) on something the person listed for this host, and 'ask' for everything else, "
        "including anything that writes, sends, runs or spends. On 'ask', call ask_approval. " + SCOPE_NOTE + " Read-only.",
        _obj({
            "action": {"type": "string", "maxLength": 20, "enum": list(permission.ACTIONS), "description": "What you are about to do."},
            "resource": _str(400, "What it acts on: a path, an address, a name."),
            "amount": _MONEY,
        }, ("action",)),
        t_check_permission, True,
    ),
    Tool(
        "record_action", "Write down what was done",
        "Add an entry to the person's Muse records saying what you did, so they can see it later. The entry is signed "
        "by the person's home and cannot be changed afterwards. It returns recorded = true with the entry's number (seq) "
        "and hash; never tell the person something was recorded unless recorded is true. Only what you report here is "
        "recorded, as this host's own report: Muse does not see what this host does with its own tools and cannot check "
        "the report. The entry is marked as done by a helper under this host, never as done by the person. "
        + SCOPE_NOTE,
        _obj({
            "action": _str(80, "What kind of action: a dotted name such as files.write, message.send or web.search, or a "
                               "few words (Muse then files it under host.<your words>).", minLength=1),
            "summary": _str(200, "What was done, in one plain sentence on one line. Say it in words: no private content, "
                                 "no names of people, no keys or long codes.", minLength=1),
            "outcome": {"type": "string", "maxLength": 12, "enum": ["done", "failed", "declined", "skipped"], "description": "How it ended."},
            "cost": _MONEY,
            "sent_to": _str(72, 'Where the content went: "none" if it stayed on this computer, or "service:<name>" in '
                                'lower case (such as service:gmail) if it was sent to an outside service. Leave it out if '
                                "you do not know.", minLength=1, pattern=r"^(?:none|service:[a-z0-9][a-z0-9._-]{0,63})$"),
        }, ("action", "summary", "outcome")),
        t_record_action, False,
    ),
    Tool(
        "queue_job", "Run something later, on free allowance",
        "Hand Muse a prompt (or up to 20 prompts as one batch) to run in the background on free allowance that would "
        "otherwise go unused. Use it for work that is not urgent: summaries, tagging, drafts. Give 'task' to say how strong "
        "a model it needs (light, everyday, heavy) and 'by' for a deadline. It returns a job id at once; the answer is not "
        "returned here but written to a file on the person's computer. allow_paid is false unless the person approved "
        "spending: to set it true, first get 'approved' from ask_approval with kind spend and pass that card_id as "
        "approval_card_id. Do not put keys or passwords in a prompt.",
        _obj({
            "prompt": _str(MAX_PROMPT_CHARS, "One prompt.", minLength=1),
            "prompts": {"type": "array", "minItems": 1, "maxItems": 20, "items": _str(MAX_PROMPT_CHARS, "A prompt.", minLength=1),
                        "description": "Several prompts run as one batch job. Use this or 'prompt', not both."},
            "task": _str(64, "How demanding the work is: light, everyday or heavy. Default: the queue's own default.", minLength=1, pattern=TASK),
            "by": _str(40, "When it should be done by: a time with a zone, such as 2026-11-14T08:30:00Z.", minLength=1),
            "allow_paid": {"type": "boolean", "description": "May it use paid services, within the person's daily cap. Default false."},
            "approval_card_id": _str(16, "With allow_paid: the card_id of an approved spend question.", minLength=16, pattern=CARD_ID),
        }),
        t_queue_job, False,
    ),
    Tool(
        "job_status", "How a queued job is doing",
        "Progress of a job made with queue_job: queued, running, deferred (held back, with the reason and the next try), "
        "done, failed or cancelled. When done it gives the file the answer was written to. It never returns a prompt or an "
        "answer. Read-only.",
        _obj({"id": _str(12, "The job id that queue_job returned.", minLength=12, pattern=JOB_ID)}, ("id",)),
        t_job_status, True,
    ),
    Tool(
        "list_skills", "Muse skills",
        "The Muse skills on the person's home: each one's name, what it does, whether it may run (can_run), whether it "
        "reads folders and whether it proposes files. Also the folder names a skill may be given: only folders the person "
        "put on a list on the home, by name. Call it before run_skill. Read-only.",
        _obj(), t_list_skills, True,
    ),
    Tool(
        "run_skill", "Run a Muse skill",
        "Run one Muse skill on the person's home, by name, and wait for its answer. The skill reaches models through Muse "
        "(rationed, within its own budget, never paid for through this tool) and reads only the folders you name from "
        "list_skills; you cannot give it a path. It returns status = done, partial, needs_choice, failed, or running "
        "(the wait ran out: call skill_status with the run_id). 'answer' is the skill's answer; 'checked' says whether it "
        "passed the skill's own check. A skill never writes files itself: each file it proposes becomes a question to the "
        "person in Muse and is written by Muse only if they approve. " + SCOPE_NOTE,
        _obj({
            "name": _str(64, "The skill's name, from list_skills.", minLength=1, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
            "inputs": {"type": "object", "description": "The skill's inputs: names and values (text, numbers, true or false)."},
            "folders": {"type": "array", "minItems": 0, "maxItems": 8, "items": _str(40, "A folder name.", minLength=1, pattern=FOLDER_NAME),
                        "description": "Names of folders, from list_skills, that the skill may read. Names only, never paths."},
            "out": _str(40, "The name of a folder, from list_skills with new_files_allowed, where the skill may propose new "
                            "files. Leave it out to use Muse's own folder for that skill.", minLength=1, pattern=FOLDER_NAME),
            "timeout_s": {"type": "integer", "minimum": 0, "maximum": MAX_TIMEOUT_S,
                          "description": f"How long to wait for it to finish, in seconds. Default {DEFAULT_TIMEOUT_S}, at most {MAX_TIMEOUT_S}."},
        }, ("name",)),
        t_run_skill, False,
    ),
    Tool(
        "skill_status", "How a skill run went",
        "The state of a run started with run_skill: still running, or its answer, notes and what became of each file it "
        "proposed (waiting for the person, written, or not written). Does not wait. Read-only.",
        _obj({"run_id": _str(16, "The run_id that run_skill returned.", minLength=16, pattern=RUN_ID)}, ("run_id",)),
        t_skill_status, True,
    ),
)

BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}

# Planned, and deliberately absent (docs/hosts/README.md): `pay` (the payment package does not exist yet),
# and `pause` / `resume` (a host bot must not be able to resume itself; it gets `pause_status` only).
NOT_EXPOSED = ("pay", "pause", "resume")


def _check_free_objects(tool: Tool, args: dict[str, Any]) -> None:
    """`extra` and `inputs` are open objects in the schema; bound them here."""
    for name in ("extra", "inputs"):
        if name in args:
            if not isinstance(args[name], dict):
                raise _invalid(f"'{name}'", "must be an object")
            if len(json.dumps(args[name], default=str)) > 4000:
                raise _invalid(f"'{name}'", "is too large")


def call(ctx: Context, name: str, arguments: Any) -> tuple[dict[str, Any], bool]:
    """Run one tool. Returns (result, is_error). Never raises; never returns anything but bounded plain data."""
    try:
        tool = BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolError("unknown_tool", "Muse has no tool with that name.")
        args = {} if arguments is None else arguments
        if not isinstance(args, dict):
            raise _invalid("the input", "must be an object")
        try:
            size = len(json.dumps(args))
        except (TypeError, ValueError, RecursionError):
            raise _invalid("the input", "must be plain JSON") from None
        if size > MAX_ARGS_CHARS:
            raise _invalid("the input", "is too large")
        if looks_key_shaped(args):
            raise ToolError("refused", SECRET_REFUSED)
        free = {k: args[k] for k in ("extra", "inputs") if k in args}
        _check_free_objects(tool, args)
        check({k: v for k, v in args.items() if k not in free}, _without(tool.input_schema, free))
        result = _tidy(tool.run(ctx, args))
        if len(json.dumps(result, ensure_ascii=False)) > MAX_RESULT_CHARS:
            raise ToolError("too_large", "The answer was too large to return. Nothing else went wrong.")
        return result, False
    except ToolError as err:
        return err.payload(), True
    except Exception:  # a bug here, or a reply of an unexpected shape: say nothing about it
        return ToolError("internal", "The Muse tools server hit a problem it did not expect. Nothing was done on purpose; "
                                     "check with allowance_status or check_approval before retrying.").payload(), True


def _without(schema: Mapping[str, Any], names: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(schema)
    out["properties"] = {k: v for k, v in schema["properties"].items() if k not in names}
    return out
