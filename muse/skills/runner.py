"""Run one skill as a job: budget, privacy class, the skill's own output check, one stronger retry.

    result = run("files-answer", {"question": "..."}, folders=["~/notes"])

What happens:
  1. The skill must be ready (shipped, or approved and unchanged). Its `needs`
     are bound to the folders named for this run.
  2. The skill's entry runs with a `Context`. It reaches models only through
     `ctx.llm` (muse/skills/llm.py) and files only through `ctx.workspace`.
  3. The skill's own check runs on what it returned. A failed check is retried
     ONCE with every chat step one tier stronger (light -> everyday -> heavy).
  4. If the check still fails, or the router deferred a step the skill could
     not do without, the result is `needs_choice` carrying exactly the options
     the router offered (wait until a time, hand off, pay about an amount),
     possibly none. The runner never invents an option and never lowers a
     privacy class.
  5. Writes are proposals. They are applied only with `apply_writes=True` (or a
     later `apply_writes(result)`), and only when the check passed.

Events (and `on_event`) carry counts, tiers, providers, costs and reasons:
never prompts, answers or file contents.
"""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from muse import paths, policy
from muse.sandbox import policy as sandbox_policy
from muse.skills import app as app_mod
from muse.skills import loader
from muse.skills.format import TIERS, Skill
from muse.skills.llm import (
    Budget, BudgetExceeded, Client, Deferred, Meter, Options, PrivacyRefused, RAISE_ORDER, RouterError, http_post,
    raised,
)
from muse.skills.workspace import NeedsViolation, ProposedWrite, Workspace

STATUSES = ("done", "partial", "needs_choice", "failed")
EXIT_CODES = {"done": 0, "partial": 3, "needs_choice": 4, "failed": 1}
MAX_WAITS = 3


@dataclass
class Result:
    status: str
    output: Any = None
    notes: list[str] = field(default_factory=list)
    check: dict[str, Any] = field(default_factory=lambda: {"status": "not-run", "reasons": []})
    used: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)  # exactly what the router offered; only for needs_choice
    refusal: dict[str, Any] | None = None  # {"kind": "privacy", "privacy": "local"} when the router refused for good
    writes: list[ProposedWrite] = field(default_factory=list)
    skill: str = ""
    privacy: str = ""
    workspace: Workspace | None = field(default=None, repr=False)
    sandbox: dict[str, Any] | None = None  # how isolated the run was (muse.sandbox.Report.public()); None = the sandbox was not involved

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def public(self) -> dict[str, Any]:
        """Everything as plain data. `output` is the skill's answer and may hold the person's content."""
        out = {"skill": self.skill, "status": self.status, "output": self.output, "notes": list(self.notes),
               "check": self.check, "used": self.used, "options": self.options, "refusal": self.refusal,
               "privacy": self.privacy, "writes": [w.public() for w in self.writes], "events": self.events}
        if self.sandbox is not None:
            out["sandbox"] = self.sandbox
        return out


class SkillStop(Exception):
    """Raised by `ctx.stop(...)`: the skill cannot go on, and the message is a plain sentence for the person."""


class Context:
    """What a skill's entry and check receive. This is the whole surface a skill should use."""

    def __init__(self, skill: Skill, inputs: Mapping[str, Any], llm: Client, workspace: Workspace, data_dir: Path | None,
                 privacy: str, attempt: int, emit: Callable[..., None]) -> None:
        self.skill_name = skill.name
        self.inputs: dict[str, Any] = dict(inputs)
        self.llm = llm
        self.workspace = workspace
        self.privacy = privacy
        self.attempt = attempt  # 1, or 2 on the stronger retry
        self._data_dir = data_dir
        self._emit = emit
        self.notes: list[str] = []
        self.incomplete = False
        self.kept: Any = None  # see keep()

    @property
    def data_dir(self) -> Path:
        """The skill's own working folder (need `data`), created on first use."""
        if self._data_dir is None:
            raise NeedsViolation("this skill did not declare `data` in its needs, so it has no working folder")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._data_dir.chmod(0o700)
        except OSError:
            pass
        return self._data_dir

    def note(self, text: str, *, incomplete: bool = False) -> None:
        """A plain sentence for the person. `incomplete=True`: something could not be done, so the result is partial."""
        text = " ".join(str(text).split())
        if text and text not in self.notes:
            self.notes.append(text)
        self.incomplete = self.incomplete or incomplete

    def keep(self, output: Any) -> None:
        """What the person should still get if a later step cannot be done (deferred, refused, over budget).

        A long job calls this with its partial result before a step it may not get. If that step then ends the
        run, the runner returns this output (after the skill's own check) with the status it would have given
        anyway: `needs_choice` with the router's options for a deferral, `partial` for a budget stop.
        """
        self.kept = output

    def stop(self, message: str) -> None:
        """End the run as failed with one plain sentence (a missing input, nothing to work on). No content in it."""
        raise SkillStop(" ".join(str(message).split())[:300])

    def event(self, kind: str, **facts: Any) -> None:
        """A progress fact for the app. Numbers and short codes only: never text from a file, a prompt or an answer."""
        self._emit(f"skill.{kind}", **facts)


