"""Permission <-> Biscuit token: the block text, issuing, reading back, narrowing, and
the authorizer that runs the token's own Datalog.

Twin of core/src/permissions/token.rs. One function per line format writes both the
text Biscuit prints for it and the parameterised source it is built from, so values
never pass through string formatting into Datalog. Reading goes the other way: parse
the printed block, then require that printing the parsed value gives the same bytes.

All cryptography is the Biscuit library's (`biscuit-python`, which wraps the same Rust
crate the core uses). It is imported on first use, so the rest of the package loads
without it.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
from typing import Any, Iterable

from wardmuse.permissions.model import (
    EACH_WRITE,
    MAX_TIME,
    PRIVACY,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    Cap,
    Confirm,
    Draft,
    Limit,
    NarrowLimit,
    Narrowing,
    PermissionError,
    Request,
    Resource,
    canonical_narrowing,
    is_issuer,
    privacy_rank,
    propose_from_fields,
)

BISCUIT_PYTHON_VERSION = "0.4.0"  # the pin in pyproject.toml; wraps biscuit-auth 6.0.0
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _lib() -> Any:
    try:
        import biscuit_auth
    except ImportError as exc:  # pragma: no cover - exercised when the extra is absent
        raise PermissionError(
            "library_missing", f'permissions need biscuit-python: pip install "biscuit-python=={BISCUIT_PYTHON_VERSION}"'
        ) from exc
    return biscuit_auth


def available() -> bool:
    """True if the Biscuit library can be imported."""
    try:
        _lib()
    except PermissionError:
        return False
    return True


# ---------------------------------------------------------------- writing block text


class Code:
    """Datalog being written twice: `text` as Biscuit prints it, `source` with `{pN}` where a string goes."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.source: list[str] = []
        self.params: dict[str, Any] = {}

    def raw(self, s: str) -> "Code":
        self.text.append(s)
        self.source.append(s)
        return self

    def string(self, s: str) -> "Code":
        name = f"p{len(self.params)}"
        self.text.append(f'"{s}"')
        self.source.append("{" + name + "}")
        self.params[name] = s
        return self

    def int(self, n: int) -> "Code":
        return self.raw(str(n))

    def date(self, t: int) -> "Code":
        return self.raw(rfc3339(t))

    def end(self) -> "Code":
        return self.raw(";\n")

    def printed(self) -> str:
        return "".join(self.text)

    def built(self) -> str:
        return "".join(self.source)


