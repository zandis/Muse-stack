"""The permission compiler: make what enforces a permission, say how each part stands, and take it away.

    Compiler(environ).issue(draft)        sign, store, plan, apply           -> (mandate, Applied)
             .compile(mandate)            plan + apply for a stored one      -> Applied
             .status(mandate_id)          per-target state                   -> {target: {state, reason}}
             .revoke(mandate_id)          revocation first, then every artifact; idempotent
             .sweep()                     remove what a stopped, ended or missing permission left behind;
                                          make again what a live one is missing. Safe every minute.

States (architecture section 2.8): `active`, `pending` (with a reason), `failed` (with a reason),
`not_built`, `revoked`.

The invariant: no artifact without a permission, and every artifact names its permission's id.
  * an app entry carries `mandate` (muse/apps.py); a folder on the skill-folders list carries `mandate`
    (muse/home/folders.py); a narrowed token holds the id in its signed first block; a consent record
    has it in its `mandate` member;
  * a row is written before its artifact and removed after it, so a crash leaves work for the sweep,
    never an artifact nobody knows about. The sweep also looks for apps and folders that name a
    permission which is no longer in force, with or without a row.

Order of a stop: the signed revocation list is written FIRST (from that moment every check refuses the
permission and everything narrowed from it), then the router's copy of it (muse/compiler/gate.py), then
each artifact is removed. A failure on the way is kept on the artifact's row (the error's TYPE only) and
the sweep tries again.

Needs `biscuit-python` (as muse/permissions does) for everything that reads a token. The router's
check (gate.py) does not.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from muse import permissions as P
from muse.compiler import gate
from muse.compiler.plan import ArtifactSpec, plan, skill_job
from muse.compiler.table import Table
from muse.permissions.store import FileKeySource, PermissionStore

log = logging.getLogger("muse.compiler")

ADOPTED = "app_adopted"  # the kind of a router row whose app existed before the permission (a host set up earlier)
APPROVAL_TTL_S = 300  # how long a single-use approval token lasts (architecture 3.6: five minutes)

R_FOLDER = "This folder could not be put on the list: it is not on this computer, or it is one Muse does not give to skills."
R_FOLDER_NAME = "The name this permission's folder needs on the list is already used by something else."
R_APP_NAME = "An app with the name this permission needs already exists and belongs to something else."
R_APP_MISSING = ("The app entry for this permission is no longer there. Its key cannot be shown again: stop this permission "
                 "and give it again to get a new one.")
R_APP = "The app entry could not be written. Run `muse doctor` on the home."
R_RECORD = "The consent record could not be written (records need the `cryptography` package, or the records file could not be used)."
R_TOKEN = "The narrowed token for a skill could not be made or stored."
R_REMOVE = "This could not be removed yet. The sweep will try again."
R_NOT_APPLIED = "Not made yet. The sweep will make it."
FAILED = {"folder": R_FOLDER, "app": R_APP, ADOPTED: R_APP, "consent_record": R_RECORD, "skill_token": R_TOKEN}


class CompileError(Exception):
    """A refusal with a code and one fixed plain sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


@dataclass
class Applied:
    mandate_id: str
    targets: dict[str, dict[str, str]]
    app_name: str | None = None
    app_token: str | None = field(default=None, repr=False)  # a NEW app key: given to the caller once, stored nowhere


def period_keys(zone: str, now: float) -> dict[str, str]:
    """`{"day": "day:2026-11-13", "month": "month:2026-11"}` in the permission's own time zone (UTC if unknown)."""
    from datetime import datetime, timezone

    tz: Any = timezone.utc
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(zone)
    except Exception:  # an unknown zone name, or no zone data on this system
        pass
    local = datetime.fromtimestamp(now, tz=tz)
    return {"day": local.strftime("day:%Y-%m-%d"), "month": local.strftime("month:%Y-%m")}


