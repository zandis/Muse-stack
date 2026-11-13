"""The check a skill's every guarded operation goes through.

    MUSE_PERMISSIONS = off | advise | enforce        (default: advise)

    off       nothing is checked
    advise    every decision is computed, recorded and shown, and nothing is blocked
    enforce   a refusal stops the operation

A skill that does not ship with Muse is ALWAYS `enforce`, whatever the setting says.

What is checked (each against the skill's own narrowed token, with `muse.permissions.check`):

    files.read    each folder the run is given, as `folder:<its real path>`
    files.write   each file the run proposes, and again at the moment it is written
    llm.call      each model call, as `provider:router`, with the call's privacy class
    llm.paid      each model call of a run that may pay, with the most it could still cost
                  (the run's remaining money budget, in the limit's own unit) and what was used so far

Whose permission: the helper's. Its holder is `helper:<MUSE_HELPER_ID>` (default `helper:home`). Every
issued permission in force with that holder that covers the skill is a candidate; each is narrowed once
to the skill (`job: skill.<name>`, stored) and the request is checked against each. One `allow` is
enough; else one `needs_confirmation`; else the run is refused with the first reason. A run may also
be given one token to present (`permission_token`), which is then the only candidate.

Decisions:
    allow                proceed
    needs_confirmation   a card: for a file, the `save_file` card the home already makes (the write happens
                         when it is approved, under a single-use token minted from the approval); for
                         anything else the `confirm` callback makes a card and waits. With no way to ask,
                         `enforce` refuses.
    deny                 the run stops with one plain sentence, and a record is written

Before the person has set up permissions at all (no root key on the home), `advise` has nothing to
compare against: it computes nothing and records nothing. `enforce` then refuses, because nothing
was allowed. If the Biscuit library is missing, `advise` is silent and `enforce` refuses.

Not a cage: a skill running in this process could still open a file by itself. The sandbox is what
stops that (docs/sandbox.md); this is the rule the sandbox's parent applies on the skill's behalf.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import time
from typing import Any, Callable, Mapping

from muse import permissions as P
from muse.compiler.plan import covers_skill, skill_job
from muse.sandbox import policy as sandbox_policy
from muse.skills.llm import LlmError

log = logging.getLogger("muse.compiler")

ENV_MODE = "MUSE_PERMISSIONS"
ENV_HELPER = "MUSE_HELPER_ID"
MODES = ("off", "advise", "enforce")
DEFAULT_MODE = "advise"
ROUTER = P.Resource("provider", "router")  # what a model call names: the Muse router, which picks the service
PAID_UNIT = "USD"  # the router reports cost in US dollars, so a paid call needs a spend limit in USD

WORDS = {
    "no_permission": "no permission in force covers this skill (the person gives one with `muse permissions grant`)",
    "revoked": "the permission was stopped",
    "expired": "the permission has ended",
    "not_yet_valid": "the permission has not started yet",
    "wrong_holder": "the permission belongs to someone else",
    "action_not_allowed": "the permission does not include this kind of action",
    "resource_not_allowed": "the permission does not cover this folder or service",
    "privacy_not_allowed": "the permission does not allow data to be sent under this privacy class",
    "wrong_job": "the token was made for another skill",
    "already_used": "this approval was already used once",
    "amount_required": "a paid action must say how much it could cost",
    "limit_missing": "the permission sets no limit in this unit, so nothing may be spent under it",
    "usage_missing": "what was used so far could not be read",
    "over_limit": "this would go over the permission's limit",
    "over_cap": "this is more than the permission allows at one time",
    "bad_request": "the request could not be put in a form a permission can be checked against",
    "needs_confirmation": "the permission asks for your approval first, and this run has no way to ask",
    "unavailable": "permissions could not be checked on this home (the Biscuit library is not installed)",
    "bad_token": "the permission token given to this run could not be read",
}
ACTION_WORDS = {"files.read": "read a folder", "files.write": "write a file", "llm.call": "ask a model",
                "llm.paid": "use a paid model"}


def reason_words(code: str) -> str:
    return WORDS.get(code, "the permission does not allow it")


def normalise_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower() or DEFAULT_MODE
    if mode not in MODES:
        raise ValueError(f"MUSE_PERMISSIONS must be one of {', '.join(MODES)} (got {str(value)[:20]!r})")
    return mode


def mode_for(source: str, env: Mapping[str, str]) -> str:
    """The mode a skill runs under: the setting for a shipped skill, `enforce` for any other.

    (Tests that exercise the runner with throw-away home skills use the sandbox's test-only switch,
    which is read from the process environment only and never set on a real home.)"""
    setting = normalise_mode(env.get(ENV_MODE))
    if source != "shipped" and not sandbox_policy.unsafe_home_skills():
        return "enforce"
    return setting


def helper_holder(env: Mapping[str, str]) -> str:
    return f"helper:{str(env.get(ENV_HELPER, '') or '').strip() or 'home'}"


class Denied(LlmError):
    """A guarded operation was refused. `code` is the check's reason; the text is one plain sentence."""

    def __init__(self, action: str, code: str) -> None:
        self.action, self.code = action, code
        super().__init__(f"not allowed to {ACTION_WORDS.get(action, 'do this')}: {reason_words(code)}")