def rfc3339(t: int) -> str:
    """`2026-11-30T00:00:00Z`, the form Biscuit prints a date in."""
    return (_EPOCH + _dt.timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _confirm_args(c: Code, confirm: Confirm) -> None:
    if confirm.rule == "over_amount":
        c.string("over_amount").raw(", ").string(confirm.unit).raw(", ").int(confirm.decimals).raw(", ").int(confirm.amount)
    else:
        c.string(confirm.rule).raw(", ").string("").raw(", 0, 0")


def authority_code(d: Draft, mandate: str | None) -> Code:
    """The authority block for `d`; without the `mandate` line when `mandate` is None
    (that text is what the id is the hash of)."""
    c = Code()
    c.raw("schema(").string(SCHEMA_NAME).raw(", ").int(SCHEMA_VERSION).raw(")").end()
    if mandate is not None:
        c.raw("mandate(").string(mandate).raw(")").end()
    c.raw("issuer(").string(d.issuer).raw(")").end()
    c.raw("holder(").string(d.holder).raw(")").end()
    c.raw("issued_at(").date(d.issued_at).raw(")").end()
    c.raw("valid_from(").date(d.not_before).raw(")").end()
    c.raw("valid_until(").date(d.not_after).raw(")").end()
    c.raw("period_zone(").string(d.period_zone).raw(")").end()
    c.raw("privacy_max(").int(PRIVACY.index(d.privacy)).raw(")").end()
    c.raw("purpose(").string(d.purpose_text).raw(", ").string(d.purpose_code).raw(")").end()
    c.raw("confirm(")
    _confirm_args(c, d.confirm)
    c.raw(")").end()
    for a in d.actions:
        c.raw("action(").string(a).raw(")").end()
    for a in d.routine:
        c.raw("routine(").string(a).raw(")").end()
    for r in d.resources:
        c.raw("resource(").string(r.kind).raw(", ").string(r.value).raw(")").end()
    for l in d.limits:
        c.raw("limit(").string(l.kind).raw(", ").string(l.unit).raw(", ").int(l.decimals).raw(", ").string(l.period).raw(", ").int(l.amount).raw(")").end()
    return c


def mandate_id(draft: Draft) -> str:
    """`m1_` + SHA-256 of the draft's authority block text without the `mandate` line."""
    return "m1_" + hashlib.sha256(authority_code(draft, None).printed().encode("utf-8")).hexdigest()


def narrowing_code(n: Narrowing) -> Code:
    """The checks of one narrowing block, in canonical order."""
    c = Code()
    if n.actions:
        c.raw("check if ")
        for i, a in enumerate(n.actions):
            c.raw("req_action(" if i == 0 else " or req_action(").string(a).raw(")")
        c.end()
    if n.resources:
        c.raw("check if ")
        for i, r in enumerate(n.resources):
            c.raw("req_resource(" if i == 0 else " or req_resource(").string(r.kind)
            if r.value == "*":
                c.raw(", $v)")
            else:
                c.raw(", ").string(r.value).raw(")")
                if r.kind == "folder":
                    c.raw(" or req_resource(").string("folder").raw(", $v), $v.starts_with(").string(r.value + "/").raw(")")
        c.end()
    for l in n.limits:
        c.raw("check if no_amount(").string(l.kind).raw(", ").string(l.unit).raw(") or amount(").string(l.kind).raw(", ").string(l.unit)
        c.raw(", $a), used(").string(l.kind).raw(", ").string(l.unit).raw(", ").string(l.period).raw(", $n), $n + $a <= ").int(l.amount).end()
    for cap in n.caps:
        c.raw("check if no_amount(").string(cap.kind).raw(", ").string(cap.unit).raw(") or amount(").string(cap.kind).raw(", ").string(cap.unit)
        c.raw(", $a), $a <= ").int(cap.amount).end()
    if n.not_before is not None:
        c.raw("check if time_late($t), $t >= ").date(n.not_before).end()
    if n.not_after is not None:
        c.raw("check if time($t), $t <= ").date(n.not_after).end()
    if n.privacy is not None:
        c.raw("check if req_privacy($p), $p <= ").int(PRIVACY.index(n.privacy)).end()
    if n.confirm is not None:
        if n.confirm.rule == "over_amount":
            c.raw("check if confirm_applied(").string("over_amount").raw(", ").string(n.confirm.unit).raw(", ")
            c.int(n.confirm.decimals).raw(", ").int(n.confirm.amount).raw(")").end()
        else:
            c.raw("check if confirm_applied(").string(n.confirm.rule).raw(")").end()
    for name, ident in (("req_job", n.job), ("req_tool", n.tool), ("req_card", n.card)):
        if ident is not None:
            c.raw("check if ").raw(name).raw("(").string(ident).raw(")").end()
    return c


# ---------------------------------------------------------------- reading block text


class _Stop(Exception):
    """A line that is not one this schema writes."""


class _Cur:
    """A cursor over one printed line. A string is read up to the next double quote:
    honest blocks never hold one inside a string (the text rules), and the caller
    re-prints what was read and compares, so anything else is refused."""

    def __init__(self, line: str) -> None:
        self.s = line

    def eat(self, lit: str) -> bool:
        if self.s.startswith(lit):
            self.s = self.s[len(lit):]
            return True
        return False

    def need(self, lit: str) -> None:
        if not self.eat(lit):
            raise _Stop

    def string(self) -> str:
        if not self.s.startswith('"'):
            raise _Stop
        end = self.s.find('"', 1)
        if end < 0:
            raise _Stop
        out, self.s = self.s[1:end], self.s[end + 1:]
        return out

    def int(self) -> int:
        n = 0
        while n < len(self.s) and self.s[n] in "0123456789":
            n += 1
        if n == 0 or int(self.s[:n]) > 2**63 - 1:
            raise _Stop
        out, self.s = int(self.s[:n]), self.s[n:]
        return out

    def date(self) -> int:
        text = self.s[:20]
        t = parse_rfc3339(text)
        if t is None:
            raise _Stop
        self.s = self.s[20:]
        return t

    def done(self, tail: str) -> None:
        if self.s != tail:
            raise _Stop


def parse_rfc3339(text: str) -> int | None:
    """The second that prints as exactly `text`, or None."""
    if len(text) != 20 or not text.isascii():
        return None
    digits = text[0:4] + text[5:7] + text[8:10] + text[11:13] + text[14:16] + text[17:19]
    if not all(c in "0123456789" for c in digits):
        return None
    try:
        when = _dt.datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]), int(text[11:13]), int(text[14:16]), int(text[17:19]), tzinfo=_dt.timezone.utc)
    except ValueError:
        return None
    t = int((when - _EPOCH).total_seconds())
    return t if 0 <= t <= MAX_TIME and rfc3339(t) == text else None