def _safe_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Events hold numbers, booleans, short codes and lists of those. Anything long or nested is dropped."""
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value if len(value) <= 80 else "(long text removed)"
        elif key == "options" and isinstance(value, list):
            out[key] = value  # built by the client from the router's trailer
        elif isinstance(value, (list, tuple)) and all(isinstance(v, (bool, int, float)) for v in value):
            out[key] = list(value)
    return out


def _where(exc: BaseException) -> str:
    remote = getattr(exc, "sandbox_where", None)  # an error raised inside the sandbox: the child reported file:line
    if isinstance(remote, str):
        return remote
    frames = traceback.extract_tb(exc.__traceback__)
    return f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""


def _reasons(value: Any) -> list[str]:
    """A check returns a list of reasons (empty = passed), or None/True for passed, or False."""
    if value is None or value is True:
        return []
    if value is False:
        return ["the check failed"]
    if isinstance(value, str):
        return [value]
    return [" ".join(str(item).split())[:300] for item in value]


def run(
    skill: Skill | str,
    inputs: Mapping[str, Any] | None = None,
    *,
    budget: Mapping[str, Any] | None = None,
    privacy: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    apply_writes: bool = False,
    folders: Sequence[str | os.PathLike[str]] = (),
    out_dir: str | os.PathLike[str] | None = None,
    task: str | None = None,
    environ: Mapping[str, str] | None = None,
    wait_up_to: float = 0.0,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    post: Callable[..., Any] = http_post,
    background: bool = False,
    sandbox: str | None = None,
    sandbox_limits: Any = None,
    permission_token: str | None = None,
    _executor: Any = None,
    _sandbox_decided: bool = False,
) -> Result:
    """Run a skill once. Never raises for a skill's or the router's failure: the Result says what happened.

    budget      changes to the skill's declared budget for this run (requests, tokens, seconds, paid_usd)
    privacy     the job's privacy class (default: the skill's); every model call carries it
    folders     the folders the person names for this run (need `files.read: [chosen]`)
    out_dir     the output folder (need `files.write: [output]`); default MUSE_HOME/skills-out/<name>/
    task        the tier for steps that name none (default: the skill's `tier_default`)
    wait_up_to  the longest Retry-After (seconds) the runner will sit out, inside the time budget; 0 = never wait
    background  send as the background app `skills-background`, which the router holds back while allowance is
                needed elsewhere (spare-allowance chores); ignored for a run with a money budget
    sandbox     `off`, `auto` or `require` (default: MUSE_SANDBOX, else `off`). Anything but `off` runs the skill's
                code in a separate locked-down process (muse/sandbox, docs/sandbox.md). A skill that does not ship
                with Muse always runs with `require`, whatever is asked for here.
    sandbox_limits, permission_token   passed on to muse.sandbox.run_skill (a `Policy`; an optional permission token)
    """
    env = os.environ if environ is None else environ
    events: list[dict[str, Any]] = []
    started = clock()

    def emit(kind: str, /, **facts: Any) -> None:
        event = {"t": round(clock() - started, 3), "kind": kind, **_safe_facts(facts)}
        events.append(event)
        if on_event is not None:
            try:
                on_event(dict(event))
            except Exception:
                pass  # a listener's bug must not change the job

    def failed(note: str, name: str = "") -> Result:
        emit("end", status="failed")
        return Result("failed", notes=[note], events=events, skill=name)

    # ---- 1. the skill, its budget, privacy class and folders
    try:
        found = loader.find(skill, env) if isinstance(skill, str) else skill
        loader.require_ready(found)
        if found.needs.net:
            raise loader.LoaderError(f"skill '{found.name}' asks for internet access of its own; that needs the sandbox "
                                     "to hold it to a list of hosts, which it cannot do yet, so it cannot run")
        if not _sandbox_decided:
            mode = sandbox_policy.mode_for(found.source, sandbox, env)
            if mode != "off":
                from muse import sandbox as sandbox_mod  # the sandbox runs this same function, with `_executor` set

                return sandbox_mod.run_skill(
                    found, inputs, roots=folders, budget=budget, privacy=privacy, token=permission_token,
                    policy=sandbox_limits, mode=mode, on_event=on_event, apply_writes=apply_writes, out_dir=out_dir,
                    task=task, environ=environ, wait_up_to=wait_up_to, clock=clock, sleep=sleep, post=post,
                    background=background)[0]
        entry, check = loader.entry_points(found) if _executor is None else (None, None)
    except (loader.LoaderError, ValueError) as exc:
        return failed(str(exc), skill if isinstance(skill, str) else skill.name)
    name = found.name
    try:
        job_budget = Budget.of(found.budget, budget)
        cls = policy.normalise_class(privacy or found.privacy_default)
        tier = task or found.tier_default
        if tier not in TIERS:
            raise ValueError(f"task must be one of {', '.join(TIERS)}")
        workspace = _workspace(found, folders, out_dir, env)
    except (ValueError, NeedsViolation) as exc:
        return failed(str(exc), name)
    data_dir = loader.data_dir(name, env) if "data" in found.needs.files_read + found.needs.files_write else None

    meter = Meter(job_budget, clock=clock, started=started)
    paid = job_budget.paid_usd > 0
    try:
        token = app_mod.ensure_app(app_mod.app_for(paid, background), env)
    except Exception as exc:
        return failed(f"Could not prepare the skills app on the router ({type(exc).__name__}).", name)
    waits = {"n": 0}

    def on_deferred(deferred: Deferred) -> bool:
        wait = deferred.retry_after
        if wait is None or wait > wait_up_to or waits["n"] >= MAX_WAITS or wait >= meter.remaining()["seconds"]:
            return False
        waits["n"] += 1
        emit("wait", seconds=wait, reason=deferred.reason)
        sleep(max(wait, 0.05))
        return True

    emit("start", skill=name, version=found.version, privacy=cls, task=tier, source=found.source,
         budget_requests=job_budget.requests, budget_tokens=job_budget.tokens, budget_seconds=job_budget.seconds,
         budget_paid_usd=job_budget.paid_usd)
    if _executor is not None:
        try:  # start the sandboxed process and load the skill in it; a refusal is one plain sentence
            _executor.start(found, job_budget, workspace, data_dir, env, emit)
        except loader.LoaderError as exc:
            return failed(str(exc), name)

    def attempt(number: int) -> tuple[Context, Any, BaseException | None]:
        client = Client(
            proxy_url(env), token, meter, default_task=tier, privacy=cls, raise_steps=number - 1, allow_paid=paid,
            on_event=emit, on_deferred=on_deferred, refresh_token=lambda: app_mod.ensure_app(app_mod.app_for(paid, background), env),
            post=post,
        )
        workspace.proposed.clear()
        if _executor is not None:
            return _executor.attempt(number, inputs or {}, client, workspace, cls, emit)
        ctx = Context(found, inputs or {}, client, workspace, data_dir, cls, number, emit)
        try:
            return ctx, entry(ctx), None
        except Exception as exc:  # the skill's or the router's failure: classified below
            return ctx, None, exc

    def finish(status: str, ctx: Context | None, output: Any, check_state: dict[str, Any], notes: list[str],
               options: Options | None = None, refusal: dict[str, Any] | None = None) -> Result:
        result = Result(
            status, output=output, notes=notes, check=check_state, used=meter.used(), events=events,
            options=options.as_list() if options is not None else [], refusal=refusal, skill=name, privacy=cls,
            writes=list(workspace.proposed) if ctx is not None and check_state["status"] == "passed" else [],
            workspace=workspace,
        )
        for write in result.writes:
            emit("write_proposed", size=len(write.data))
        if apply_writes and status in ("done", "partial"):
            _apply(result, emit)
        emit("end", status=status, requests=meter.requests, tokens=meter.tokens, cost_usd=round(meter.cost_usd, 6))
        return result

    def run_check(ctx: Context, output: Any) -> dict[str, Any]:
        try:
            reasons = _reasons(check(output, ctx) if _executor is None else _executor.check(ctx, output))
        except Exception as exc:
            reasons = [f"the check itself stopped with an error ({type(exc).__name__})"]
        state = {"status": "failed" if reasons else "passed", "reasons": reasons}
        emit("check", attempt=ctx.attempt, status=state["status"], reasons=len(reasons))
        return state

    def stopped(ctx: Context, exc: BaseException, kept: tuple[Any, dict[str, Any], list[str]] | None) -> Result:
        """An attempt ended in an exception. `kept` is the first attempt's (output, check, notes) on the retry."""
        output, check_state, notes = kept if kept is not None else (None, {"status": "not-run", "reasons": []}, [])
        notes = list(notes) + [n for n in ctx.notes if n not in notes]
        if kept is None and ctx.kept is not None and isinstance(exc, (Deferred, PrivacyRefused, BudgetExceeded, RouterError)):
            output, check_state = ctx.kept, run_check(ctx, ctx.kept)  # the skill's partial result (ctx.keep)
        if isinstance(exc, Deferred):
            options = exc.options
            when = f" It said to come back in about {_duration(exc.retry_after)}." if exc.retry_after else ""
            notes.append(("The stronger model needed for a second try" if kept is not None else "A model this job needs")
                         + f" is not free right now ({exc.reason or 'rate limit'}).{when}")
            if not options.as_list():
                notes.append("The router offered no other choice right now.")
            return finish("needs_choice", ctx, output, check_state, notes, options)
        if isinstance(exc, PrivacyRefused):
            notes.append(f"Refused for privacy: no service allowed for the '{exc.privacy}' class could take this. "
                         "Nothing was sent, and Muse did not lower the privacy class.")
            return finish("failed", ctx, output, check_state, notes, refusal={"kind": "privacy", "privacy": exc.privacy})
        if isinstance(exc, BudgetExceeded):
            notes.append(f"Stopped: this job's budget of {exc.limit:g} {_budget_word(exc.which)} was used up. Nothing more was sent.")
            return finish("partial" if output is not None else "failed", ctx, output, check_state, notes)
        if isinstance(exc, RouterError):
            detail = f" {exc.detail}" if exc.detail else ""
            hint = " Start it with: muse up" if exc.status == 0 else ""
            notes.append(f"The router could not answer: {exc.kind}" + (f" (HTTP {exc.status})" if exc.status else "") + f".{detail}{hint}")
            return finish("partial" if output is not None else "failed", ctx, output, check_state, notes)
        if isinstance(exc, SkillStop):
            notes.append(str(exc))
            return finish("failed", ctx, None, {"status": "not-run", "reasons": []}, notes)
        if isinstance(exc, NeedsViolation):
            notes.append(f"Stopped: the skill asked for something it did not declare ({exc}).")
            return finish("failed", ctx, None, {"status": "not-run", "reasons": []}, notes)
        emit("error", error=type(exc).__name__, where=_where(exc))
        notes.append(f"The skill stopped with an error ({type(exc).__name__} at {_where(exc) or 'an unknown place'}).")
        return finish("partial" if output is not None else "failed", ctx, output, check_state, notes)

    # ---- 2. first attempt
    ctx, output, exc = attempt(1)
    if exc is not None:
        return stopped(ctx, exc, None)
    state = run_check(ctx, output)
    if state["status"] == "passed":
        return finish("partial" if ctx.incomplete else "done", ctx, output, state, list(ctx.notes))

    # ---- 3. one retry, one tier stronger
    first = (output, state, list(ctx.notes))
    chat_tasks = [a.get("task", "") for a in ctx.llm.answers]
    if any(raised(t, 1) != t for t in chat_tasks):
        emit("retry", reason="check-failed", raise_steps=1)
        ctx2, output2, exc2 = attempt(2)
        if exc2 is not None:
            result = stopped(ctx2, exc2, first)
            if result.status != "needs_choice":
                result.notes.append("The first answer did not pass its check, and the stronger second try could not finish. "
                                    "The first answer is included, marked as not checked.")
            return result
        state2 = run_check(ctx2, output2)
        if state2["status"] == "passed":
            notes = list(ctx2.notes)
            notes.append("The first answer did not pass its check; this one is from a stronger model and passed.")
            return finish("partial" if ctx2.incomplete else "done", ctx2, output2, state2, notes)
        ctx, output, state = ctx2, output2, state2
        handoff = any(a.get("handoff") for a in ctx2.llm.answers)
        tried = "also after one retry on a stronger model"
    else:
        handoff = any(a.get("handoff") for a in ctx.llm.answers)
        tried = (f"and it already ran on the strongest tier Muse retries on ({RAISE_ORDER[-1]})" if chat_tasks
                 else "and it used no model, so a stronger one would not change it")
    notes = list(ctx.notes)
    notes.append(f"This answer did not pass its check, {tried}. It is included, marked as not checked: " + "; ".join(state["reasons"][:5]) + ".")
    options = Options(handoff=handoff, offered=handoff)  # the hand-off only when the router suggested it on an answer
    if not handoff:
        notes.append("The router offered no other choice right now.")
    return finish("needs_choice", ctx, output, state, notes, options)


def proxy_url(env: Mapping[str, str]) -> str:
    explicit = str(env.get("MUSE_PROXY_URL", "") or "").strip()
    return (explicit or f"http://127.0.0.1:{str(env.get('MUSE_PORT', '') or '').strip() or '4000'}").rstrip("/")


def _budget_word(which: str) -> str:
    return {"paid_usd": "US dollars"}.get(which, which)


def _duration(seconds: float | None) -> str:
    s = int(seconds or 0)
    if s < 90:
        return f"{s} seconds"
    if s < 5400:
        return f"{round(s / 60)} minutes"
    return f"{round(s / 3600)} hours"


def _workspace(skill: Skill, folders: Sequence[str | os.PathLike[str]], out_dir: str | os.PathLike[str] | None,
               env: Mapping[str, str]) -> Workspace:
    """Bind the skill's declared needs to the folders named for this run."""
    roots: list[Path] = []
    if folders and "chosen" not in skill.needs.files_read:
        raise NeedsViolation(f"skill '{skill.name}' did not ask to read folders, so none can be given to it")
    for folder in folders:
        path = Path(os.fspath(folder)).expanduser()
        if not path.is_dir():
            raise NeedsViolation(f"not a folder: {path}")
        roots.append(path)
    write_root: Path | None = None
    if "output" in skill.needs.files_write:
        write_root = Path(os.fspath(out_dir)).expanduser() if out_dir is not None else paths.home(env) / "skills-out" / skill.name
    elif out_dir is not None:
        raise NeedsViolation(f"skill '{skill.name}' did not ask for an output folder, so none can be given to it")
    return Workspace(roots, write_root, may_write=skill.writes)


def _apply(result: Result, emit: Callable[..., None]) -> None:
    if result.workspace is None:
        return
    for write in result.writes:
        if write.applied:
            continue
        ok = result.workspace.apply(write)
        emit("write_applied" if ok else "write_refused", size=len(write.data))
        if not ok:
            result.notes.append(f"Not written: {write.path} ({write.refused}).")


def apply_writes(result: Result) -> list[ProposedWrite]:
    """Write the files a finished run proposed, after the person agreed. Returns them with `applied` set.

    Only for a result whose check passed (others carry no proposals). New files
    only: an existing file is never overwritten.
    """
    if result.status in ("done", "partial"):
        _apply(result, lambda kind, **facts: result.events.append({"t": None, "kind": kind, **_safe_facts(facts)}))
    return result.writes
