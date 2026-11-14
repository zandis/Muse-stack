"""Skills through the home API: list them, run one, and write what it proposed once the person agrees.

    GET  /v1/skills                 what there is, in plain words, and whether each may run
    POST /v1/skills/{name}/run      start a run; returns its id at once
    GET  /v1/skills/runs/{id}       how it went, and what became of the files it proposed

What a caller may and may not choose (a caller may be a host bot):
  * The skill must be ready on the home: shipped, or approved by the person and unchanged.
  * Folders are given by NAME from the person's list (muse/home/folders.py), never by path.
  * A budget, a privacy class and a task tier may be given, but never beyond the skill's own:
    no budget number above the skill's, no privacy class weaker than the skill's, no tier
    stronger than the skill's. Money is zero unless an `owner` device asks for some.
  * The run goes through the ordinary runner (muse/skills/runner.py) with nothing applied.

Card -> write. A skill only proposes files. For each proposed file the home makes a `save_file`
card and keeps the bytes (muse/home/state.py). Nothing is written until that card is approved:

    approved   the home writes the file, by the runner's own rule (a new file only, inside the
               output folder, re-checked at the moment of writing), and records `files.write`
    declined   the bytes are erased; nothing is written
    expired    the same as declined

`settle_card` runs right after an answer; `sweep` catches every other way a card can end (it
expired, or it was answered while the home was down). A write is claimed with a compare-and-set
before anything is done, so it is applied at most once however many answers and sweeps race.

Runs happen on threads of the home's own process, at most MAX_RUNS at once and one at a time for
a device that is not the person's. A run cut off by the home stopping is reported as failed.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from muse.home import folders as folder_list
from muse.home.cards import CardError
from muse.home.state import HomeState

if TYPE_CHECKING:  # pragma: no cover
    from muse.home.api import Home
    from muse.home.auth import Device

log = logging.getLogger("muse.home")

MAX_RUNS = 2  # at once, for the whole home
MAX_RUNS_PER_HELPER = 1  # at once, for one device that is not the person's
MAX_INPUTS = 32
MAX_INPUT_CHARS = 20_000
MAX_INPUTS_TOTAL = 60_000
MAX_FOLDERS_PER_RUN = 8
MAX_WRITES_PER_RUN = 20  # proposed files that become cards; more than that are dropped and the run says so
BUDGET_FIELDS = ("requests", "tokens", "seconds", "paid_usd")
RUN_REF = "skill-run:"

INTERRUPTED = "The home stopped while this skill was running. Nothing it proposed was written."


class _Stopped(Exception):
    """Inside `settle_card`: the write was already refused; skip to the bookkeeping."""


class SkillRefused(Exception):
    """Why a run was not started. `code` is an API error code; `message` a fixed plain sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


def _refuse(message: str, code: str = "invalid_request") -> SkillRefused:
    return SkillRefused(code, message)


# ---------------------------------------------------------------- listing


def listing(env: Mapping[str, str]) -> dict[str, Any]:
    """Every loadable skill as plain data, and how many folders could not be loaded."""
    from muse.skills import loader
    from muse.skills.format import needs_in_words

    skills, broken = loader.discover(env)
    out = []
    for skill in sorted(skills.values(), key=lambda s: s.name):
        out.append({
            "name": skill.name, "description": skill.description, "version": skill.version, "source": skill.source,
            "approved": skill.state == "ready" and not skill.needs.net, "state": skill.state,
            "needs": needs_in_words(skill),
            "reads_folders": "chosen" in skill.needs.files_read, "writes_files": bool(skill.writes),
            "tier_default": skill.tier_default, "privacy_default": skill.privacy_default, "budget": skill.budget.as_dict(),
        })
    return {"skills": out, "broken": len(broken)}


# ---------------------------------------------------------------- what a run may be given