def _lines(text: str) -> list[str]:
    if not text.endswith("\n"):
        raise _Stop
    return text[:-1].split("\n")


def _parse_confirm(c: _Cur) -> Confirm:
    name = c.string()
    c.need(", ")
    unit = c.string()
    c.need(", ")
    decimals = c.int()
    c.need(", ")
    amount = c.int()
    if name not in ("over_amount", "each_write", "never"):
        raise _Stop
    return Confirm(name, unit, decimals, amount)


def _parse_authority(text: str) -> tuple[str, Draft]:
    mandate = None
    f: dict[str, Any] = {"actions": [], "routine": [], "resources": [], "limits": []}
    for line in _lines(text):
        c = _Cur(line)
        if c.eat("schema("):
            if c.string() != SCHEMA_NAME:
                raise _Stop
            c.need(", ")
            if c.int() != SCHEMA_VERSION:
                raise _Stop
        elif c.eat("mandate("):
            mandate = c.string()
        elif c.eat("issuer("):
            f["issuer"] = c.string()
        elif c.eat("holder("):
            f["holder"] = c.string()
        elif c.eat("issued_at("):
            f["issued_at"] = c.date()
        elif c.eat("valid_from("):
            f["not_before"] = c.date()
        elif c.eat("valid_until("):
            f["not_after"] = c.date()
        elif c.eat("period_zone("):
            f["period_zone"] = c.string()
        elif c.eat("privacy_max("):
            rank = c.int()
            if rank >= len(PRIVACY):
                raise _Stop
            f["privacy"] = PRIVACY[rank]
        elif c.eat("purpose("):
            f["purpose_text"] = c.string()
            c.need(", ")
            f["purpose_code"] = c.string()
        elif c.eat("confirm("):
            f["confirm"] = _parse_confirm(c)
        elif c.eat("action("):
            f["actions"].append(c.string())
        elif c.eat("routine("):
            f["routine"].append(c.string())
        elif c.eat("resource("):
            kind = c.string()
            c.need(", ")
            f["resources"].append(Resource(kind, c.string()))
        elif c.eat("limit("):
            kind = c.string()
            c.need(", ")
            unit = c.string()
            c.need(", ")
            decimals = c.int()
            c.need(", ")
            period = c.string()
            c.need(", ")
            f["limits"].append(Limit(kind, unit, decimals, period, c.int()))
        else:
            raise _Stop
        c.done(");")
    if mandate is None:
        raise _Stop
    # A missing line leaves a value no valid draft has, so validation refuses it (as in the core).
    draft = Draft(
        issuer=f.get("issuer", ""),
        holder=f.get("holder", ""),
        actions=tuple(f["actions"]),
        routine=tuple(f["routine"]),
        resources=tuple(f["resources"]),
        limits=tuple(f["limits"]),
        purpose_text=f.get("purpose_text", ""),
        purpose_code=f.get("purpose_code", ""),
        issued_at=f.get("issued_at", -1),
        not_before=f.get("not_before", -1),
        not_after=f.get("not_after", -1),
        confirm=f.get("confirm", EACH_WRITE),
        privacy=f.get("privacy", "local"),
        period_zone=f.get("period_zone", ""),
    )
    return mandate, draft