class Compiler:
    def __init__(self, environ: Mapping[str, str] | None = None, *, clock: Callable[[], float] = time.time,
                 record: Callable[..., Any] | None = None) -> None:
        self.environ = environ
        self.clock = clock
        self.store = PermissionStore(environ=environ)
        self.keys = FileKeySource(environ=environ)
        self.table = Table(environ, clock)
        self._record = record
        self.fault: Callable[[str], None] | None = None  # tests: called at named steps; raising simulates a crash there
        self._new_token: tuple[str, str] | None = None

    # ---------------------------------------------------------------- the stored permissions

    def now(self) -> int:
        return int(self.clock())

    def _step(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)

    def root_public(self) -> str | None:
        return self.keys.public() if self.keys.exists() else None

    def ensure_root(self) -> str:
        """The root public key, making the key on first use (MUSE_HOME/root.key, 0600)."""
        if not self.keys.exists():
            try:
                return self.keys.create()
            except P.PermissionError as exc:
                if exc.code != "key_exists":  # two callers at once: the other one made it
                    raise
        return self.keys.public()

    def revocations(self) -> Any:
        root = self.root_public()
        return self.store.revocations(root) if root else P.RevocationList()

    def tokens(self) -> dict[str, Any]:
        """Every stored token that verifies under the root key, by stored name."""
        root = self.root_public()
        out: dict[str, Any] = {}
        if root is None:
            return out
        for name in self.store.names():
            try:
                out[name] = self.store.load(name, root)
            except P.PermissionError as exc:
                if exc.code == "library_missing":
                    raise
                log.warning("compiler: a stored token could not be read (%s)", exc.code)
            except OSError as exc:
                log.warning("compiler: a stored token could not be read (%s)", type(exc).__name__)
        return out

    def mandates(self) -> list[Any]:
        """The issued permissions (not their narrowed copies), oldest first."""
        seen: dict[str, Any] = {}
        for mandate in self.tokens().values():
            if not mandate.levels:
                seen.setdefault(mandate.id, mandate)
        return sorted(seen.values(), key=lambda m: (m.draft.issued_at, m.id))

    def find(self, ident: str) -> Any | None:
        """A permission by its id, or by the start of its id with or without `m1_` (None when none or several match)."""
        if not isinstance(ident, str) or not ident:
            return None
        found = [m for m in self.mandates() if m.id == ident or m.id.startswith(ident) or m.id[3:].startswith(ident)]
        return found[0] if len(found) == 1 else None

    def state_of(self, mandate: Any, revoked: Any = None, now: int | None = None) -> str:
        """`active`, `stopped`, `expired` or `not_yet_valid`."""
        revoked = self.revocations() if revoked is None else revoked
        now = self.now() if now is None else now
        if any(revoked.contains(i) for i in P.revocation_ids(mandate)):
            return "stopped"
        if now > mandate.not_after():
            return "expired"
        return "not_yet_valid" if now < mandate.not_before() else "active"

    # ---------------------------------------------------------------- records

    def record(self, action: str, summary: str, **fields: Any) -> Any | None:
        """Append a record; None when it could not be written. Never raises."""
        try:
            if self._record is not None:
                return self._record(action, summary, **fields)
            from muse.records.home import HomeRecords

            return HomeRecords(self.environ, clock=self.clock).write(action, summary, **fields)
        except Exception as exc:  # ImportError included: records are optional
            log.warning("compiler: no record was written for %s (%s)", action, type(exc).__name__)
            return None

    # ---------------------------------------------------------------- issue, compile, apply

    def issue(self, draft: Any, *, adopt_app: str | None = None, actor: str = "person") -> tuple[Any, Applied]:
        """Sign a draft with the root key, store the token, and make everything that enforces it."""
        mandate = P.issue(draft, self.keys.load())
        self.store.save(mandate)
        return mandate, self.compile(mandate, adopt_app=adopt_app, actor=actor)

    def compile(self, mandate: Any, *, adopt_app: str | None = None, mint: bool = True, actor: str = "person") -> Applied:
        """Plan and apply. `mint=False` (the sweep) never makes a new app key, since nobody would see it."""
        if mandate.levels:
            raise CompileError("invalid_request", "Only an issued permission is compiled, not a narrowed copy of one.")
        if adopt_app is not None and self.table.get(mandate.id, "router", "app") is None:
            self.table.put(mandate.id, "router", "app", ADOPTED, state="pending", artifact_id=adopt_app, reason=R_NOT_APPLIED)
        return self.apply(mandate, plan(mandate), mint=mint, actor=actor)

    def apply(self, mandate: Any, specs: list[ArtifactSpec], *, mint: bool = True, actor: str = "person") -> Applied:
        self._new_token = None
        for spec in specs:
            try:
                self._apply_one(mandate, spec, mint, actor)
            except P.PermissionError as exc:
                if exc.code == "library_missing":
                    raise
                self._failed(spec, exc)
            except Exception as exc:
                self._failed(spec, exc)
        name, token = self._new_token or (None, None)
        self._new_token = None
        row = self.table.get(mandate.id, "router", "app")
        return Applied(mandate.id, self.status(mandate.id), app_name=name or (row["artifact_id"] if row else None) or None, app_token=token)

    def _failed(self, spec: ArtifactSpec, exc: BaseException) -> None:
        log.warning("compiler: %s/%s could not be made (%s)", spec.target, spec.kind, type(exc).__name__)
        row = self.table.get(spec.mandate_id, spec.target, spec.key)
        kind = row["kind"] if row else spec.kind
        self.table.put(spec.mandate_id, spec.target, spec.key, kind, state="failed", reason=FAILED.get(kind, R_NOT_APPLIED),
                       error=type(exc).__name__)

    def _apply_one(self, mandate: Any, spec: ArtifactSpec, mint: bool, actor: str) -> None:
        mid, put = mandate.id, self.table.put
        if spec.status == "not_built":
            put(mid, spec.target, spec.key, spec.kind, state="not_built", reason=spec.reason)
        elif spec.status == "pending":
            put(mid, spec.target, spec.key, spec.kind, state="pending", reason=spec.reason)
        elif spec.kind == "skills_on_demand":
            put(mid, spec.target, spec.key, spec.kind, state="active", artifact_id="on-demand")
        elif spec.kind == "skill_token":
            self.skill_token(mandate, spec.params["skill"])
        elif spec.kind == "folder":
            self._apply_folder(mandate, spec)
        elif spec.kind == "app":
            self._apply_app(mandate, spec, mint)
        elif spec.kind == "consent_record":
            self._apply_consent(mandate, spec, actor)

    def _apply_folder(self, mandate: Any, spec: ArtifactSpec) -> None:
        from muse.home import folders

        mid, name, path = mandate.id, spec.params["name"], spec.params["path"]
        entry = folders.load(self.environ).get(name)
        if entry is not None and entry.get("mandate") == mid:
            self.table.put(mid, spec.target, spec.key, spec.kind, state="active", artifact_id=name)
            return
        if entry is not None:
            self.table.put(mid, spec.target, spec.key, spec.kind, state="failed", artifact_id="", reason=R_FOLDER_NAME)
            return
        self.table.put(mid, spec.target, spec.key, spec.kind, state="pending", artifact_id=name, reason=R_NOT_APPLIED)
        self._step("folder_row_written")
        try:
            folders.add(name, path, output=bool(spec.params["output"]), environ=self.environ, now=self.clock(), mandate=mid)
        except folders.FolderError:
            # Not there (yet), or a folder Muse never gives to skills. `pending`: the sweep tries again.
            self.table.put(mid, spec.target, spec.key, spec.kind, state="pending", artifact_id="", reason=R_FOLDER)
            return
        self.table.put(mid, spec.target, spec.key, spec.kind, state="active", artifact_id=name)

    def _apply_app(self, mandate: Any, spec: ArtifactSpec, mint: bool) -> None:
        from muse import apps

        mid, p = mandate.id, spec.params
        row = self.table.get(mid, spec.target, spec.key)
        adopted = row is not None and row["kind"] == ADOPTED
        kind = ADOPTED if adopted else spec.kind
        name = (row["artifact_id"] if row and row["artifact_id"] else "") or p["name"]
        existing = apps.load_apps(self.environ).get(name)
        if existing is not None and existing.mandate == mid:
            self.table.put(mid, spec.target, spec.key, kind, state="active", artifact_id=name, reason=spec.reason)
            return
        if existing is not None and existing.mandate is None and adopted:
            apps.bind_mandate(name, mid, p["valid_from"], p["valid_until"], self.environ, daily_requests=p["daily_requests"],
                              allow_paid=p["allow_paid"], default_privacy=p["default_privacy"])
            self.table.put(mid, spec.target, spec.key, kind, state="active", artifact_id=name, reason=spec.reason)
            return
        if existing is not None:
            self.table.put(mid, spec.target, spec.key, kind, state="failed", artifact_id="", reason=R_APP_NAME)
            return
        if not mint or adopted:
            self.table.put(mid, spec.target, spec.key, kind, state="failed", artifact_id=name, reason=R_APP_MISSING)
            return
        self.table.put(mid, spec.target, spec.key, kind, state="pending", artifact_id=name, reason=R_NOT_APPLIED)
        self._step("app_row_written")
        _, token = apps.add_app(name, p["priority"], daily_requests=p["daily_requests"], daily_tokens=p["daily_tokens"],
                                allow_paid=p["allow_paid"], default_privacy=p["default_privacy"], note="made from a permission",
                                environ=self.environ, now=self.clock(), mandate=mid, valid_from=p["valid_from"],
                                valid_until=p["valid_until"])
        self._new_token = (name, token)
        self.table.put(mid, spec.target, spec.key, kind, state="active", artifact_id=name, reason=spec.reason)

    def _apply_consent(self, mandate: Any, spec: ArtifactSpec, actor: str) -> None:
        mid = mandate.id
        row = self.table.get(mid, spec.target, spec.key)
        if row is not None and row["state"] == "active" and row["artifact_id"]:
            return
        self.table.put(mid, spec.target, spec.key, spec.kind, state="pending", reason=R_NOT_APPLIED)
        d = mandate.draft
        count = len(d.actions)
        summary = (f"Gave a permission to {_holder_word(d.holder)}: {count} action{'s' if count != 1 else ''}, "
                   f"until {P.format_time(mandate.not_after())[:10]}")
        rec = self.record("mandate.issue", summary, actor=actor, mandate=mid, egress="none")
        if rec is None:
            self.table.put(mid, spec.target, spec.key, spec.kind, state="failed", reason=R_RECORD)
            return
        from muse.records import format as fmt

        self.table.put(mid, spec.target, spec.key, spec.kind, state="active", artifact_id=fmt.record_hash(rec))

    # ---------------------------------------------------------------- narrowed tokens for skills

    def skill_token(self, mandate: Any, skill: str) -> tuple[str, Any]:
        """The stored token narrowed to one skill (made on first use): (its stored name, the token)."""
        mid, key = mandate.id, f"skill:{skill}"
        row = self.table.get(mid, "skills", key)
        root = self.keys.public()
        if row is not None and row["artifact_id"] and row["state"] == "active":
            try:
                return row["artifact_id"], self.store.load(row["artifact_id"], root)
            except (OSError, P.PermissionError) as exc:
                if isinstance(exc, P.PermissionError) and exc.code == "library_missing":
                    raise
        child = P.narrow(mandate, P.Narrowing(job=skill_job(skill)))
        self.table.put(mid, "skills", key, "skill_token", state="pending", artifact_id="", reason=R_NOT_APPLIED)
        name = self.store.save(child)
        self.table.put(mid, "skills", key, "skill_token", state="active", artifact_id=name)
        return name, child

    # ---------------------------------------------------------------- usage ("used so far this period")

    def usage(self, mandate: Any, now: float | None = None) -> dict[tuple[str, str, str], int]:
        keys = period_keys(mandate.draft.period_zone, self.clock() if now is None else now)
        return {(l.kind, l.unit, l.period): self.table.used(mandate.id, l.kind, l.unit, keys[l.period]) for l in mandate.limits()}

    def add_usage(self, mandate: Any, kind: str, unit: str, amount: int, now: float | None = None) -> None:
        keys = period_keys(mandate.draft.period_zone, self.clock() if now is None else now)
        self.table.add_usage(mandate.id, kind, unit, [keys["day"], keys["month"]], amount)

    # ---------------------------------------------------------------- single-use approvals

    @contextmanager
    def spent_lock(self) -> Iterator[None]:
        """One reader-writer of the spent set at a time, across threads and processes."""
        self.store._ensure()  # noqa: SLF001 - the directory, 0700
        fd = os.open(str(self.store.dir / ".spent.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows
                pass
            yield
        finally:
            os.close(fd)

    def use_approval(self, parent: Any, request: Any, card_id: str, *, ttl: int = APPROVAL_TTL_S) -> Any:
        """An approved card becomes one action: narrow `parent` to this action and this card for a few minutes,
        check the request under that token, and remember the card as used. A second use of the same card is
        refused (`already_used`), also by another process. Returns the Decision."""
        now = self.now()
        # The approval covers exactly what the card showed: this action, this resource, at most this amount, briefly.
        caps = tuple(sorted({P.Cap(a.kind, a.unit, a.value) for a in request.amounts}))
        resources = (request.resource,) if request.resource.value != "*" else ()
        try:
            child = P.narrow(parent, P.Narrowing(actions=(request.action,), resources=resources, caps=caps, card=card_id,
                                                 not_after=min(now + ttl, parent.not_after())))
        except P.PermissionError as exc:
            if exc.code == "library_missing":
                raise
            return P.deny("expired" if now > parent.not_after() else exc.code)
        request = dataclasses.replace(request, card=card_id)
        with self.spent_lock():
            spent = self.store.spent()
            decision = P.check_and_record(child, request, self.usage(parent), self.revocations(), spent, now)
            if decision.allowed:
                self.store.save_spent(spent)
        return decision

    # ---------------------------------------------------------------- status

    def status(self, mandate_id: str) -> dict[str, dict[str, str]]:
        """Per target: {"state": active | pending | failed | not_built | revoked, "reason": str, "note"?: str}."""
        rows = self.table.rows(mandate_id)
        mandate = self.find(mandate_id) if P.available() else None
        planned = [s.target for s in plan(mandate)] if mandate is not None else []
        live = mandate is not None and self.state_of(mandate) in ("active", "not_yet_valid")
        out: dict[str, dict[str, str]] = {}
        for target in dict.fromkeys(planned + [r["target"] for r in rows]):
            mine = [r for r in rows if r["target"] == target]
            if not live:
                left = [r for r in mine if r["state"] != "not_built"]
                if not left:
                    out[target] = {"state": "revoked", "reason": ""}
                else:
                    out[target] = {"state": "failed" if any(r["error"] for r in left) else "pending", "reason": R_REMOVE}
                continue
            if not mine:
                out[target] = {"state": "pending", "reason": R_NOT_APPLIED}
                continue
            for state in ("failed", "pending"):
                bad = [r for r in mine if r["state"] == state]
                if bad:
                    out[target] = {"state": state, "reason": bad[0]["reason"] or R_NOT_APPLIED}
                    break
            else:
                if all(r["state"] == "not_built" for r in mine):
                    out[target] = {"state": "not_built", "reason": mine[0]["reason"]}
                else:
                    out[target] = {"state": "active", "reason": ""}
                    notes = [r["reason"] for r in mine if r["state"] == "active" and r["reason"]]
                    if notes:
                        out[target]["note"] = notes[0]
        return out

    # ---------------------------------------------------------------- revoke

    def revoke(self, mandate_id: str, *, actor: str = "person") -> dict[str, dict[str, str]]:
        """Stop a permission everywhere. Idempotent; returns the per-target state afterwards."""
        mandate = self.find(mandate_id)
        if mandate is None:
            raise CompileError("not_found", "There is no such permission.")
        revoked = self.revocations()
        root_id = P.revocation_ids(mandate)[0]
        if not revoked.contains(root_id):
            revoked.add(root_id, self.now(), self.keys.load())
            self.store.save_revocations(revoked)
        self._step("revocation_recorded")
        gate.mark_stopped([mandate.id], self.environ, self.clock())
        self._step("router_told")
        self._remove_artifacts(mandate.id, "stopped", actor)
        return self.status(mandate.id)

    def _remove_artifacts(self, mandate_id: str, why: str, actor: str) -> int:
        """Remove everything made for a permission. Returns how many rows could not be removed."""
        failed = 0
        for row in self.table.rows(mandate_id):
            try:
                self._remove_one(row, why, actor)
                self.table.delete(mandate_id, row["target"], row["key"])
            except Exception as exc:
                failed += 1
                log.warning("compiler: %s/%s could not be removed (%s)", row["target"], row["kind"], type(exc).__name__)
                self.table.put(mandate_id, row["target"], row["key"], row["kind"], state="failed", reason=R_REMOVE,
                               error=type(exc).__name__)
        try:
            self._remove_family(mandate_id)
            self.table.drop_approvals(mandate_id)
        except Exception as exc:
            failed += 1
            log.warning("compiler: narrowed tokens could not be removed (%s)", type(exc).__name__)
        return failed

    def _remove_family(self, mandate_id: str) -> None:
        """Delete every stored narrowed copy of a permission (the permission's own token stays, as its history)."""
        root = self.root_public()
        prefix = mandate_id[3:15] + "-"
        for name in self.store.names():
            if not name.startswith(prefix):
                continue
            try:
                narrowed = bool(self.store.load(name, root or "").levels)
            except (OSError, P.PermissionError) as exc:
                if isinstance(exc, P.PermissionError) and exc.code == "library_missing":
                    raise
                narrowed = True  # unreadable, and named as a copy in this family
            if narrowed:
                self.store.remove(name)

    def _remove_one(self, row: Mapping[str, Any], why: str, actor: str) -> None:
        from muse import apps
        from muse.home import folders

        mid, kind, ident = row["mandate_id"], row["kind"], row["artifact_id"]
        self._step(f"remove_{kind}")
        if kind == "skill_token" and ident:
            self.store.remove(ident)
        elif kind == "folder" and ident:
            folders.remove(ident, self.environ, only_mandate=mid)
        elif kind in ("app", ADOPTED) and ident:
            apps.remove_app(ident, self.environ, only_mandate=mid)
        elif kind == "consent_record":
            words = {"stopped": "Stopped a permission", "expired": "A permission ended: its time ran out",
                     "missing": "Removed what was left of a permission whose token is gone"}[why]
            if self.record("mandate.revoke", words, actor=actor, mandate=mid, egress="none") is None:
                raise RuntimeError("no record")

    # ---------------------------------------------------------------- sweep

    def sweep(self, now: float | None = None) -> dict[str, Any]:
        """Bring the artifacts back in line with the permissions. Never raises for one bad artifact.

        Returns {"removed": mandate ids cleaned up, "applied": ids made whole again, "failed": rows still wrong,
        "skipped": why nothing was done (when the library is missing)}."""
        from muse import apps
        from muse.home import folders

        if not P.available():
            return {"removed": [], "applied": [], "failed": 0, "skipped": "biscuit-python is not installed"}
        when = int(self.clock() if now is None else now)
        revoked = self.revocations()
        live: dict[str, Any] = {}
        dead: dict[str, str] = {}
        for mandate in self.mandates():
            state = self.state_of(mandate, revoked, when)
            if state in ("active", "not_yet_valid"):
                live[mandate.id] = mandate
            else:
                dead[mandate.id] = state
        stopped = [mid for mid, state in dead.items() if state == "stopped"]
        if stopped or gate.stopped_ids(self.environ) is None:
            gate.mark_stopped(stopped, self.environ, when)
        removed, failed = [], 0
        for mid in sorted(self.table.mandate_ids() - set(live)):
            failed += self._remove_artifacts(mid, dead.get(mid, "missing"), "system")
            removed.append(mid)
        # Artifacts that name a permission no longer in force, row or no row (a crash between making one and noting it).
        try:
            for app in apps.load_apps(self.environ).values():
                if app.mandate and app.mandate not in live and apps.remove_app(app.name, self.environ, only_mandate=app.mandate):
                    removed.append(app.mandate)
            for entry in folders.load(self.environ).values():
                tag = entry.get("mandate")
                if tag and tag not in live and folders.remove(entry["name"], self.environ, only_mandate=tag):
                    removed.append(tag)
        except Exception as exc:
            failed += 1
            log.warning("compiler: the sweep could not look at apps or folders (%s)", type(exc).__name__)
        for mid in dead:
            try:
                self._remove_family(mid)
            except Exception as exc:
                failed += 1
                log.warning("compiler: narrowed tokens could not be removed (%s)", type(exc).__name__)
        applied = []
        for mid, mandate in live.items():
            before = {(r["target"], r["key"]): (r["state"], r["artifact_id"]) for r in self.table.rows(mid)}
            self.compile(mandate, mint=False, actor="system")
            after = self.table.rows(mid)
            if {(r["target"], r["key"]): (r["state"], r["artifact_id"]) for r in after} != before:
                applied.append(mid)
            failed += sum(1 for r in after if r["state"] == "failed")
        try:
            with self.spent_lock():
                spent = self.store.spent()
                size = len(spent)
                spent.purge(when)
                if len(spent) != size:
                    self.store.save_spent(spent)
        except Exception as exc:
            log.warning("compiler: the spent set could not be tidied (%s)", type(exc).__name__)
        return {"removed": sorted(set(removed)), "applied": applied, "failed": failed, "skipped": ""}

    # ---------------------------------------------------------------- the invariant, for tests and `muse permissions status`

    def orphans(self) -> list[str]:
        """Everything that breaks the invariant right now, in words. Empty when it holds."""
        from muse import apps
        from muse.home import folders

        revoked, now = self.revocations(), self.now()
        live = {m.id for m in self.mandates() if self.state_of(m, revoked, now) in ("active", "not_yet_valid")}
        known = {m.id for m in self.mandates()}
        out = [f"artifact row for an unknown permission ({r['target']}/{r['key']})" for r in self.table.rows() if r["mandate_id"] not in known]
        out += [f"app {a.name} names a permission that is not in force" for a in apps.load_apps(self.environ).values()
                if a.mandate and a.mandate not in live]
        out += [f"folder {e['name']} names a permission that is not in force" for e in folders.load(self.environ).values()
                if e.get("mandate") and e["mandate"] not in live]
        for name, token in self.tokens().items():
            if token.levels and token.id not in live:
                out.append(f"narrowed token {name} belongs to a permission that is not in force")
        return out


def _holder_word(holder: str) -> str:
    """A holder as a record's summary may name it: its kind and, unless it is shaped like a key, its name."""
    from muse.records.format import key_shaped

    kind, _, ident = holder.partition(":")
    words = {"helper": "the helper", "app": "the app", "device": "the device", "clinician": "a clinician"}.get(kind, "a holder")
    return words if key_shaped(ident) or len(ident) > 40 else f"{words} {ident}"
