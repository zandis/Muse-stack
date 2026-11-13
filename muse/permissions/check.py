"""Deciding one request (interface document, section 5). Pure: the clock, the usage
figures, the revocation list and the spent set are all arguments.

Twin of core/src/permissions/check.rs. A decision is a tuple:
`("allow", "")`, `("deny", reason)` or `("needs_confirmation", rule)`.
"""

from __future__ import annotations

from typing import Iterable, Mapping, NamedTuple

from muse.permissions.lists import RevocationList, SpentSet
from muse.permissions.model import (
    DEFAULT_SKEW,
    INT64_MAX,
    MAX_SINGLE_USE_TTL,
    MAX_SKEW,
    PRIVACY,
    Amount,
    Confirm,
    PermissionError,
    Request,
    action_class,
    is_time,
    valid_request,
)
from muse.permissions.token import Mandate, engine_allows, revocation_ids, unverified_revocation_ids


class Decision(NamedTuple):
    kind: str  # "allow" | "deny" | "needs_confirmation"
    detail: str = ""  # the reason for a deny; the rule that asks for a confirmation

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"


ALLOW = Decision("allow")


def deny(reason: str) -> Decision:
    return Decision("deny", reason)


# "Used so far this period" for (kind, unit, period), supplied by the enforcing module
# from its ledger for this mandate id. The token cannot check these figures.
Usage = Mapping[tuple[str, str, str], int]


def spent_key(mandate_id: str, kind: str, ident: str) -> str:
    """The key a single-use id is remembered under."""
    return f"{mandate_id}/{kind}/{ident}"


def single_use_keys(mandate: Mandate, skew: int = DEFAULT_SKEW) -> list[tuple[str, int]]:
    """The single-use ids a request spends when it is allowed, with the time each may be forgotten."""
    until = min(mandate.not_after() + min(max(skew, 0), MAX_SKEW), INT64_MAX)
    return [(spent_key(mandate.id, "tool", i), until) for i in mandate.tools()] + [
        (spent_key(mandate.id, "card", i), until) for i in mandate.cards()
    ]


def _single_use_ends(m: Mandate) -> list[int]:
    """For each block that binds a tool call or a card: the end time in force at that block.
    A later block cannot change it, so shortening a token afterwards never turns
    `single_use_too_long` into allow."""
    end = m.draft.not_after
    out = []
    for level in m.levels:
        if level.not_after is not None:
            end = min(end, level.not_after)
        if level.tool is not None or level.card is not None:
            out.append(end)
    return out


def _asks(rules: Iterable[Confirm], klass: str, routine: bool, amounts: Iterable[Amount]) -> str | None:
    """Which confirmation rule, if any, asks for this request (section 5.1)."""
    over = False
    for rule in rules:
        if rule.rule == "each_write":
            if klass != "read" and not routine:
                return "each_write"
        elif rule.rule == "over_amount":
            if klass == "write" and not routine:
                return "each_write"
            if klass == "spend" and any(a.kind == "spend" and (a.unit != rule.unit or a.value > rule.amount) for a in amounts):
                over = True
    return "over_amount" if over else None


def check(
    mandate: Mandate,
    request: Request,
    usage: Usage,
    revoked: RevocationList,
    spent: SpentSet,
    now: int,
    skew: int = DEFAULT_SKEW,
) -> Decision:
    """Allow, deny with a reason, or ask for confirmation. Steps and reasons are those of
    the interface document, in that order."""
    m, req = mandate, request
    skew = min(max(skew, 0), MAX_SKEW)
    if any(revoked.contains(i) for i in revocation_ids(m)):
        return deny("revoked")
    if not valid_request(req) or not is_time(now):
        return deny("bad_request")
    if req.holder != m.draft.holder:
        return deny("wrong_holder")
    if now + skew < m.not_before():
        return deny("not_yet_valid")
    if now - skew > m.not_after():
        return deny("expired")
    if req.action not in m.draft.actions or not all(not l.actions or req.action in l.actions for l in m.levels):
        return deny("action_not_allowed")

    def covers(resources: Iterable) -> bool:
        return any(r.covers(req.resource) for r in resources)

    if not covers(m.draft.resources) or not all(not l.resources or covers(l.resources) for l in m.levels):
        return deny("resource_not_allowed")
    if PRIVACY.index(req.privacy) > PRIVACY.index(m.privacy()):
        return deny("privacy_not_allowed")
    for bound, given, reason in ((m.jobs(), req.job, "wrong_job"), (m.tools(), req.tool, "wrong_tool"), (m.cards(), req.card, "wrong_card")):
        if not all(i == given for i in bound):
            return deny(reason)
    single_use = single_use_keys(m, skew)
    if single_use:
        if any(end - now > MAX_SINGLE_USE_TTL + skew for end in _single_use_ends(m)):
            return deny("single_use_too_long")
        if any(spent.contains(key, now) for key, _ in single_use):
            return deny("already_used")
    klass = action_class(req.action)
    if klass == "spend" and not any(a.kind == "spend" for a in req.amounts):
        return deny("amount_required")
    limits = m.limits()
    caps = m.caps()
    used_facts = []
    for a in req.amounts:
        if not any(l.kind == a.kind and l.unit == a.unit for l in m.draft.limits):
            return deny("limit_missing")
        for l in limits:
            if l.kind != a.kind or l.unit != a.unit:
                continue
            used = usage.get((l.kind, l.unit, l.period))
            if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= INT64_MAX:
                return deny("usage_missing")
            if used + a.value > min(l.amount, INT64_MAX):
                return deny("over_limit")
            used_facts.append((l.kind, l.unit, l.period, used))
        if any(c.kind == a.kind and c.unit == a.unit and a.value > c.amount for c in caps):
            return deny("over_cap")
    rules = m.confirm_rules()
    approved = bool(m.cards())
    if not approved:
        rule = _asks(rules, klass, req.action in m.draft.routine, req.amounts)
        if rule is not None:
            return Decision("needs_confirmation", rule)
    if not engine_allows(m, req, used_facts, rules, now, skew):
        return deny("engine_disagreement")
    return ALLOW


def check_encoded(
    token: str,
    root: str,
    request: Request,
    usage: Usage,
    revoked: RevocationList,
    spent: SpentSet,
    now: int,
    skew: int = DEFAULT_SKEW,
) -> Decision:
    """`check` from the encoded token: decoding, revocation, signature and structure first."""
    try:
        ids = unverified_revocation_ids(token)
    except PermissionError as exc:
        if exc.code == "library_missing":
            raise
        return deny("malformed_token")
    if any(revoked.contains(i) for i in ids):
        return deny("revoked")
    try:
        mandate = Mandate.parse(token, root)
    except PermissionError as exc:
        return deny(exc.code)
    return check(mandate, request, usage, revoked, spent, now, skew)


def check_and_record(
    mandate: Mandate,
    request: Request,
    usage: Usage,
    revoked: RevocationList,
    spent: SpentSet,
    now: int,
    skew: int = DEFAULT_SKEW,
) -> Decision:
    """`check`, and when the answer is allow, remember the token's single-use ids.

    A full spent set refuses the request: forgetting a live id would allow a replay.
    """
    decision = check(mandate, request, usage, revoked, spent, now, skew)
    if decision.allowed:
        keys = single_use_keys(mandate, skew)
        if not spent.has_room(len(keys), now):
            return deny("spent_set_full")
        for key, until in keys:
            spent.insert(key, until, now)
    return decision