def _parse_narrowing(text: str) -> Narrowing:
    f: dict[str, Any] = {"actions": [], "resources": [], "limits": [], "caps": []}
    for line in _lines(text):
        c = _Cur(line)
        if c.eat("check if req_action("):
            while True:
                f["actions"].append(c.string())
                if not c.eat(") or req_action("):
                    break
            c.done(");")
        elif c.eat("check if req_resource("):
            while True:
                kind = c.string()
                if c.eat(", $v), $v.starts_with("):
                    c.string()  # the "under this folder" half; re-printing checks it
                elif c.eat(", $v"):
                    f["resources"].append(Resource(kind, "*"))
                else:
                    c.need(", ")
                    f["resources"].append(Resource(kind, c.string()))
                if not c.eat(") or req_resource("):
                    break
            c.done(");")
        elif c.eat("check if no_amount("):
            kind = c.string()
            c.need(", ")
            unit = c.string()
            c.need(") or amount(")
            c.string()  # the same kind and unit again; re-printing checks them
            c.need(", ")
            c.string()
            if c.eat(", $a), $a <= "):
                f["caps"].append(Cap(kind, unit, c.int()))
            else:
                c.need(", $a), used(")
                c.string()
                c.need(", ")
                c.string()
                c.need(", ")
                period = c.string()
                c.need(", $n), $n + $a <= ")
                f["limits"].append(NarrowLimit(kind, unit, period, c.int()))
            c.done(";")
        elif c.eat("check if time_late($t), $t >= "):
            f["not_before"] = c.date()
            c.done(";")
        elif c.eat("check if time($t), $t <= "):
            f["not_after"] = c.date()
            c.done(";")
        elif c.eat("check if req_privacy($p), $p <= "):
            rank = c.int()
            if rank >= len(PRIVACY):
                raise _Stop
            f["privacy"] = PRIVACY[rank]
            c.done(";")
        elif c.eat('check if confirm_applied("each_write");'):
            f["confirm"] = EACH_WRITE
            c.done("")
        elif c.eat("check if confirm_applied("):
            f["confirm"] = _parse_confirm(c)
            c.done(");")
        elif c.eat("check if req_job("):
            f["job"] = c.string()
            c.done(");")
        elif c.eat("check if req_tool("):
            f["tool"] = c.string()
            c.done(");")
        elif c.eat("check if req_card("):
            f["card"] = c.string()
            c.done(");")
        else:
            raise _Stop
    for name in ("actions", "resources", "limits", "caps"):
        f[name] = tuple(f[name])
    return Narrowing(**f)


# ---------------------------------------------------------------- keys


def parse_public_key(text: str) -> Any:
    if not is_issuer(text):
        raise PermissionError("invalid_key", "a public key is ed25519/<64 hex>")
    try:
        return _lib().PublicKey(text)
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("invalid_key", str(exc)) from exc


def parse_private_key(text: str) -> Any:
    """A root key pair from `ed25519-private/<64 hex>`."""
    ok = isinstance(text, str) and text.startswith("ed25519-private/") and len(text) == 80
    if not ok or not all(c in "0123456789abcdefABCDEF" for c in text[16:]):
        raise PermissionError("invalid_key", "a private key is ed25519-private/<64 hex>")
    lib = _lib()
    try:
        return lib.KeyPair.from_private_key(lib.PrivateKey(text))
    except Exception as exc:
        raise PermissionError("invalid_key", str(exc)) from exc


def generate_private_key() -> str:
    """A new root key as text (the library's random source)."""
    return repr(_lib().KeyPair().private_key)


def public_key_text(root: Any) -> str:
    """The text form of the public half of a root key."""
    return repr(root.public_key)


# ---------------------------------------------------------------- the token


def _merge_ids(levels: Iterable[Narrowing], name: str) -> tuple[str, ...]:
    return tuple(sorted({getattr(n, name) for n in levels if getattr(n, name) is not None}))


