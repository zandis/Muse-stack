"""The values a permission is made of, and the rules for each of them.

The specification is docs/spec/interfaces/permission-token.md, section 3. This file
is the twin of core/src/permissions/model.rs: same names, same rules, same error codes.
"""

from __future__ import annotations

import dataclasses
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

SCHEMA_NAME = "muse.permission"
SCHEMA_VERSION = 1
MAX_TIME = 253_402_300_799  # 9999-12-31T23:59:59Z
DEFAULT_SKEW = 60
MAX_SKEW = 300
MAX_SINGLE_USE_TTL = 86_400
INT64_MAX = 2**63 - 1

RESOURCE_KINDS = ("folder", "host", "calendar", "record_kind", "provider", "home")
HOLDER_KINDS = ("device", "helper", "app", "clinician")
READ_ACTIONS = ("files.read", "mail.read", "calendar.read", "health.read", "records.read", "llm.call")
SPEND_ACTIONS = ("pay.spend", "llm.paid")
PRIVACY = ("local", "private", "standard", "public")  # strictest first; the index is the rank

_IDENT = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:@+-")
_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGIT = frozenset("0123456789")
_HEX = frozenset("0123456789abcdef")


class PermissionError(Exception):  # noqa: A001 - the name the Rust twin uses; never caught as the builtin here
    """A failure with a machine-readable `code` (the same strings as the Rust core)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, order=True)
class Resource:
    kind: str
    value: str

    def covers(self, other: "Resource") -> bool:
        """True if a permission for `self` includes `other` (segment-aware for folders)."""
        if self.kind != other.kind:
            return False
        if self.value == "*" or self.value == other.value:
            return True
        if other.value == "*":
            return False
        return self.kind == "folder" and other.value.startswith(self.value + "/")

    def intersect(self, other: "Resource") -> "Resource | None":
        if self.covers(other):
            return other
        if other.covers(self):
            return self
        return None


@dataclass(frozen=True, order=True)
class Limit:
    kind: str
    unit: str
    decimals: int
    period: str
    amount: int


@dataclass(frozen=True, order=True)
class NarrowLimit:
    """A lower limit added by narrowing (decimals come from the authority block)."""

    kind: str
    unit: str
    period: str
    amount: int


@dataclass(frozen=True, order=True)
class Cap:
    """The most a single request may carry."""

    kind: str
    unit: str
    amount: int


@dataclass(frozen=True)
class Confirm:
    """`each_write`, `never`, or `over_amount` with a unit, its decimals and the threshold."""

    rule: str
    unit: str = ""
    decimals: int = 0
    amount: int = 0


EACH_WRITE = Confirm("each_write")
NEVER = Confirm("never")


@dataclass(frozen=True)
class Draft:
    """The structured form a person approves. `propose_from_fields` returns it validated and sorted."""

    issuer: str
    holder: str
    actions: tuple[str, ...]
    resources: tuple[Resource, ...]
    not_before: int
    not_after: int
    issued_at: int
    routine: tuple[str, ...] = ()
    limits: tuple[Limit, ...] = ()
    purpose_text: str = ""
    purpose_code: str = ""
    confirm: Confirm = EACH_WRITE
    privacy: str = "local"
    period_zone: str = "UTC"


@dataclass(frozen=True)
class Narrowing:
    """What one appended block restricts. Empty fields restrict nothing."""

    actions: tuple[str, ...] = ()
    resources: tuple[Resource, ...] = ()
    limits: tuple[NarrowLimit, ...] = ()
    caps: tuple[Cap, ...] = ()
    not_before: int | None = None
    not_after: int | None = None
    privacy: str | None = None
    confirm: Confirm | None = None
    job: str | None = None
    tool: str | None = None
    card: str | None = None


@dataclass(frozen=True, order=True)
class Amount:
    kind: str
    unit: str
    value: int


@dataclass(frozen=True)
class Request:
    holder: str
    action: str
    resource: Resource
    privacy: str
    amounts: tuple[Amount, ...] = field(default=())
    job: str | None = None
    tool: str | None = None
    card: str | None = None


# ---------------------------------------------------------------- text rules


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_safe_text(s: object, max_chars: int) -> bool:
    """No double quote, backslash or control character; at most `max_chars` characters."""
    if not isinstance(s, str) or len(s) > max_chars:
        return False
    try:
        s.encode("utf-8")  # a lone surrogate is not text
    except UnicodeEncodeError:
        return False
    return not any(c in '"\\' or unicodedata.category(c) == "Cc" for c in s)


def is_identifier(s: object) -> bool:
    return isinstance(s, str) and 1 <= len(s) <= 128 and all(c in _IDENT for c in s)


def is_action(s: object) -> bool:
    if not isinstance(s, str) or len(s) > 64:
        return False
    parts = s.split(".")
    return len(parts) >= 2 and all(p and p[0] in _LOWER and all(c in _LOWER or c in _DIGIT or c == "_" for c in p) for p in parts)


def is_holder(s: object) -> bool:
    if not isinstance(s, str) or ":" not in s:
        return False
    kind, ident = s.split(":", 1)
    return kind in HOLDER_KINDS and is_identifier(ident)


def is_lower_hex(s: str) -> bool:
    return all(c in _HEX for c in s)


def is_issuer(s: object) -> bool:
    return isinstance(s, str) and s.startswith("ed25519/") and len(s) == 72 and is_lower_hex(s[8:])


def is_revocation_id(s: object) -> bool:
    return isinstance(s, str) and len(s) == 128 and is_lower_hex(s)


def is_folder(s: object) -> bool:
    if not is_safe_text(s, 1024) or len(s.encode("utf-8")) > 1024 or not s.startswith("/"):  # type: ignore[union-attr]
        return False
    return all(seg not in ("", ".", "..") for seg in s[1:].split("/"))  # type: ignore[index]


def is_host(s: object) -> bool:
    if not isinstance(s, str):
        return False
    name, sep, port = s.partition(":")
    edge = lambda c: c in _LOWER or c in _DIGIT  # noqa: E731
    return (
        1 <= len(name) <= 253
        and edge(name[0])
        and edge(name[-1])
        and all(edge(c) or c in ".-" for c in name)
        and (not sep or (1 <= len(port) <= 5 and all(c in _DIGIT for c in port)))
    )


def is_zone(s: object) -> bool:
    return isinstance(s, str) and 1 <= len(s) <= 64 and all(c in _UPPER or c in _LOWER or c in _DIGIT or c in "_+/-" for c in s)


def is_currency(s: object) -> bool:
    return isinstance(s, str) and 3 <= len(s) <= 10 and s[0] in _UPPER and all(c in _UPPER or c in _DIGIT for c in s)


def is_time(t: object) -> bool:
    return _is_int(t) and 0 <= t <= MAX_TIME  # type: ignore[operator]


def is_amount(n: object) -> bool:
    return _is_int(n) and 0 <= n <= INT64_MAX  # type: ignore[operator]


def is_resource(r: object, allow_any: bool) -> bool:
    """`value` may be `*` only where `allow_any` (a permission); a request names one resource."""
    if not isinstance(r, Resource) or r.kind not in RESOURCE_KINDS or not isinstance(r.value, str):
        return False
    if r.value == "*":
        return allow_any
    if r.kind == "folder":
        return is_folder(r.value)
    if r.kind == "host":
        return is_host(r.value)
    return r.value != "" and is_safe_text(r.value, 128)


def is_counter(kind: object, unit: object) -> bool:
    """`spend` needs a currency; `requests` and `messages` have the empty unit."""
    if kind == "spend":
        return is_currency(unit)
    return kind in ("requests", "messages") and unit == ""


def is_period(s: object) -> bool:
    return s in ("day", "month")


def action_class(action: str) -> str:
    """`read`, `write` or `spend`; a name the registry does not know is `write`."""
    if action in READ_ACTIONS:
        return "read"
    if action in SPEND_ACTIONS:
        return "spend"
    return "write"


def privacy_rank(name: object) -> int | None:
    return PRIVACY.index(name) if isinstance(name, str) and name in PRIVACY else None


def _sorted_unique(items: Iterable) -> tuple | None:
    try:
        items = tuple(items)
        unique = set(items)
        return tuple(sorted(unique)) if len(unique) == len(items) else None
    except TypeError:
        return None


def _valid_confirm(confirm: object, limits: Iterable[Limit]) -> bool:
    if not isinstance(confirm, Confirm) or confirm.rule not in ("each_write", "over_amount", "never"):
        return False
    if confirm.rule != "over_amount":
        return confirm.unit == "" and confirm.decimals == 0 and confirm.amount == 0
    return is_amount(confirm.amount) and any(
        l.kind == "spend" and l.unit == confirm.unit and l.decimals == confirm.decimals for l in limits
    )


def _valid_limits(limits: tuple) -> bool:
    keys = set()
    for l in limits:
        if not (
            isinstance(l, Limit)
            and is_counter(l.kind, l.unit)
            and is_period(l.period)
            and is_amount(l.amount)
            and _is_int(l.decimals)
            and 0 <= l.decimals <= 18
            and (l.kind == "spend" or l.decimals == 0)
            and all(m.kind != l.kind or m.unit != l.unit or m.decimals == l.decimals for m in limits)
        ):
            return False
        key = (l.kind, l.unit, l.period)
        if key in keys:
            return False
        keys.add(key)
    return True


# ---------------------------------------------------------------- drafts


def propose_from_fields(fields: Draft) -> Draft:
    """Validate the fields of a permission and return them in canonical order.

    Turning a person's free text into these fields is the helper's job, not this function's.
    """
    d = fields
    if not is_issuer(d.issuer):
        raise PermissionError("invalid_issuer", "issuer must be ed25519/<64 hex>")
    if not is_holder(d.holder):
        raise PermissionError("invalid_holder", "holder must be <device|helper|app|clinician>:<id>")
    actions = _sorted_unique(d.actions)
    if actions is None or not 1 <= len(actions) <= 32 or not all(is_action(a) for a in actions):
        raise PermissionError("invalid_actions", "1 to 32 distinct dotted action names")
    routine = _sorted_unique(d.routine)
    if routine is None or not all(a in actions for a in routine):
        raise PermissionError("invalid_routine", "routine actions must be distinct and listed in actions")
    resources = _sorted_unique(d.resources) if all(isinstance(r, Resource) for r in d.resources) else None
    if resources is None or not 1 <= len(resources) <= 64 or not all(is_resource(r, True) for r in resources):
        raise PermissionError("invalid_resources", "1 to 64 distinct resources of a known kind")
    limits = _sorted_unique(d.limits) if all(isinstance(l, Limit) for l in d.limits) else None
    if limits is None or len(limits) > 16 or not _valid_limits(limits):
        raise PermissionError("invalid_limits", "limits need a known kind, unit, period and a non-negative amount, once per key")
    code = d.purpose_code
    if not is_safe_text(d.purpose_text, 500) or not (code == "" or (is_identifier(code) and len(code) <= 64)):
        raise PermissionError("invalid_purpose", "purpose text: no double quote, backslash or control character; code: an identifier")
    if not (is_time(d.issued_at) and is_time(d.not_before) and is_time(d.not_after) and d.not_before < d.not_after):
        raise PermissionError("invalid_validity", "not_before must be before not_after, both within years 1970 to 9999")
    if not _valid_confirm(d.confirm, limits):
        raise PermissionError("invalid_confirm", "over_amount needs a spend limit in the same unit and decimals")
    if d.confirm.rule == "never" and not all(action_class(a) == "read" or a in routine for a in actions):
        raise PermissionError("invalid_confirm", "never is only for read actions and actions marked routine")
    if privacy_rank(d.privacy) is None:
        raise PermissionError("invalid_privacy", "privacy is local, private, standard or public")
    if not is_zone(d.period_zone):
        raise PermissionError("invalid_zone", "period_zone must be an IANA zone name")
    return dataclasses.replace(d, actions=actions, routine=routine, resources=resources, limits=limits)


def canonical_narrowing(n: Narrowing, authority: Draft) -> Narrowing:
    """Shape rules for one block; returns it in canonical order.

    Whether it is narrower than a given token is `narrow`'s question.
    """

    def bad(what: str) -> PermissionError:
        return PermissionError("invalid_narrowing", what)

    actions = _sorted_unique(n.actions)
    if actions is None or len(actions) > 32 or not all(is_action(a) for a in actions):
        raise bad("actions")
    resources = _sorted_unique(n.resources) if all(isinstance(r, Resource) for r in n.resources) else None
    if resources is None or len(resources) > 64 or not all(is_resource(r, True) for r in resources):
        raise bad("resources")
    limited = {(l.kind, l.unit) for l in authority.limits}
    limits = _sorted_unique(n.limits) if all(isinstance(l, NarrowLimit) for l in n.limits) else None
    if limits is None:
        raise bad("limits")
    keys: set = set()
    for l in limits:
        key = (l.kind, l.unit, l.period)
        if (l.kind, l.unit) not in limited or not is_period(l.period) or not is_amount(l.amount) or key in keys:
            raise bad("limits")
        keys.add(key)
    caps = _sorted_unique(n.caps) if all(isinstance(c, Cap) for c in n.caps) else None
    if caps is None:
        raise bad("caps")
    keys = set()
    for c in caps:
        if (c.kind, c.unit) not in limited or not is_amount(c.amount) or (c.kind, c.unit) in keys:
            raise bad("caps")
        keys.add((c.kind, c.unit))
    if not (n.not_before is None or is_time(n.not_before)) or not (n.not_after is None or is_time(n.not_after)):
        raise bad("validity")
    if not (n.privacy is None or privacy_rank(n.privacy) is not None):
        raise bad("privacy")
    if n.confirm is not None:
        if isinstance(n.confirm, Confirm) and n.confirm.rule == "never":
            raise bad("confirm: never is not a narrowing")
        if not _valid_confirm(n.confirm, authority.limits):
            raise bad("confirm")
    for ident in (n.job, n.tool, n.card):
        if ident is not None and not is_identifier(ident):
            raise bad("job, tool and card must be identifiers")
    if (n.tool is not None or n.card is not None) and n.not_after is None:
        raise bad("a block bound to a tool call or a card must carry an end time")
    out = dataclasses.replace(n, actions=actions, resources=resources, limits=limits, caps=caps)
    if out == Narrowing():
        raise PermissionError("empty_narrowing", "a block must restrict something")
    return out


def valid_request(r: Request) -> bool:
    """Well-formedness of a request (step 5 of the check)."""
    if not (isinstance(r, Request) and is_holder(r.holder) and is_action(r.action) and is_resource(r.resource, False)):
        return False
    if privacy_rank(r.privacy) is None:
        return False
    keys = set()
    for a in r.amounts:
        if not (isinstance(a, Amount) and is_counter(a.kind, a.unit) and is_amount(a.value)) or (a.kind, a.unit) in keys:
            return False
        keys.add((a.kind, a.unit))
    return all(ident is None or is_identifier(ident) for ident in (r.job, r.tool, r.card))
