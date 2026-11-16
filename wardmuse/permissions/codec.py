"""Plain-data (JSON) forms of drafts, narrowings and requests.

Used by the command line, by the conformance vectors and by anything that passes
these values between processes. Decoding checks shapes only; the rules are
`propose_from_fields`, `canonical_narrowing` and `valid_request`.
"""

from __future__ import annotations

from typing import Any

from wardmuse.permissions.model import (
    Amount,
    Cap,
    Confirm,
    Draft,
    Limit,
    NarrowLimit,
    Narrowing,
    PermissionError,
    Request,
    Resource,
)


def _bad(what: str) -> PermissionError:
    return PermissionError("invalid_data", what)


def confirm_to_data(c: Confirm) -> list:
    return ["over_amount", c.unit, c.decimals, c.amount] if c.rule == "over_amount" else [c.rule]


def confirm_from_data(data: Any) -> Confirm:
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], str):
        return Confirm(data[0])
    if isinstance(data, list) and len(data) == 4 and data[0] == "over_amount":
        return Confirm("over_amount", data[1], data[2], data[3])
    raise _bad("confirm is [rule] or [\"over_amount\", unit, decimals, amount]")


def _rows(data: Any, width: int, what: str) -> list[list]:
    if not isinstance(data, list) or not all(isinstance(row, list) and len(row) == width for row in data):
        raise _bad(f"{what} is a list of {width}-item lists")
    return data


def _strings(data: Any, what: str) -> tuple[str, ...]:
    if not isinstance(data, list):
        raise _bad(f"{what} is a list")
    return tuple(data)


def draft_to_data(d: Draft) -> dict[str, Any]:
    return {
        "issuer": d.issuer,
        "holder": d.holder,
        "actions": list(d.actions),
        "routine": list(d.routine),
        "resources": [[r.kind, r.value] for r in d.resources],
        "limits": [[l.kind, l.unit, l.decimals, l.period, l.amount] for l in d.limits],
        "purpose_text": d.purpose_text,
        "purpose_code": d.purpose_code,
        "issued_at": d.issued_at,
        "not_before": d.not_before,
        "not_after": d.not_after,
        "confirm": confirm_to_data(d.confirm),
        "privacy": d.privacy,
        "period_zone": d.period_zone,
    }


def draft_from_data(data: Any) -> Draft:
    if not isinstance(data, dict):
        raise _bad("a draft is an object")
    try:
        return Draft(
            issuer=data["issuer"],
            holder=data["holder"],
            actions=_strings(data["actions"], "actions"),
            routine=_strings(data.get("routine", []), "routine"),
            resources=tuple(Resource(*row) for row in _rows(data["resources"], 2, "resources")),
            limits=tuple(Limit(*row) for row in _rows(data.get("limits", []), 5, "limits")),
            purpose_text=data.get("purpose_text", ""),
            purpose_code=data.get("purpose_code", ""),
            issued_at=data["issued_at"],
            not_before=data["not_before"],
            not_after=data["not_after"],
            confirm=confirm_from_data(data.get("confirm", ["each_write"])),
            privacy=data.get("privacy", "local"),
            period_zone=data.get("period_zone", "UTC"),
        )
    except KeyError as exc:
        raise _bad(f"a draft needs {exc.args[0]}") from None


def narrowing_to_data(n: Narrowing) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if n.actions:
        out["actions"] = list(n.actions)
    if n.resources:
        out["resources"] = [[r.kind, r.value] for r in n.resources]
    if n.limits:
        out["limits"] = [[l.kind, l.unit, l.period, l.amount] for l in n.limits]
    if n.caps:
        out["caps"] = [[c.kind, c.unit, c.amount] for c in n.caps]
    for name in ("not_before", "not_after", "privacy", "job", "tool", "card"):
        if getattr(n, name) is not None:
            out[name] = getattr(n, name)
    if n.confirm is not None:
        out["confirm"] = confirm_to_data(n.confirm)
    return out


_NARROWING_KEYS = {"actions", "resources", "limits", "caps", "not_before", "not_after", "privacy", "confirm", "job", "tool", "card"}


def narrowing_from_data(data: Any) -> Narrowing:
    if not isinstance(data, dict) or not set(data) <= _NARROWING_KEYS:
        raise _bad("a narrowing is an object with known keys")
    return Narrowing(
        actions=_strings(data.get("actions", []), "actions"),
        resources=tuple(Resource(*row) for row in _rows(data.get("resources", []), 2, "resources")),
        limits=tuple(NarrowLimit(*row) for row in _rows(data.get("limits", []), 4, "limits")),
        caps=tuple(Cap(*row) for row in _rows(data.get("caps", []), 3, "caps")),
        not_before=data.get("not_before"),
        not_after=data.get("not_after"),
        privacy=data.get("privacy"),
        confirm=None if data.get("confirm") is None else confirm_from_data(data["confirm"]),
        job=data.get("job"),
        tool=data.get("tool"),
        card=data.get("card"),
    )


def request_to_data(r: Request) -> dict[str, Any]:
    return {
        "holder": r.holder,
        "action": r.action,
        "resource": [r.resource.kind, r.resource.value],
        "amounts": [[a.kind, a.unit, a.value] for a in r.amounts],
        "privacy": r.privacy,
        "job": r.job,
        "tool": r.tool,
        "card": r.card,
    }


def request_from_data(data: Any) -> Request:
    if not isinstance(data, dict):
        raise _bad("a request is an object")
    try:
        resource = data["resource"]
        if not isinstance(resource, list) or len(resource) != 2:
            raise _bad("resource is [kind, value]")
        return Request(
            holder=data["holder"],
            action=data["action"],
            resource=Resource(*resource),
            amounts=tuple(Amount(*row) for row in _rows(data.get("amounts", []), 3, "amounts")),
            privacy=data["privacy"],
            job=data.get("job"),
            tool=data.get("tool"),
            card=data.get("card"),
        )
    except KeyError as exc:
        raise _bad(f"a request needs {exc.args[0]}") from None