@dataclasses.dataclass(frozen=True)
class Mandate:
    """A verified permission token: the signed bytes and what they say."""

    biscuit: Any = dataclasses.field(repr=False, compare=False)
    id: str
    draft: Draft
    levels: tuple[Narrowing, ...] = ()  # one per appended block, oldest first

    @staticmethod
    def parse(token: str, root: str) -> "Mandate":
        """Decode and verify a token under `root`, and read what it says."""
        lib = _lib()
        key = parse_public_key(root)
        try:
            unverified = lib.UnverifiedBiscuit.from_base64(token)
        except Exception as exc:
            raise PermissionError("malformed_token", str(exc)) from exc
        try:
            biscuit = unverified.verify(key)
        except Exception as exc:
            raise PermissionError("bad_signature", str(exc)) from exc
        return _read(biscuit, root)

    def to_base64(self) -> str:
        return self.biscuit.to_base64()

    def size(self) -> int:
        """Size of the token in bytes (before base64)."""
        return len(self.biscuit.to_bytes())

    # -------- the effective permission: authority and every block together

    def not_before(self) -> int:
        return max([self.draft.not_before] + [n.not_before for n in self.levels if n.not_before is not None])

    def not_after(self) -> int:
        return min([self.draft.not_after] + [n.not_after for n in self.levels if n.not_after is not None])

    def privacy(self) -> str:
        return PRIVACY[min([PRIVACY.index(self.draft.privacy)] + [PRIVACY.index(n.privacy) for n in self.levels if n.privacy is not None])]

    def actions(self) -> tuple[str, ...]:
        out = list(self.draft.actions)
        for n in self.levels:
            if n.actions:
                out = [a for a in out if a in n.actions]
        return tuple(out)

    def resources(self) -> tuple[Resource, ...]:
        out = list(self.draft.resources)
        for n in self.levels:
            if n.resources:
                both = {a.intersect(b) for a in out for b in n.resources}
                out = sorted(r for r in both if r is not None)
        return tuple(out)

    def limits(self) -> tuple[Limit, ...]:
        """Every limit that applies: the authority's, lowered where a block is lower,
        plus limits a block adds for a new period."""
        out = {(l.kind, l.unit, l.period): l for l in self.draft.limits}
        for n in self.levels:
            for l in n.limits:
                key = (l.kind, l.unit, l.period)
                if key in out:
                    out[key] = dataclasses.replace(out[key], amount=min(out[key].amount, l.amount))
                else:
                    out[key] = Limit(l.kind, l.unit, self.decimals(l.kind, l.unit), l.period, l.amount)
        return tuple(sorted(out.values()))

    def caps(self) -> tuple[Cap, ...]:
        out: dict[tuple[str, str], Cap] = {}
        for n in self.levels:
            for c in n.caps:
                key = (c.kind, c.unit)
                out[key] = Cap(c.kind, c.unit, min(out[key].amount, c.amount)) if key in out else c
        return tuple(sorted(out.values()))

    def decimals(self, kind: str, unit: str) -> int:
        return next((l.decimals for l in self.draft.limits if l.kind == kind and l.unit == unit), 0)

    def confirm_rules(self) -> tuple[Confirm, ...]:
        """Every confirmation rule in the chain, the authority's first."""
        return (self.draft.confirm,) + tuple(n.confirm for n in self.levels if n.confirm is not None)

    def jobs(self) -> tuple[str, ...]:
        """Ids this token is bound to. More than one distinct id means no request can match."""
        return _merge_ids(self.levels, "job")

    def tools(self) -> tuple[str, ...]:
        return _merge_ids(self.levels, "tool")

    def cards(self) -> tuple[str, ...]:
        return _merge_ids(self.levels, "card")


def _read(biscuit: Any, root: str) -> Mandate:
    try:
        authority = biscuit.block_source(0)
    except Exception as exc:
        raise PermissionError("malformed_token", str(exc)) from exc
    if not authority.startswith('schema("muse.permission", 1);\n'):
        raise PermissionError("unsupported_schema", "not a muse.permission schema 1 token")
    try:
        mandate, parsed = _parse_authority(authority)
    except _Stop:
        raise PermissionError("invalid_mandate", "the authority block is not in the schema's form") from None
    try:
        draft = propose_from_fields(parsed)
    except PermissionError as exc:
        raise PermissionError("invalid_mandate", str(exc)) from None
    if draft != parsed or mandate != mandate_id(draft) or authority_code(draft, mandate).printed() != authority:
        raise PermissionError("invalid_mandate", "the authority block is not canonical or its id does not match")
    if draft.issuer != root:
        raise PermissionError("wrong_issuer", "the token names a different root key")
    levels = []
    for i in range(1, biscuit.block_count()):
        unknown = PermissionError("unknown_block", f"block {i} is not a schema 1 narrowing")
        try:
            if biscuit.block_external_key(i) is not None:
                raise unknown
            text = biscuit.block_source(i)
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("malformed_token", str(exc)) from exc
        try:
            parsed_level = _parse_narrowing(text)
            level = canonical_narrowing(parsed_level, draft)
        except (_Stop, PermissionError):
            raise unknown from None
        if level != parsed_level or narrowing_code(level).printed() != text:
            raise unknown
        levels.append(level)
    return Mandate(biscuit=biscuit, id=mandate, draft=draft, levels=tuple(levels))