def check_request(skill: Any, body: Mapping[str, Any], device: "Device", env: Mapping[str, str]) -> dict[str, Any]:
    """The runner's arguments for this request, or SkillRefused. Nothing here trusts the caller."""
    from muse import policy
    from muse.skills.format import TIERS

    inputs = body.get("inputs") or {}
    if len(inputs) > MAX_INPUTS:
        raise _refuse(f"A run takes at most {MAX_INPUTS} inputs.")
    total = 0
    for key, value in inputs.items():
        if not isinstance(key, str) or not key or len(key) > 64 or not (key[0].isalpha() or key[0] == "_") \
                or not all(c.isalnum() or c == "_" for c in key) or not key.isascii():
            raise _refuse("An input name must be letters, digits and '_', starting with a letter.")
        if isinstance(value, str):
            if len(value) > MAX_INPUT_CHARS or "\x00" in value:
                raise _refuse(f"An input is longer than {MAX_INPUT_CHARS} characters.")
            total += len(value)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise _refuse("An input must be text, a number, true or false.")
    if total > MAX_INPUTS_TOTAL:
        raise _refuse(f"The inputs are longer than {MAX_INPUTS_TOTAL} characters in all.")

    names = body.get("folders") or []
    if len(names) > MAX_FOLDERS_PER_RUN or len(set(map(str, names))) != len(names):
        raise _refuse(f"A run takes at most {MAX_FOLDERS_PER_RUN} different folders.")
    if names and "chosen" not in skill.needs.files_read:
        raise _refuse("That skill does not read folders, so none can be given to it.")
    roots: list[Path] = []
    for name in names:
        try:
            roots.append(folder_list.resolve(name, environ=env))
        except folder_list.FolderError:
            raise _refuse("A folder name is not on the person's list, or its folder is no longer there. "
                          "`muse skills folders list` on the home shows the names.") from None
    out_dir: Path | None = None
    if body.get("out") is not None:
        if "output" not in skill.needs.files_write:
            raise _refuse("That skill does not propose files, so it cannot be given an output folder.")
        try:
            out_dir = folder_list.resolve(body["out"], for_output=True, environ=env)
        except folder_list.FolderError:
            raise _refuse("The output folder name is not on the person's list as a folder for new files. "
                          "The person adds one with `muse skills folders add NAME PATH --output`.") from None

    budget: dict[str, Any] = {"paid_usd": 0}  # money only when asked for, below
    asked = body.get("budget") or {}
    for key, value in asked.items():
        if key not in BUDGET_FIELDS:
            raise _refuse("budget takes requests, tokens, seconds and paid_usd.")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
            raise _refuse("Every budget value must be a number, zero or more.")
        if value > getattr(skill.budget, key):
            raise _refuse("A budget value is above this skill's own limit. A run can be given less, never more.")
        budget[key] = value
    if budget["paid_usd"] > 0 and device.role != "owner":
        raise _refuse("Only the person's own device can give a skill money to spend.", "forbidden")

    privacy = body.get("privacy")
    if privacy is not None:
        order = policy.PRIVACY_CLASSES
        if privacy not in order:
            raise _refuse(f"privacy must be one of {', '.join(order)}.")
        if order.index(privacy) < order.index(policy.normalise_class(skill.privacy_default)):
            raise _refuse("privacy cannot be weaker than the skill's own privacy class.")
    task = body.get("task")
    if task is not None:
        if task not in TIERS:
            raise _refuse(f"task must be one of {', '.join(TIERS)}.")
        if TIERS.index(task) > TIERS.index(skill.tier_default):
            raise _refuse("task cannot be a stronger tier than the skill's own.")
    return {"inputs": dict(inputs), "folders": roots, "out_dir": out_dir, "budget": budget, "privacy": privacy, "task": task}


# ---------------------------------------------------------------- runs