def folder_resource(path: str | os.PathLike[str]) -> Any:
    return P.Resource("folder", os.fspath(path))


def minor_units(usd: float, decimals: int) -> int:
    """US dollars as the limit's minor units, rounded UP (a check must never see less than could be spent)."""
    return max(0, int(math.ceil(max(usd, 0.0) * 10**decimals - 1e-9)))


@dataclasses.dataclass
class _Candidate:
    mandate: Any  # the issued permission (usage is counted against it)
    token: Any  # narrowed to this skill
    name: str  # the stored name of `token` ("" for a token the run was given)


class Guard:
    """One run's checks. Built by the runner; cheap when permissions are off or not set up."""

    def __init__(self, skill: str, source: str, env: Mapping[str, str], *, token: str | None = None,
                 clock: Callable[[], float] = time.time, confirm: Callable[..., Any] | None = None,
                 record: Callable[..., Any] | None = None, emit: Callable[..., None] | None = None) -> None:
        self.skill, self.env, self.clock = skill, env, clock
        self.mode = mode_for(source, env)
        self.confirm = confirm
        self._record = record
        self._emit = emit or (lambda kind, **facts: None)
        self.decisions: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, str]] = set()
        self.state = "off"  # off | unconfigured | unavailable | ready
        self.compiler: Any = None
        self._candidates: list[_Candidate] = []
        self._bad_token = False
        self._chosen: dict[str, _Candidate] = {}  # action -> the candidate that answered last
        self._approved: dict[str, float] = {}  # action -> the amount one answered card covered, for the rest of this run
        self.holder = helper_holder(env)
        self.job = skill_job(skill)
        if self.mode == "off":
            return
        from muse.compiler.core import Compiler

        self.compiler = Compiler(env, clock=clock, record=record)
        if not self.compiler.keys.exists() and token is None:
            self.state = "unconfigured"
            return
        if not P.available():
            self.state = "unavailable"
            return
        self.state = "ready"
        try:
            self._load(token)
        except P.PermissionError as exc:
            log.warning("guard: the permissions could not be read (%s)", exc.code)
            self._bad_token = True

    # ---------------------------------------------------------------- candidates

    def _load(self, token: str | None) -> None:
        c = self.compiler
        if token is not None:
            root = c.root_public()
            if root is None:
                self._bad_token = True
                return
            try:
                given = P.Mandate.parse(token, root)
            except P.PermissionError:
                self._bad_token = True
                return
            parent = c.find(given.id) or given
            self.holder = given.draft.holder
            if not given.jobs():
                given = P.narrow(given, P.Narrowing(job=self.job))
            self._candidates = [_Candidate(parent, given, "")]
            return
        revoked, now = c.revocations(), c.now()
        for mandate in c.mandates():
            if mandate.draft.holder != self.holder or c.state_of(mandate, revoked, now) != "active":
                continue
            if not covers_skill(mandate, self.skill):
                continue
            name, narrowed = c.skill_token(mandate, self.skill)
            self._candidates.append(_Candidate(mandate, narrowed, name))

    @property
    def active(self) -> bool:
        """Does this guard compute decisions at all?"""
        return self.mode != "off" and (self.state == "ready" or self.mode == "enforce")

    # ---------------------------------------------------------------- one decision

    def decide(self, action: str, resource: Any, privacy: str, *, paid_usd: float | None = None) -> tuple[Any, _Candidate | None]:
        """The best decision over the candidates, and the candidate that gave it. Computes only; blocks nothing."""
        if self.state != "ready":
            return P.deny("unavailable" if self.state == "unavailable" else "no_permission"), None
        if self._bad_token:
            return P.deny("bad_token"), None
        c = self.compiler
        revoked, now = c.revocations(), c.now()
        with c.spent_lock():
            spent = c.store.spent()
        best: tuple[Any, _Candidate | None] = (P.deny("no_permission"), None)
        for cand in self._candidates:
            amounts = []
            if action == "llm.call" and any(l.kind == "requests" for l in cand.mandate.draft.limits):
                amounts.append(P.Amount("requests", "", 1))
            if paid_usd is not None:
                amounts.append(P.Amount("spend", PAID_UNIT, minor_units(paid_usd, cand.mandate.decimals("spend", PAID_UNIT))))
            request = P.Request(holder=self.holder, action=action, resource=resource, privacy=privacy,
                                amounts=tuple(amounts), job=self.job)
            decision = P.check(cand.token, request, c.usage(cand.mandate), revoked, spent, now)
            if decision.allowed:
                return decision, cand
            if decision.kind == "needs_confirmation" and best[0].kind != "needs_confirmation":
                best = (decision, cand)
            elif best[1] is None:
                best = (decision, cand)
        return best

    def _note(self, action: str, decision: Any, cand: _Candidate | None, blocked: bool) -> None:
        key = (action, decision.kind, decision.detail)
        if key in self._seen:
            return
        self._seen.add(key)
        mandate = cand.mandate.id if cand is not None else None
        self.decisions.append({"action": action, "decision": decision.kind, "reason": decision.detail, "blocked": blocked,
                               "mandate": mandate})
        self._emit("permission", action=action, decision=decision.kind, reason=decision.detail, blocked=blocked, mode=self.mode)
        if decision.kind == "allow" or self.compiler is None:
            return
        what = ACTION_WORDS.get(action, "do something")
        if decision.kind == "needs_confirmation" and not blocked:
            return  # asked through a card, which has its own record
        why = reason_words(decision.detail if decision.kind == "deny" else "needs_confirmation")
        head = f"Refused the skill {self.skill}" if blocked else f"Advice only, not blocked: the skill {self.skill} would be refused"
        self.compiler.record("mandate.check", f"{head} when it went to {what}: {why}"[:200], actor="helper", mandate=mandate,
                             outcome="declined" if blocked else "ok", egress="none")

    def require(self, action: str, resource: Any, privacy: str, *, paid_usd: float | None = None, ask: bool = True,
                facts: Mapping[str, Any] | None = None) -> bool:
        """Check one operation. True: go ahead. Raises Denied when it is refused under `enforce`.

        `ask=False`: a `needs_confirmation` is not a refusal here, because the caller asks in its own way
        (a proposed file becomes a card later)."""
        if not self.active:
            return True
        decision, cand = self.decide(action, resource, privacy, paid_usd=paid_usd)
        if cand is not None:
            self._chosen[action] = cand
        if decision.allowed:
            self._note(action, decision, cand, False)
            return True
        enforce = self.mode == "enforce"
        if decision.kind == "needs_confirmation":
            if not ask:
                self._note(action, decision, cand, False)
                return True
            if action in self._approved and (paid_usd or 0.0) <= self._approved[action] + 1e-12:
                return True  # the person already approved this much for this run; the money only goes down from there
            if self.confirm is not None and cand is not None:
                approved = self._ask(action, resource, privacy, paid_usd, cand, facts or {})
                if approved:
                    self._approved[action] = paid_usd or 0.0
                    self._note(action, decision, cand, False)
                    return True
                self._note(action, P.deny("needs_confirmation"), cand, enforce)
                if enforce:
                    raise Denied(action, "needs_confirmation")
                return True
            self._note(action, P.deny("needs_confirmation"), cand, enforce)
            if enforce:
                raise Denied(action, "needs_confirmation")
            return True
        self._note(action, decision, cand, enforce)
        if enforce:
            raise Denied(action, decision.detail)
        return True

    def _ask(self, action: str, resource: Any, privacy: str, paid_usd: float | None, cand: _Candidate,
             facts: Mapping[str, Any]) -> bool:
        """Ask through the caller's card, then spend the approval as a single-use token."""
        try:
            card_id = self.confirm(action, {"skill": self.skill, "paid_usd": paid_usd, "mandate": cand.mandate.id, **facts})
        except Exception as exc:
            log.warning("guard: the person could not be asked (%s)", type(exc).__name__)
            return False
        if not card_id:
            return False
        amounts = ()
        if paid_usd is not None:
            amounts = (P.Amount("spend", PAID_UNIT, minor_units(paid_usd, cand.mandate.decimals("spend", PAID_UNIT))),)
        request = P.Request(holder=self.holder, action=action, resource=resource, privacy=privacy, amounts=amounts, job=self.job)
        return bool(self.compiler.use_approval(cand.token, request, str(card_id)).allowed)

    # ---------------------------------------------------------------- the runner's hooks

    def read_root(self, path: str | os.PathLike[str], privacy: str) -> None:
        self.require("files.read", folder_resource(path), privacy)

    def propose(self, path: str, privacy: str) -> None:
        """A file the run would like written. Asking comes later (a card), so only a plain refusal stops here."""
        self.require("files.write", folder_resource(path), privacy, ask=False)

    def write_now(self, path: str, privacy: str) -> bool:
        """May a proposed file be written right now, with nobody asked through a card? (`muse skills run --apply`)"""
        try:
            return self.require("files.write", folder_resource(path), privacy)
        except Denied:
            return False

    def pending_write(self, card_id: str, path: str, privacy: str) -> None:
        """Remember which token asked for the file behind a `save_file` card, so approving the card can mint
        the single-use token for exactly that write (muse/home/skills.py)."""
        cand = self._chosen.get("files.write")
        if not self.active or cand is None or not cand.name:
            return
        self.compiler.table.add_approval(card_id, mandate_id=cand.mandate.id, token_name=cand.name, action="files.write",
                                         resource=("folder", path), privacy=privacy, job=self.job, mode=self.mode)

    def llm(self, privacy: str, *, paid_left: float | None) -> None:
        """Before a model call is sent. `paid_left` is the run's remaining money, or None for a run that may not pay."""
        self.require("llm.call", ROUTER, privacy)
        if paid_left is not None:
            self.require("llm.paid", ROUTER, privacy, paid_usd=paid_left, facts={"amount_usd": paid_left})

    def settle_llm(self, cost_usd: float | None, paid: bool) -> None:
        """After a model call answered: count it against the permission that allowed it."""
        if not self.active or self.state != "ready":
            return
        cand = self._chosen.get("llm.call")
        if cand is not None and any(l.kind == "requests" for l in cand.mandate.draft.limits):
            self.compiler.add_usage(cand.mandate, "requests", "", 1)
        payer = self._chosen.get("llm.paid") if paid else None
        if payer is not None and cost_usd:
            self.compiler.add_usage(payer.mandate, "spend", PAID_UNIT, minor_units(cost_usd, payer.mandate.decimals("spend", PAID_UNIT)))

    def wrap_post(self, post: Callable[..., Any], paid_left: Callable[[], float | None]) -> Callable[..., Any]:
        """The router transport with the check in front of it: a refused call is never sent."""
        if not self.active:
            return post

        def guarded(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Any:
            paid = str(headers.get("x-muse-allow-paid", "")).lower() == "true"
            self.llm(str(headers.get("x-muse-privacy", "") or "standard"), paid_left=paid_left() if paid else None)
            reply = post(url, headers, body, timeout)
            if getattr(reply, "status", 0) == 200:
                try:
                    cost = float(str(reply.headers.get("x-muse-cost-usd", "") or 0))
                except (TypeError, ValueError):
                    cost = 0.0
                self.settle_llm(cost if math.isfinite(cost) else 0.0, paid)
            return reply

        return guarded

    def sandbox_authorize(self, default_privacy: str) -> Callable[[str, Mapping[str, Any], str | None], None]:
        """`muse.sandbox.Policy.authorize`: the parent's check before it does something a sandboxed skill asked for."""

        def authorize(action: str, facts: Mapping[str, Any], token: str | None) -> None:
            try:
                if action in ("llm.ask", "llm.embed"):
                    privacy = facts.get("privacy") if isinstance(facts.get("privacy"), str) else None
                    try:
                        from muse import policy

                        cls = policy.stricter(default_privacy, policy.normalise_class(privacy)) if privacy else default_privacy
                    except Exception:
                        cls = default_privacy
                    self.require("llm.call", ROUTER, cls)
                elif action == "files.propose" and isinstance(facts.get("path"), str):
                    self.propose(facts["path"], default_privacy)
            except Denied as exc:
                raise PermissionError(str(exc)) from None

        return authorize

    def summary(self) -> dict[str, Any] | None:
        """For the run's result: the mode and what was decided. None when there is nothing to say."""
        if not self.active or (self.mode != "enforce" and not self.decisions):
            return None
        return {"mode": self.mode, "decisions": [dict(d) for d in self.decisions],
                "blocked": any(d["blocked"] for d in self.decisions)}


# ---------------------------------------------------------------- an approved card becomes one write


def settle_write(env: Mapping[str, str] | None, card_id: str, *, clock: Callable[[], float] = time.time,
                 record: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Called when a `save_file` card was approved, just before the file is written.

    Mints the single-use token for that one write from the token that asked (narrowed to `files.write`, this
    card, five minutes), checks the write under it and marks the card as used. Returns
    {"known": bool, "allowed": bool, "reason": code, "mandate": id | None, "mode": str}. `known` is False when
    no permission was involved in the proposal (nothing to mint from): the caller then does what it always did."""
    from muse.compiler.core import Compiler

    compiler = Compiler(env, clock=clock, record=record)
    row = compiler.table.approval(card_id)
    if row is None:
        return {"known": False, "allowed": True, "reason": "", "mandate": None, "mode": ""}
    out = {"known": True, "allowed": False, "reason": "", "mandate": row["mandate_id"], "mode": row["mode"]}
    if row["state"] != "pending":
        out["reason"] = "revoked" if row["state"] == "revoked" else "already_used"
        return out
    try:
        parent = compiler.store.load(row["token_name"], compiler.keys.public())
        request = P.Request(holder=parent.draft.holder, action=row["action"],
                            resource=P.Resource(row["resource_kind"], row["resource_value"]), privacy=row["privacy"], job=row["job"])
        decision = compiler.use_approval(parent, request, card_id)
    except P.PermissionError as exc:
        decision = P.deny("unavailable" if exc.code == "library_missing" else exc.code)
    except OSError:
        decision = P.deny("revoked")  # the narrowed token is gone: its permission was stopped and cleaned up
    out["allowed"], out["reason"] = bool(decision.allowed), decision.detail
    compiler.table.settle_approval(card_id, "used" if decision.allowed else "refused")
    return out