def issue(draft: Draft, root: Any) -> Mandate:
    """Sign a draft with the person's root key (a key pair from `parse_private_key`)."""
    lib = _lib()
    draft = propose_from_fields(draft)
    if draft.issuer != public_key_text(root):
        raise PermissionError("wrong_issuer", "the draft names a different root key")
    mandate = mandate_id(draft)
    code = authority_code(draft, mandate)
    try:
        builder = lib.BiscuitBuilder()
        builder.add_code(code.built(), code.params)
        biscuit = builder.build(root.private_key)
        printed = biscuit.block_source(0)
    except Exception as exc:
        raise PermissionError("library_error", str(exc)) from exc
    if printed != code.printed():
        raise PermissionError("library_error", "the library printed the authority block differently")
    return Mandate(biscuit=biscuit, id=mandate, draft=draft, levels=())


def revocation_ids(mandate: Mandate) -> list[str]:
    """One revocation identifier per block, as hex; the first is the root's."""
    return list(mandate.biscuit.revocation_ids)


def unverified_revocation_ids(token: str) -> list[str]:
    """The same, read from a token that has not been verified (so that a revoked token
    is refused before anything else is looked at)."""
    try:
        return list(_lib().UnverifiedBiscuit.from_base64(token).revocation_ids)
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("malformed_token", str(exc)) from exc


def narrow(mandate: Mandate, narrowing: Narrowing) -> Mandate:
    """Append one block. Needs no key: anyone holding the token can do this offline.
    Refuses a narrowing that is not inside what the token already allows."""
    lib = _lib()
    n = canonical_narrowing(narrowing, mandate.draft)

    def wider(what: str) -> PermissionError:
        return PermissionError("not_narrower", what)

    actions = mandate.actions()
    if not all(a in actions for a in n.actions):
        raise wider("an action the token does not allow")

    def covered(r: Resource) -> bool:
        return any(p.covers(r) for p in mandate.draft.resources) and all(
            not l.resources or any(p.covers(r) for p in l.resources) for l in mandate.levels
        )

    if not all(covered(r) for r in n.resources):
        raise wider("a resource the token does not cover")
    limits = mandate.limits()
    for l in n.limits:
        if any(p.kind == l.kind and p.unit == l.unit and p.period == l.period and l.amount > p.amount for p in limits):
            raise wider("a limit above the token's")
    caps = mandate.caps()
    for c in n.caps:
        if any(p.kind == c.kind and p.unit == c.unit and c.amount > p.amount for p in caps):
            raise wider("a cap above the token's")
    start = mandate.not_before() if n.not_before is None else n.not_before
    end = mandate.not_after() if n.not_after is None else n.not_after
    if start < mandate.not_before() or end > mandate.not_after() or start > end:
        raise wider("validity outside the token's")
    if n.privacy is not None and PRIVACY.index(n.privacy) > PRIVACY.index(mandate.privacy()):
        raise wider("a less protective privacy class")
    for new, old in ((n.job, mandate.jobs()), (n.tool, mandate.tools()), (n.card, mandate.cards())):
        if new is not None and any(o != new for o in old):
            raise wider("already bound to a different job, tool call or card")
    code = narrowing_code(n)
    try:
        block = lib.BlockBuilder()
        block.add_code(code.built(), code.params)
        biscuit = mandate.biscuit.append(block)
        printed = biscuit.block_source(biscuit.block_count() - 1)
    except Exception as exc:
        if "seal" in str(exc).lower():
            raise PermissionError("sealed_token", "a sealed token cannot be narrowed") from exc
        raise PermissionError("library_error", str(exc)) from exc
    if printed != code.printed():
        raise PermissionError("library_error", "the library printed the block differently")
    return Mandate(biscuit=biscuit, id=mandate.id, draft=mandate.draft, levels=mandate.levels + (n,))


