"""A host bot's permission: the default one `muse hosts setup` offers, and the answer behind `check_permission`.

A host's permission is issued with holder `device:<the host's tools device>`. The default:

    llm.call      through the Muse router, up to the caps of the host's app (if it has a daily request cap)
    skills.run    asks the person each time (it is not marked routine)
    no llm.paid   the host cannot spend
    no files.*    a host reaches the person's files only by running a skill with a folder the person
                  put on the list by name; everything else is "ask"

`check_for_device` answers what the tools server's `check_permission` tool asks. What Muse can and cannot
do with that answer has not changed: a host's own shell and file tools are outside Muse, so for those the
answer is still advice. It is no longer a guess, though: it is the person's signed permission, checked.
"""

from __future__ import annotations

import posixpath
from typing import Any

from muse import permissions as P

DEFAULT_DAYS = 90
TOOL_ACTIONS = {
    "read": "files.read", "list": "files.read", "search": "files.read",
    "write": "files.write", "delete": "files.write",
    "run": "skills.run", "spend": "pay.spend",
}
SKILLS = P.Resource("home", "skills")
ROUTER = P.Resource("provider", "router")
# A refusal that only means "the permission says nothing about this": the person may still say yes to it once.
NOT_COVERED = ("action_not_allowed", "resource_not_allowed", "amount_required", "limit_missing", "bad_request")


def holder_for(device: str) -> str:
    return f"device:{device}"


def default_draft(issuer: str, device: str, *, now: int, daily_requests: int | None = None, privacy: str = "private",
                  days: int = DEFAULT_DAYS, zone: str = "UTC", with_models: bool = True) -> Any:
    """The permission `muse hosts setup` offers for a host's tools device. `with_models=False` for a host that
    only uses Muse's tools and has no app of its own on the router."""
    limits = (P.Limit("requests", "", 0, "day", int(daily_requests)),) if daily_requests is not None and with_models else ()
    actions = ("llm.call", "skills.run") if with_models else ("skills.run",)
    resources = (ROUTER, SKILLS) if with_models else (SKILLS,)
    return P.propose_from_fields(P.Draft(
        issuer=issuer, holder=holder_for(device), actions=actions, resources=resources, limits=limits,
        purpose_text="Host bot: models through Muse, skills when you agree", purpose_code="router.priority.interactive",
        issued_at=now, not_before=now, not_after=now + days * 86400, confirm=P.EACH_WRITE, privacy=privacy, period_zone=zone))


def _resource(action: str, resource: Any) -> Any | None:
    if action == "skills.run":
        return SKILLS
    if action in ("files.read", "files.write") and isinstance(resource, str):
        text = resource.strip().replace("\\", "/")
        if not text.startswith("/") or "\x00" in text or ".." in text.split("/"):
            return None
        return P.Resource("folder", posixpath.normpath(text))
    return None


def check_for_device(compiler: Any, device: str, action: str, resource: Any = None, amount: Any = None) -> dict[str, Any]:
    """{"has_mandate": bool, "decision": allow | ask | deny, "reason": code, "mandate": short name | None}.

    `action` is one of the tool's words (read, list, search, write, delete, send, run, install, spend, other)."""
    holder = holder_for(device)
    revoked, now = compiler.revocations(), compiler.now()
    mine = [m for m in compiler.mandates() if m.draft.holder == holder]
    live = [m for m in mine if compiler.state_of(m, revoked, now) == "active"]
    if not live:
        ended = [compiler.state_of(m, revoked, now) for m in mine]
        if "stopped" in ended or "expired" in ended:
            # It had one, and the person stopped it or it ran out: that is an answer, not "no opinion".
            return {"has_mandate": True, "decision": "deny", "reason": "revoked" if "stopped" in ended else "expired", "mandate": None}
        return {"has_mandate": False, "decision": "ask", "reason": "", "mandate": None}
    mapped = TOOL_ACTIONS.get(action)
    target = _resource(mapped, resource) if mapped else None
    if mapped is None or target is None or amount is not None:
        # Sending, installing, paying, or something not named as a folder: no permission can be checked for it yet.
        return {"has_mandate": True, "decision": "ask", "reason": "not_covered", "mandate": live[0].id[3:15]}
    spent = compiler.store.spent()
    best: dict[str, Any] | None = None
    for mandate in live:
        request = P.Request(holder=holder, action=mapped, resource=target, privacy=mandate.privacy())
        decision = P.check(mandate, request, compiler.usage(mandate), revoked, spent, now)
        name = mandate.id[3:15]
        if decision.allowed:
            return {"has_mandate": True, "decision": "allow", "reason": "", "mandate": name}
        if decision.kind == "needs_confirmation":
            best = {"has_mandate": True, "decision": "ask", "reason": "needs_confirmation", "mandate": name}
        elif best is None or (best["decision"] == "ask" and best["reason"] == "not_covered"):
            soft = decision.detail in NOT_COVERED
            best = {"has_mandate": True, "decision": "ask" if soft else "deny", "reason": "not_covered" if soft else decision.detail,
                    "mandate": name}
    assert best is not None
    return best