class SkillRuns:
    """Starts runs, keeps their results, and settles the files they proposed."""

    def __init__(self, home: "Home", runner: Callable[..., Any] | None = None) -> None:
        self.home = home
        self.book: HomeState = home.book
        self.runner = runner  # muse.skills.runner.run unless a test gives another
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}  # run id -> device name, for runs on this process's threads
        self._threads: dict[str, threading.Thread] = {}

    # ---- starting

    def start(self, device: "Device", name: str, body: Mapping[str, Any]) -> dict[str, Any]:
        from muse.skills import loader

        env = self.home.run_env()
        try:
            skill = loader.find(name, env)
        except loader.LoaderError:
            raise _refuse("There is no skill with that name on the home.", "not_found") from None
        try:
            loader.require_ready(skill)
        except loader.LoaderError:
            raise _refuse("That skill is not approved on the home, or it changed since it was approved. "
                          "The person looks at it with `muse skills approve` on the home.", "conflict") from None
        if skill.needs.net:
            raise _refuse("That skill asks for internet access of its own, which Muse cannot give yet, so it cannot run.", "conflict")
        args = check_request(skill, body, device, env)
        with self._lock:
            mine = sum(1 for owner in self._active.values() if owner == device.name)
            if len(self._active) >= MAX_RUNS or (device.role != "owner" and mine >= MAX_RUNS_PER_HELPER):
                raise _refuse("A skill is already running. Wait for it to finish, then try again.", "conflict")
            run_id = self.book.new_run(skill.name, device.name, device.role)
            self._active[run_id] = device.name
        thread = threading.Thread(target=self._work, args=(run_id, device, skill, args, env), name=f"skill-run-{run_id}", daemon=True)
        self._threads[run_id] = thread
        started = self.public(self.book.get_run(run_id), device)  # read before the run starts: the reply always says `running`
        thread.start()
        return started

    def wait(self, run_id: str, timeout: float = 30.0) -> None:
        """For tests and for a clean shutdown: block until a run started here has finished."""
        thread = self._threads.get(run_id)
        if thread is not None:
            thread.join(timeout)

    # ---- the worker thread

    def _work(self, run_id: str, device: "Device", skill: Any, args: Mapping[str, Any], env: Mapping[str, str]) -> None:
        from muse.home.api import actor_of, mandate_of

        status, public, used = "failed", {}, {}
        cards: list[str] = []
        try:
            run = self.runner
            if run is None:
                from muse.skills import runner as runner_mod

                run = runner_mod.run
            extra: dict[str, Any] = {}
            if self.runner is None:  # the real runner: how it asks the person and records a refusal (muse/compiler/guard.py)
                extra = {"permission_confirm": self._confirmer(run_id, skill.name, device), "permission_record": self.home.record}
            result = run(skill, args["inputs"], budget=args["budget"], privacy=args["privacy"], folders=args["folders"],
                         out_dir=args["out_dir"], task=args["task"], environ=env, apply_writes=False, **extra)
            status = result.status
            public = {k: v for k, v in result.public().items() if k not in ("events", "writes", "skill")}
            used = dict(result.used or {})
            notes = list(public.get("notes") or [])
            if result.writes and status in ("done", "partial"):
                more, cards = self._propose(run_id, skill.name, result)
                notes += more
            public["notes"] = notes
        except Exception as exc:  # the runner reports a skill's own failures in its result; this is a bug or a broken store
            log.error("skills: a run could not be finished (%s)", type(exc).__name__)
            status = "failed"
            public = {"output": None, "notes": [f"The home hit a problem running this skill ({type(exc).__name__}). Nothing was written."],
                      "check": {"status": "not-run", "reasons": []}, "used": {}, "options": [], "refusal": None, "privacy": ""}
        finally:
            try:
                self.book.finish_run(run_id, status, public)
            except Exception as exc:
                log.error("skills: a run's result could not be kept (%s)", type(exc).__name__)
            with self._lock:
                self._active.pop(run_id, None)
        who = {"actor": actor_of(device), "mandate": mandate_of(device)}
        ended = {"done": "ok", "partial": "ok"}.get(status, "failed")
        # The answer goes back to whoever asked. For a host bot that is a program Muse does not control.
        egress = "none" if device.role == "owner" else f"host:{device.name}"
        self.home.record("skill.run", f"Ran the skill {skill.name}" + ("" if device.role == "owner" else " for a host bot")
                         + ("" if ended == "ok" else ": it did not finish"), outcome=ended, egress=egress, **who)
        for provider, numbers in sorted((used.get("providers") or {}).items()):
            where = self._service(provider)
            if where is None:
                continue
            requests = numbers.get("requests") if isinstance(numbers, Mapping) else None
            count = f": {requests} request{'s' if requests != 1 else ''}" if isinstance(requests, int) else ""
            self.home.record("llm.call", f"The skill {skill.name} used the service {provider}{count}", egress=where, **who)
        for card_id in cards:
            self.home.record("card.create", "Asked whether to save a file a skill proposed", links={"card": card_id}, egress="none", **who)
        self._threads.pop(run_id, None)  # the last thing: `wait` covers the records too

    def _confirmer(self, run_id: str, skill: str, device: "Device") -> Callable[[str, Mapping[str, Any]], str | None]:
        """How a run asks when its permission says "ask first" about something that is not a file (a file gets its
        `save_file` card after the run). Today that is paying for a model: a `spend` card, waited on for as long as
        the card lives. Returns the id of the card when it was approved, None otherwise."""
        from muse.home.api import actor_of, mandate_of

        def ask(action: str, facts: Mapping[str, Any]) -> str | None:
            if action != "llm.paid":
                return None
            amount = f"{max(float(facts.get('paid_usd') or 0.0), 0.0):.2f}"
            short = str(facts.get("mandate") or "")[:15] or None
            try:
                card_id = self.home.cards.create(
                    kind="spend", title="Use a paid model", thing=f"The skill {skill} wants to use a paid model",
                    under={"text": "Your permission asks before every payment", **({"mandate_id": short} if short else {})},
                    cost={"amount": amount, "currency": "USD"},
                    extra={"payee": "A paid model service, through Muse", "limit_left": {"amount": amount, "currency": "USD"}},
                    job_ref=f"{RUN_REF}{run_id}")
            except CardError:
                return None
            self.home.record("card.create", "Asked whether a skill may use a paid model", links={"card": card_id}, egress="none",
                             actor=actor_of(device), mandate=mandate_of(device))
            from muse.home.cards import SPEND_TTL_S

            return card_id if self.home.cards.wait(card_id, SPEND_TTL_S + 1.0) == "approved" else None

        return ask

    def _service(self, provider: str) -> str | None:
        """`service:<id>` for a provider outside this computer; None for a local model, the cache, or a name that is not an id."""
        import re

        if provider in ("cache", "unknown", "") or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", provider):
            return None
        try:
            local = {p.id for p in self.home.load_catalogue().providers if p.local}
        except Exception:
            local = set()
        return None if provider in local else f"service:{provider}"

    def _propose(self, run_id: str, skill: str, result: Any) -> tuple[list[str], list[str]]:
        """One `save_file` card per proposed file, with the bytes kept until the card is answered.
        Returns (notes for the run, the ids of the cards made)."""
        notes: list[str] = []
        cards: list[str] = []
        root = str(result.workspace.write_root) if result.workspace is not None and result.workspace.write_root is not None else ""
        made = 0
        for write in result.writes:
            if not root or made >= MAX_WRITES_PER_RUN:
                break
            path = Path(write.path)
            where = str(path.parent)
            try:
                card_id = self.home.cards.create(
                    kind="save_file", title="Save a file", thing=path.name, where=where if len(where) <= 200 else "…" + where[-199:],
                    under=f"The skill {skill} proposed this file", extra={"size_bytes": len(write.data)},
                    job_ref=f"{RUN_REF}{run_id}", title_key="save_file_skill", params={"skill": skill})
            except CardError:
                notes.append("A proposed file could not be put to the person as a question (its name was not accepted, or too many "
                             "questions are waiting), so it will not be written.")
                continue
            try:
                self.book.add_write(card_id=card_id, run_id=run_id, skill=skill, root=root, path=str(path), data=bytes(write.data))
            except Exception as exc:
                log.error("skills: a proposed file could not be kept (%s)", type(exc).__name__)
                notes.append("A proposed file could not be kept on the home, so it will not be written.")
                continue
            guard = getattr(result, "guard", None)
            if guard is not None:  # which permission token asked: approving the card mints the single-use token from it
                try:
                    guard.pending_write(card_id, str(path), result.privacy or "standard")
                except Exception as exc:
                    log.error("skills: a proposed file's permission could not be noted (%s)", type(exc).__name__)
            made += 1
            cards.append(card_id)
        dropped = len(result.writes) - made
        if made:
            notes.append(f"{made} file{'s were' if made != 1 else ' was'} proposed. Nothing is written until the person approves "
                         "each one in Muse.")
        if dropped > 0 and made >= MAX_WRITES_PER_RUN:
            notes.append(f"{dropped} more proposed file{'s were' if dropped != 1 else ' was'} left out: a run may ask about at most "
                         f"{MAX_WRITES_PER_RUN}.")
        return notes, cards

    # ---- reading

    def get(self, device: "Device", run_id: str) -> dict[str, Any] | None:
        """A run this device may see, or None (also when it is somebody else's)."""
        self.sweep()
        row = self.book.get_run(run_id)
        if row is None or (not device.sees_all and row["device"] != device.name):
            return None
        if row["status"] == "running" and run_id not in self._active:
            # Started by a home that has since stopped: nobody will ever finish it.
            self.book.finish_run(run_id, "failed", {"output": None, "notes": [INTERRUPTED], "check": {"status": "not-run", "reasons": []},
                                                    "used": {}, "options": [], "refusal": None, "privacy": ""})
            row = self.book.get_run(run_id) or row
        return self.public(row, device)

    def public(self, row: Mapping[str, Any] | None, device: "Device") -> dict[str, Any]:
        from muse.home.cards import iso

        assert row is not None
        out: dict[str, Any] = {
            "id": row["id"], "skill": row["skill"], "status": row["status"], "created_at": iso(row["created_at"]),
            "finished_at": iso(row["finished_at"]), "started_by": row["device"],
            "result": row["result"] if row["status"] != "running" else None,
            "writes": [{"card_id": w["card_id"], "file": Path(w["path"]).name, "size": w["size"], "state": w["state"],
                        "reason": w["reason"] or None} for w in self.book.writes(run_id=row["id"])],
        }
        return out

    # ---- card -> write

    def settle_card(self, card: Mapping[str, Any]) -> dict[str, Any] | None:
        """Act on an answered or expired card that stands for a proposed file. None when it does not.

        Returns {"state": written | refused | discarded, "file", "reason"}. Safe to call any number of
        times and from any thread: only the caller that claims the write does anything."""
        from muse.skills.workspace import ProposedWrite, Workspace

        status = card.get("status")
        if status != "pending" and card.get("kind") == "grant_permission":
            from muse.home import mandates_api  # a device asked for a permission: approving the card issues it

            return mandates_api.settle_card(self.home, card)
        if status == "pending" or not str(card.get("job_ref") or "").startswith(RUN_REF):
            return None
        claimed = self.book.claim_write(card["id"])
        if claimed is None:
            seen = self.book.write_for(card["id"])
            return None if seen is None else {"state": seen["state"], "file": Path(seen["path"]).name, "reason": seen["reason"] or None}
        name = Path(claimed["path"]).name
        if status != "approved":
            self.book.settle_write(card["id"], "discarded", "the person said no" if status == "declined" else "nobody answered in time")
            return {"state": "discarded", "file": name, "reason": None}
        run = self.book.get_run(claimed["run_id"]) or {}
        who = {"actor": "helper", "mandate": None if run.get("role") in (None, "owner") else f"device:{run.get('device')}"}
        reason = ""
        # The approval becomes a single-use permission token for exactly this write (architecture 3.6): narrowed
        # from the token that asked, bound to this card, good for five minutes, spent here. A second use is refused.
        permit = self._permit(card["id"])
        if permit["known"]:
            who["mandate"] = permit["mandate"]
            if not permit["allowed"] and permit["mode"] == "enforce":
                from muse.compiler.guard import reason_words

                reason = f"your permissions no longer allow it: {reason_words(permit['reason'])}"
        try:
            if reason:
                raise _Stopped()
            root = claimed["root"]
            default_root = os.path.realpath(str(Path(self.home.muse_home()) / "skills-out" / claimed["skill"]))
            if not (folder_list.allowed_output_root(root, self.home.environ) or os.path.realpath(root) == default_root):
                reason = "the output folder is no longer on the person's list"
            elif claimed["data"] is None:
                reason = "the file's contents were no longer on the home"
            else:
                write = ProposedWrite(claimed["path"], bytes(claimed["data"]))
                if not Workspace((), root, may_write=True).apply(write):  # new files only, inside the folder, re-checked now
                    reason = write.refused or "the file could not be written"
        except _Stopped:
            pass
        except Exception as exc:
            log.error("skills: an approved file could not be written (%s)", type(exc).__name__)
            reason = f"the home hit a problem writing it ({type(exc).__name__})"
        self.book.settle_write(card["id"], "refused" if reason else "written", reason)
        if reason:
            self.home.record("files.write", "Could not write a file a skill proposed, although it was approved", outcome="failed",
                             links={"card": card["id"]}, egress="none", **who)
            return {"state": "refused", "file": name, "reason": reason}
        by = str(card.get("answered_by") or "")
        words = "Wrote a file a skill proposed, after it was approved"
        if permit["known"]:  # the record names the permission (above) and the device the approval came from
            from muse.home.api import _word

            words += f" on {_word(by)}" if by else ""
            if not permit["allowed"]:
                words += "; advice only: the permission would have refused it"
        self.home.record("files.write", words[:200], resource=f"file:{claimed['path']}"[:1024],
                         links={"card": card["id"]}, egress="none", **who)
        return {"state": "written", "file": name, "reason": None}

    def _permit(self, card_id: str) -> dict[str, Any]:
        """The single-use permission for an approved card's write. `known: False` when no permission was involved."""
        try:
            from muse.compiler.guard import settle_write

            return settle_write(self.home.environ, card_id, clock=self.home.clock, record=self.home.record)
        except Exception as exc:
            log.error("skills: an approval could not be turned into a permission (%s)", type(exc).__name__)
            # Fail closed when a permission might have been involved (the compiler's table exists); a home that
            # never set up permissions keeps writing approved files as it always did.
            from muse.compiler.table import table_path

            involved = table_path(self.home.environ).exists()
            return {"known": involved, "allowed": not involved, "reason": "unavailable" if involved else "", "mandate": None,
                    "mode": "enforce" if involved else ""}

    def sweep(self) -> int:
        """Settle every waiting file whose card has ended some other way than an answer through this API:
        it expired, or it was answered while nothing was looking. Never raises. Returns how many it settled."""
        settled = 0
        try:
            for waiting in self.book.writes(states=("pending",)):
                try:
                    card = self.home.cards.get(waiting["card_id"])
                except CardError:
                    card = {"id": waiting["card_id"], "status": "expired", "job_ref": f"{RUN_REF}{waiting['run_id']}"}  # the card is gone
                if card["status"] != "pending" and self.settle_card(card) is not None:
                    settled += 1
        except Exception as exc:
            log.warning("skills: waiting files could not be looked at (%s)", type(exc).__name__)
        return settled