# ---------------------------------------------------------------- the authorizer

# The fixed policy of schema 1 (interface document, section 5.2).
AUTHORIZER_POLICY = """check if schema("muse.permission", 1);
check if holder($h), req_holder($h);
check if valid_from($d), time_late($t), $t >= $d;
check if valid_until($d), time($t), $t <= $d;
check if action($a), req_action($a);
check if req_resource($k, $v), resource($k, $v) or req_resource($k, $v), resource($k, "*") or req_resource("folder", $v), resource("folder", $p), $v.starts_with($p + "/");
check if privacy_max($m), req_privacy($p), $p <= $m;
allow if mandate($id);
"""

# Added to the policy once per limit in the authority block. (`check all` cannot be used:
# in Biscuit it fails when nothing matches, and most requests carry no amount.)
AUTHORIZER_LIMIT_CHECK = "check if no_amount({kind}, {unit}) or limit({kind}, {unit}, $d, {period}, $max), amount({kind}, {unit}, $a), used({kind}, {unit}, {period}, $n), $n + $a <= $max;\n"


def _request_code(req: Request, limits: Iterable[Limit], usage: Iterable[tuple[str, str, str, int]], rules: Iterable[Confirm], now: int, skew: int) -> Code:
    c = Code()
    c.raw("time(").date(min(max(now - skew, 0), MAX_TIME)).raw(")").end()
    c.raw("time_late(").date(min(max(now + skew, 0), MAX_TIME)).raw(")").end()
    c.raw("req_holder(").string(req.holder).raw(")").end()
    c.raw("req_action(").string(req.action).raw(")").end()
    c.raw("req_resource(").string(req.resource.kind).raw(", ").string(req.resource.value).raw(")").end()
    rank = privacy_rank(req.privacy)
    c.raw("req_privacy(").int(99 if rank is None else rank).raw(")").end()
    for name, ident in (("req_job", req.job), ("req_tool", req.tool), ("req_card", req.card)):
        if ident is not None:
            c.raw(name).raw("(").string(ident).raw(")").end()
    for a in req.amounts:
        c.raw("amount(").string(a.kind).raw(", ").string(a.unit).raw(", ").int(a.value).raw(")").end()
    counters: list[tuple[str, str]] = []
    for l in limits:
        if (l.kind, l.unit) not in counters:
            counters.append((l.kind, l.unit))
    for kind, unit in counters:
        if not any(a.kind == kind and a.unit == unit for a in req.amounts):
            c.raw("no_amount(").string(kind).raw(", ").string(unit).raw(")").end()
    for kind, unit, period, used in usage:
        c.raw("used(").string(kind).raw(", ").string(unit).raw(", ").string(period).raw(", ").int(used).raw(")").end()
    for rule in rules:
        if rule.rule == "over_amount":
            c.raw("confirm_applied(").string("over_amount").raw(", ").string(rule.unit).raw(", ").int(rule.decimals).raw(", ").int(rule.amount).raw(")").end()
        else:
            c.raw("confirm_applied(").string(rule.rule).raw(")").end()
    return c


def engine_allows(mandate: Mandate, req: Request, usage: Iterable[tuple[str, str, str, int]], rules: Iterable[Confirm], now: int, skew: int) -> bool:
    """Run the token's own Datalog against the request. True only if the library allows.

    This is the second, independent gate of `check`; it is public so tests can ask the
    library alone.
    """
    lib = _lib()
    code = _request_code(req, mandate.draft.limits, usage, rules, now, skew)
    try:
        builder = lib.AuthorizerBuilder()
        builder.add_code(code.built(), code.params)
        builder.add_code(AUTHORIZER_POLICY)
        for l in mandate.draft.limits:
            builder.add_code(AUTHORIZER_LIMIT_CHECK, {"kind": l.kind, "unit": l.unit, "period": l.period})
        limits = builder.limits()
        limits.max_facts = 10_000
        limits.max_iterations = 1_000
        limits.max_time = _dt.timedelta(seconds=2)
        builder.set_limits(limits)
        builder.build(mandate.biscuit).authorize()
    except Exception:
        return False
    return True

