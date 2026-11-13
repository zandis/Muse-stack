"""What one permission needs made, per enforcing system. Pure: a function of the token's own facts.

    plan(mandate) -> [ArtifactSpec]

Targets (architecture section 2.8):

  skills      (built)      a narrowed token per skill the helper may run, and the folders the permission
                           names put on the skill-folders list
  router      (built)      an app entry with the permission's caps, for an app or a device (a host bot)
  records     (built)      a consent record; a revocation record when the permission is stopped
  payments    (NOT built)  a spending limit on chain
  connectors  (NOT built)  the narrowest scope a mail or calendar service offers
  health      (NOT built)  a FHIR Consent

A target the permission has nothing to do with is left out. A target it needs but that is not built
is in the plan as `not_built` with a plain reason, so the Permissions screen can show a grey mark
and never a false green one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

TARGETS = ("skills", "router", "records", "payments", "connectors", "health")
BUILT = ("skills", "router", "records")

SKILL_ACTIONS = ("files.read", "files.write", "llm.call", "llm.paid", "skills.run")
ROUTER_ACTIONS = ("llm.call", "llm.paid")
SKILL_PREFIX = "skill/"  # a `home` resource `skill/<name>` names one skill; `home:*` or none named means any skill
PRIORITY_CODE = "router.priority."  # purpose code `router.priority.background` sets the app's priority
DEFAULT_PRIORITY = "normal"

NOT_BUILT = {
    "payments": "Paying from this permission is not built yet: no spending limit exists on chain, and Muse makes no payment under it.",
    "connectors": "Connecting mail or a calendar is not built yet, so nothing can use this part of the permission.",
    "health": "The health add-on is not built yet, so nothing can use this part of the permission.",
}


@dataclass(frozen=True)
class ArtifactSpec:
    """One thing to make (or, for `not_built`, to say honestly that nothing was made)."""

    mandate_id: str
    target: str
    kind: str  # app | skill_token | skills_on_demand | folder | consent_record | not_built
    key: str  # stable inside one permission: `app`, `skill:<name>`, `folder:<path>`, `consent`
    status: str = "planned"  # planned | not_built
    reason: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)


def app_name(mandate_id: str) -> str:
    """The router app made for a permission: `perm-` and the first 12 hex digits of its id."""
    return f"perm-{mandate_id[3:15]}"


def folder_name(mandate_id: str, index: int) -> str:
    return f"perm-{mandate_id[3:11]}-{index}"


def skill_job(skill: str) -> str:
    """The job id a skill's narrowed token is bound to, and that the skill's requests carry."""
    return f"skill.{skill}"


def named_skills(mandate: Any) -> tuple[str, ...] | None:
    """The skills a permission names (`home:skill/<name>`), or None when it does not restrict them."""
    home = [r.value for r in mandate.draft.resources if r.kind == "home"]
    names = tuple(sorted(v[len(SKILL_PREFIX):] for v in home if v.startswith(SKILL_PREFIX) and len(v) > len(SKILL_PREFIX)))
    return names or None


def covers_skill(mandate: Any, skill: str) -> bool:
    names = named_skills(mandate)
    return names is None or skill in names


def _limit(mandate: Any, kind: str, period: str, unit: str = "") -> int | None:
    for l in mandate.limits():
        if l.kind == kind and l.period == period and l.unit == unit:
            return l.amount
    return None


def plan(mandate: Any) -> list[ArtifactSpec]:
    """Every artifact this permission needs, in a fixed order. `mandate` is an issued (not narrowed) permission."""
    d = mandate.draft
    mid = mandate.id
    kind = d.holder.split(":", 1)[0]
    actions = set(d.actions)
    out: list[ArtifactSpec] = []

    if kind == "helper" and actions & set(SKILL_ACTIONS):
        names = named_skills(mandate)
        if names is None:
            out.append(ArtifactSpec(mid, "skills", "skills_on_demand", "skills", params={}))
        else:
            out += [ArtifactSpec(mid, "skills", "skill_token", f"skill:{name}", params={"skill": name}) for name in names]
        if actions & {"files.read", "files.write"}:
            folders = sorted(r.value for r in d.resources if r.kind == "folder")
            for index, value in enumerate(folders):
                if value == "*":
                    out.append(ArtifactSpec(mid, "skills", "folder", "folder:*", status="pending",
                                            reason="This permission says any folder. A run through the home is given folders by "
                                                   "name, so name the folders in the permission to put them on the list.",
                                            params={}))
                    continue
                out.append(ArtifactSpec(mid, "skills", "folder", f"folder:{value}",
                                        params={"path": value, "output": "files.write" in actions, "name": folder_name(mid, index)}))

    if kind in ("app", "device") and actions & set(ROUTER_ACTIONS):
        code = d.purpose_code
        priority = code[len(PRIORITY_CODE):] if code.startswith(PRIORITY_CODE) else DEFAULT_PRIORITY
        if priority not in ("interactive", "normal", "background"):
            priority = DEFAULT_PRIORITY
        paid = "llm.paid" in actions
        note = ""
        if paid:
            note = ("Paid services are switched on for this app. The router does not hold it to this permission's own amount "
                    "yet: the home's daily paid cap applies.")
        out.append(ArtifactSpec(mid, "router", "app", "app", reason=note, params={
            "name": app_name(mid), "priority": priority, "daily_requests": _limit(mandate, "requests", "day"),
            "daily_tokens": None, "allow_paid": paid, "default_privacy": mandate.privacy(),
            "valid_from": mandate.not_before(), "valid_until": mandate.not_after()}))

    out.append(ArtifactSpec(mid, "records", "consent_record", "consent", params={}))

    if "pay.spend" in actions:
        out.append(ArtifactSpec(mid, "payments", "not_built", "spending_limit", status="not_built", reason=NOT_BUILT["payments"]))
    if any(a.split(".")[0] in ("mail", "calendar") for a in actions):
        out.append(ArtifactSpec(mid, "connectors", "not_built", "scope", status="not_built", reason=NOT_BUILT["connectors"]))
    if any(a.split(".")[0] == "health" for a in actions):
        out.append(ArtifactSpec(mid, "health", "not_built", "consent", status="not_built", reason=NOT_BUILT["health"]))
    return out
