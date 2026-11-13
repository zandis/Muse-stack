"""The parent's side of a sandboxed run: start the child, serve its requests, turn every failure into a plain result.

`run_skill` runs one skill through the ordinary runner (muse/skills/runner.py)
with one thing swapped: the skill's entry and check run in a child process
(muse/sandbox/child.py) instead of in this one. Budget, privacy class, the
router client and its app token, the retry on a failed check, the result
statuses and the notes are the runner's own code, unchanged, in this process.

What crosses the control pipe (muse/sandbox/protocol.py):

    parent -> child   init, load (the skill's hashed bytes), attempt, check, exit, reply
    child -> parent   ready, loaded, attempt_done, check_done,
                      ask / embed / remaining   (answered by the parent's router client)
                      event                     (numbers and short codes only)

Everything from the child is untrusted. Proposed writes arrive as data and
are checked again by the parent's own `Workspace`; an error the child reports
is looked up among the errors the parent itself raised, never rebuilt from
the child's words; a frame that is malformed, too large or out of turn ends
the run. A crash, a timeout or an attempted escape becomes `failed` with one
plain sentence: never a traceback, never file contents.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from muse import paths
from muse.sandbox import launch
from muse.sandbox.policy import Capabilities, Policy, Report, normalise_mode, unsafe_home_skills
from muse.sandbox.detect import probe
from muse.sandbox.protocol import Channel, ChannelClosed, ChannelTimeout, ProtocolError, b64, unb64
from muse.skills import loader, runner
from muse.skills.format import Skill
from muse.skills.llm import BudgetExceeded, Deferred, LlmError, PrivacyRefused, RouterError
from muse.skills.workspace import MAX_PROPOSED, NeedsViolation

START_TIMEOUT_S = 30.0
NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")
WHERE_OK = re.compile(r"^[\w.\-]{1,80}:\d{1,7}$")
KIND_OK = re.compile(r"^skill\.[A-Za-z0-9_.\-]{1,50}$")
USES_MUSE = re.compile(rb"^[ \t]*(?:from|import)[ \t]+muse(?:[ \t.]|$)", re.MULTILINE)
MAX_WALK = 200_000

STOP_WORDS = {
    "timeout": "Stopped: the skill ran past its time budget and was ended.",
    "cpu": "Stopped: the skill used more processor time than its budget allows and was ended.",
    "data-cap": "Stopped: the skill filled its working folder past the allowed size and was ended.",
    "protocol": "Stopped: the skill sent something the sandbox does not accept and was ended.",
    "crashed": "Stopped: the skill's process ended unexpectedly (it crashed, ran out of memory, or was ended by the sandbox).",
    "refused": "Stopped: the skill asked for something this run's permission does not cover.",
}


class SandboxStop(runner.SkillStop):
    """The sandbox ended the run. A `SkillStop`, so the runner reports it as `failed` with this one sentence."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(STOP_WORDS.get(code, STOP_WORDS["crashed"]))


class _DataCap(Exception):
    pass


def remote_error(type_name: Any, where: Any = "") -> Exception:
    """An exception whose type NAME is the one the skill raised in the child. No message, no traceback."""
    name = type_name if isinstance(type_name, str) and NAME_OK.match(type_name) else "Exception"
    exc = type(name.rsplit(".", 1)[-1], (Exception,), {})()
    exc.sandbox_where = where if isinstance(where, str) and WHERE_OK.match(where) else ""  # type: ignore[attr-defined]
    return exc


def in_process_reason(skill: Skill) -> str:
    """Why a SHIPPED skill cannot run in the sandbox yet, or "" when it can.

    Two shipped skills read Muse's own state (the catalogue, which services have a key, the chore list) through
    Muse's modules. The sandbox hides that state, so they would answer wrongly inside it.
    """
    if skill.source != "shipped":
        return ""
    for rel, data in skill.files.items():
        if rel.endswith(".py") and USES_MUSE.search(data):
            return "this shipped skill reads Muse's own state through Muse's modules, which the sandbox hides"
    return ""


def _plain(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _tree_bytes(path: str) -> int:
    total = seen = 0
    for folder, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            seen += 1
            if seen > MAX_WALK:
                return total
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


class RemoteContext:
    """What the runner reads from a context after an attempt, filled from the child's report."""

    def __init__(self, skill: Skill, inputs: Mapping[str, Any], llm: Any, privacy: str, attempt: int) -> None:
        self.skill_name = skill.name
        self.inputs = dict(inputs)
        self.llm = llm
        self.privacy = privacy
        self.attempt = attempt
        self.notes: list[str] = []
        self.incomplete = False
        self.kept: Any = None


class Executor:
    """Runs a skill's entry and check in a sandboxed child. One per run; used by `runner.run(..., _executor=...)`."""

    def __init__(self, policy: Policy | None = None, mode: str = "require", token: str | None = None,
                 caps: Capabilities | None = None) -> None:
        self.policy = policy or Policy()
        self.mode = normalise_mode(mode)
        self.token = token
        self.caps = caps
        self.report = Report(mode=self.mode, platform=sys.platform)
        self._proc: subprocess.Popen[bytes] | None = None
        self._chan: Channel | None = None
        self._plan: launch.Plan | None = None
        self._deadline = 0.0
        self._dead: SandboxStop | None = None
        self._errors: dict[int, LlmError] = {}
        self._events = 0
        self._last_tick = 0.0
        self._watch: list[tuple[str, int]] = []
        self._emit: Any = lambda kind, **facts: None
        self._client: Any = None
        self._skill: Skill | None = None

    # ------------------------------------------------------------ start

    def _refuse(self, reason: str) -> loader.LoaderError:
        self.report.ran, self.report.reason = "refused", reason
        self.close()
        return loader.LoaderError(reason)

    def start(self, skill: Skill, budget: Any, workspace: Any, data_dir: Any, env: Mapping[str, str], emit: Any) -> None:
        """Launch the child, lock it down, load the skill. Raises loader.LoaderError with a plain sentence on refusal."""
        self._skill, self._emit = skill, emit
        policy, report = self.policy, self.report
        who = f"skill '{skill.name}'"
        if skill.source != "shipped":
            who += " is not one of Muse's own, so it runs only inside the sandbox"
        else:
            who += " was asked to run only inside the sandbox"
        if sys.platform == "win32" or not hasattr(os, "fork"):
            raise self._refuse(f"{who}, and no isolation is available on this system yet")
        reason = in_process_reason(skill)
        if reason:
            raise self._refuse(f"{who}, but {reason}")
        caps = self.caps or probe()
        self.caps = caps
        if self.mode == "require":
            fs = (caps.bwrap_works and policy.wants("bwrap")) or (caps.landlock_works and caps.landlock_abi >= 3 and policy.wants("landlock"))
            net = (caps.bwrap_works and policy.wants("bwrap")) or (caps.seccomp_works and policy.wants("seccomp"))
            if not (fs and net):
                missing = "; ".join(caps.missing()) or "the isolation layers are switched off for this run"
                raise self._refuse(f"{who}, and the sandbox is not complete here: {missing}. See `muse doctor`.")
        data = None
        if data_dir is not None:
            try:
                os.makedirs(data_dir, mode=0o700, exist_ok=True)
            except OSError:
                raise self._refuse(f"{who}, and its working folder could not be created") from None
            data = os.path.realpath(data_dir)
        to_child_r, to_child_w = os.pipe()
        from_child_r, from_child_w = os.pipe()
        try:
            plan = launch.make(caps, policy, read_roots=[str(r) for r in workspace.read_roots], data_dir=data,
                               muse_home=str(paths.home(env)), repo_root=launch.repo_root(), parent_env=env,
                               fds=(to_child_r, from_child_w))
        except launch.Unavailable as exc:
            for fd in (to_child_r, to_child_w, from_child_r, from_child_w):
                os.close(fd)
            raise self._refuse(f"{who}, and {exc}") from None
        self._plan = plan
        debug = os.environ.get("MUSE_SANDBOX_DEBUG", "") == "1"
        try:
            self._proc = subprocess.Popen(
                plan.argv, env=plan.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=None if debug else subprocess.DEVNULL, pass_fds=plan.pass_fds, close_fds=True, start_new_session=True,
                cwd=plan.host_tmp or "/")
        except OSError as exc:
            for fd in (to_child_r, to_child_w, from_child_r, from_child_w):
                os.close(fd)
            raise self._refuse(f"{who}, and the sandboxed process could not be started ({type(exc).__name__})") from None
        os.close(to_child_r)
        os.close(from_child_w)
        self._chan = Channel(from_child_r, to_child_w, policy.frame_bytes, tick=self._tick)
        seconds = float(getattr(budget, "seconds", 60.0))
        cpu = policy.cpu_seconds if policy.cpu_seconds is not None else seconds + policy.grace_seconds
        rlimits = {"AS": policy.memory_bytes, "CPU": int(cpu) + 1, "NOFILE": policy.open_files, "NPROC": policy.processes,
                   "FSIZE": policy.file_bytes, "CORE": 0}
        start_deadline = time.monotonic() + START_TIMEOUT_S
        try:
            self._chan.send({"op": "init", "rlimits": rlimits, "landlock": plan.landlock, "seccomp": plan.seccomp, "tmp": plan.tmp},
                            start_deadline)
            ready = self._chan.recv(start_deadline)
            if ready.get("op") != "ready" or not isinstance(ready.get("applied"), dict):
                raise ProtocolError("unexpected-frame")
        except (ChannelClosed, ChannelTimeout, ProtocolError):
            raise self._refuse(f"{who}, and the sandboxed process did not start"
                               + (f" ({'; '.join(plan.notes)})" if plan.notes else "")) from None
        self._fill_report(ready["applied"], plan, caps)
        report.ran = "sandboxed"
        if self.mode == "require" and not report.isolated:
            raise self._refuse(f"{who}, and the sandbox did not come up complete (files: {report.files}; network: {report.net})")
        self._deadline = time.monotonic() + seconds + policy.grace_seconds
        if data:
            self._watch.append((data, policy.data_bytes))
        if plan.host_tmp:
            self._watch.append((plan.host_tmp, policy.tmp_bytes))
        emit("sandbox", mode=self.mode, isolated=report.isolated, bwrap=report.bwrap, landlock_abi=report.landlock_abi,
             seccomp=report.seccomp, files=report.files, net=report.net)
        files = {rel: b64(data_) for rel, data_ in skill.files.items()}
        try:
            self._send({"op": "load", "name": skill.name, "hash": skill.hash, "path": skill.path, "source": skill.source,
                        "files": files, "read_roots": [str(r) for r in workspace.read_roots],
                        "write_root": str(workspace.write_root) if workspace.write_root is not None else None,
                        "may_write": bool(skill.writes and workspace.write_root is not None), "data_dir": data})
            loaded = self._serve("loaded")
        except SandboxStop as stop:
            self.report.ran = "sandboxed"
            raise loader.LoaderError(str(stop)) from None
        if loaded.get("ok") is not True:
            self.close()
            raise loader.LoaderError(_plain(loaded.get("message") or f"skill '{skill.name}' could not be imported", 300))

    def _fill_report(self, applied: Mapping[str, Any], plan: launch.Plan, caps: Capabilities) -> None:
        r = self.report
        r.separate_process, r.env_scrubbed, r.bwrap = True, True, plan.bwrap
        landlock = applied.get("landlock") if isinstance(applied.get("landlock"), dict) else None
        if landlock:
            r.landlock_abi = int(landlock.get("abi", 0))
            r.landlock_full = bool(landlock.get("full"))
            r.best_effort += [_plain(m, 200) for m in list(landlock.get("missing") or [])[:8]]
        elif plan.landlock is not None:
            r.best_effort.append("Landlock was planned but could not be applied in the child")
        r.seccomp = applied.get("seccomp") is True
        if plan.seccomp and not r.seccomp:
            r.best_effort.append("the seccomp filter was planned but could not be installed in the child")
        r.no_new_privs = applied.get("no_new_privs") is True
        r.caps_dropped = applied.get("caps_dropped") is True
        rl = applied.get("rlimits") if isinstance(applied.get("rlimits"), dict) else {}
        r.rlimits = {str(k)[:10]: int(v) for k, v in rl.items() if isinstance(v, int)}
        r.nproc_enforced = "NPROC" in r.rlimits and not caps.running_as_root
        r.best_effort += plan.notes
        if caps.running_as_root:
            r.best_effort.append("running as root: the kernel does not apply the process-count limit to root")
        if not plan.bwrap and not r.seccomp:
            r.best_effort.append("a process the skill starts in a new session is not ended with it (needs bubblewrap or seccomp)")
        if plan.bwrap or (r.landlock_abi and r.landlock_full):
            r.files = "isolated"
        elif r.landlock_abi or plan.macos_profile:
            r.files = "best effort"
        if plan.bwrap or r.seccomp:
            r.net = "blocked"
        elif landlock and landlock.get("net_tcp"):
            r.net = "tcp blocked"
            r.best_effort.append("only TCP is blocked; UDP and unix sockets are not")

    # ------------------------------------------------------------ the pipe

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self._last_tick < 1.0:
            return
        self._last_tick = now
        for path, cap in self._watch:
            if _tree_bytes(path) > cap:
                raise _DataCap()

    def _stop(self, code: str) -> SandboxStop:
        if self._dead is None:
            status = self._kill()
            if code == "crashed" and status is not None and -status == getattr(signal, "SIGXCPU", -1):
                code = "cpu"
            self._dead = SandboxStop(code)
            self.report.stopped = code
            try:
                self._emit("sandbox.stop", reason=code)
            except Exception:
                pass
        return self._dead

    def _send(self, frame: dict[str, Any]) -> None:
        if self._dead is not None or self._chan is None:
            raise self._dead or SandboxStop("crashed")
        try:
            self._chan.send(frame, self._deadline or time.monotonic() + START_TIMEOUT_S)
        except ChannelTimeout:
            raise self._stop("timeout") from None
        except ChannelClosed:
            raise self._stop("crashed") from None
        except _DataCap:
            raise self._stop("data-cap") from None

    def _serve(self, expect: str) -> dict[str, Any]:
        """Answer the child's requests until it sends `expect`. Any other outcome ends the run."""
        if self._dead is not None or self._chan is None:
            raise self._dead or SandboxStop("crashed")
        while True:
            try:
                self._tick()
                frame = self._chan.recv(self._deadline or time.monotonic() + START_TIMEOUT_S)
                op = frame["op"]
                if op == expect:
                    return frame
                if op == "event":
                    self._event(frame)
                elif op in ("ask", "embed", "remaining") and self._client is not None:
                    self._chan.send({"op": "reply", **self._request(op, frame)}, self._deadline)
                else:
                    raise ProtocolError("unexpected-frame")
            except ChannelTimeout:
                raise self._stop("timeout") from None
            except ChannelClosed:
                raise self._stop("crashed") from None
            except _DataCap:
                raise self._stop("data-cap") from None
            except ProtocolError:
                raise self._stop("protocol") from None

    def _event(self, frame: Mapping[str, Any]) -> None:
        self._events += 1
        kind, facts = frame.get("kind"), frame.get("facts")
        if self._events > self.policy.events or not isinstance(kind, str) or not KIND_OK.match(kind) or not isinstance(facts, dict):
            return  # dropped: over the cap, or not a `skill.*` progress fact
        clean = {str(k)[:40]: v for k, v in list(facts.items())[:40] if k != "options"}
        self._emit(kind, **clean)

    def _authorize(self, action: str, facts: Mapping[str, Any]) -> None:
        if self.policy.authorize is not None:
            self.policy.authorize(action, facts, self.token)

    def _request(self, op: str, frame: Mapping[str, Any]) -> dict[str, Any]:
        client = self._client
        try:
            if op == "remaining":
                return {"ok": client.remaining()}
            if op == "ask":
                messages = frame.get("messages")
                task, privacy, max_tokens = frame.get("task"), frame.get("privacy"), frame.get("max_tokens")
                schema, handoff = frame.get("json_schema"), frame.get("handoff")
                if (not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages)
                        or not (task is None or (isinstance(task, str) and len(task) <= 60))
                        or not (privacy is None or (isinstance(privacy, str) and len(privacy) <= 60))
                        or not (schema is None or isinstance(schema, dict)) or not isinstance(handoff, bool)):
                    raise ProtocolError("bad-request")
                self._authorize("llm.ask", {"task": task, "privacy": privacy, "messages": len(messages)})
                answer = client.ask(messages, task=task, privacy=privacy, max_tokens=max_tokens, json_schema=schema, handoff=handoff)
                return {"ok": dataclasses.asdict(answer)}
            texts = frame.get("texts")
            if not isinstance(texts, list):
                raise ProtocolError("bad-request")
            self._authorize("llm.embed", {"count": len(texts)})
            got = client.embed_with_facts(texts)
            return {"ok": {"vectors": got.vectors, "provider": got.provider, "deployment": got.deployment, "cost_usd": got.cost_usd}}
        except LlmError as exc:
            ident = len(self._errors) + 1
            self._errors[ident] = exc
            return {"err": {"id": ident, **self._describe(exc)}}
        except PermissionError as exc:
            return {"err": {"id": None, "kind": "Refused", "message": _plain(exc, 200) or "not allowed"}}
        except (ValueError, TypeError) as exc:
            return {"err": {"id": None, "kind": "ValueError", "message": _plain(exc, 300)}}

    @staticmethod
    def _describe(exc: LlmError) -> dict[str, Any]:
        if isinstance(exc, BudgetExceeded):
            return {"kind": "BudgetExceeded", "which": exc.which, "limit": exc.limit, "used": exc.used}
        if isinstance(exc, Deferred):
            o = exc.options
            return {"kind": "Deferred", "retry_after": exc.retry_after, "reason": exc.reason, "task": exc.task,
                    "sent_upstream": exc.sent_upstream,
                    "options": {"wait_at": o.wait_at, "handoff": o.handoff, "pay_usd": o.pay_usd, "offered": o.offered}}
        if isinstance(exc, PrivacyRefused):
            return {"kind": "PrivacyRefused", "privacy": exc.privacy, "task": exc.task}
        if isinstance(exc, RouterError):
            return {"kind": "RouterError", "status": exc.status, "text": exc.kind, "detail": exc.detail}
        return {"kind": "LlmError"}

    # ------------------------------------------------------------ what the runner calls

    def attempt(self, number: int, inputs: Mapping[str, Any], client: Any, workspace: Any, privacy: str,
                emit: Any) -> tuple[RemoteContext, Any, BaseException | None]:
        """Run the skill's entry in the child. Never raises: returns (context, output, exception) like the runner's own attempt."""
        assert self._skill is not None
        self._client, self._emit = client, emit
        ctx = RemoteContext(self._skill, inputs, client, privacy, number)
        try:
            self._send({"op": "attempt", "n": number, "inputs": json.loads(json.dumps(dict(inputs), default=str)),
                        "privacy": privacy, "default_task": client.default_task, "raise_steps": client.raise_steps,
                        "allow_paid": client.allow_paid})
            done = self._serve("attempt_done")
        except SandboxStop as stop:
            return ctx, None, stop
        finally:
            self._client = None
        try:
            return self._finish_attempt(ctx, done, workspace)
        except ProtocolError:
            return ctx, None, self._stop("protocol")

    def _finish_attempt(self, ctx: RemoteContext, done: Mapping[str, Any], workspace: Any) -> tuple[RemoteContext, Any, BaseException | None]:
        notes = done.get("notes")
        if not isinstance(notes, list) or done.get("n") != ctx.attempt:
            raise ProtocolError("bad-result")
        for note in notes[:500]:
            text = _plain(note, 2000)
            if isinstance(note, str) and text and text not in ctx.notes:
                ctx.notes.append(text)
        ctx.incomplete = done.get("incomplete") is True
        ctx.kept = done.get("kept")
        if isinstance(done.get("skipped_links"), int):
            workspace.skipped_links = max(workspace.skipped_links, min(done["skipped_links"], 10**9))
        proposed = done.get("proposed")
        if not isinstance(proposed, list) or len(proposed) > MAX_PROPOSED:
            raise ProtocolError("bad-result")
        if done.get("ok") is True:
            # Proposals are data. The parent's own Workspace decides whether each path is inside the output folder.
            try:
                for item in proposed:
                    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                        raise ProtocolError("bad-result")
                    self._authorize("files.propose", {"size": len(str(item.get("data", "")))})
                    workspace.propose_write(item["path"], unb64(item.get("data")), _plain(item.get("reason", ""), 300))
            except (NeedsViolation, ValueError):
                workspace.proposed.clear()
                return ctx, None, NeedsViolation("a file it proposed is outside the output folder")
            except PermissionError:
                workspace.proposed.clear()
                return ctx, None, self._stop("refused")
            return ctx, done.get("output"), None
        error = done.get("error")
        if not isinstance(error, dict):
            raise ProtocolError("bad-result")
        kind = error.get("kind")
        if kind == "llm":
            exc = self._errors.get(error.get("id")) if isinstance(error.get("id"), int) else None
            return ctx, None, exc if exc is not None else remote_error("LlmError")
        if kind == "stop":
            return ctx, None, runner.SkillStop(_plain(error.get("message", ""), 300))
        if kind == "needs":
            return ctx, None, NeedsViolation(_plain(error.get("message", ""), 300))
        return ctx, None, remote_error(error.get("type"), error.get("where"))

    def check(self, ctx: RemoteContext, output: Any) -> list[str]:
        """Run the skill's check in the child, on the child's own copy of the output. Returns the reasons (empty = passed)."""
        which = "kept" if ctx.kept is not None and output is ctx.kept else "output"
        self._client = ctx.llm
        try:
            self._send({"op": "check", "n": ctx.attempt, "which": which})
            done = self._serve("check_done")
        finally:
            self._client = None
        if "error" in done:
            raise remote_error(done.get("error"))
        reasons = done.get("reasons")
        if not isinstance(reasons, list):
            raise self._stop("protocol")
        return [_plain(r, 300) for r in reasons[:200]]

    # ------------------------------------------------------------ end

    def _kill(self) -> int | None:
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.wait(timeout=0.05)  # a child that just closed its pipe: let it report how it ended
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            for send in (lambda: os.killpg(proc.pid, signal.SIGKILL), proc.kill):
                try:
                    send()
                except (OSError, AttributeError):
                    pass
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        """End the child (asking first, then killing its whole process group) and remove the private temp folder."""
        if self._chan is not None and self._dead is None and self._proc is not None and self._proc.poll() is None:
            try:
                self._chan.send({"op": "exit"}, time.monotonic() + 1.0)
                self._proc.wait(timeout=2.0)
            except (ChannelClosed, ChannelTimeout, ProtocolError, subprocess.TimeoutExpired, _DataCap):
                pass
        if self._proc is not None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)  # anything the skill left running in its process group
            except (OSError, AttributeError):
                pass
            self._kill()
            self._proc = None
        if self._chan is not None:
            self._chan.close()
            self._chan = None
        if self._plan is not None and self._plan.host_tmp:
            shutil.rmtree(self._plan.host_tmp, ignore_errors=True)
            self._plan.host_tmp = ""


def run_skill(
    skill: Skill | str,
    inputs: Mapping[str, Any] | None = None,
    *,
    roots: Sequence[str | os.PathLike[str]] = (),
    budget: Mapping[str, Any] | None = None,
    privacy: str | None = None,
    token: str | None = None,
    policy: Policy | None = None,
    mode: str = "require",
    **runner_options: Any,
) -> tuple[runner.Result, Report]:
    """Run one skill in the sandbox and say honestly how isolated it was.

    roots     the folders the person names for this run (the runner's `folders`)
    token     an optional permission token; passed to `policy.authorize`, not checked here
    mode      `require` (default): refuse unless files AND network are isolated. `auto`: use what this machine has
              and report it. `off`: run in this process. A skill that does not ship with Muse always gets `require`.
    Other keywords are the runner's (`out_dir`, `task`, `environ`, `on_event`, `apply_writes`, `wait_up_to`, ...).

    Returns (Result, Report). `Result.sandbox` holds `Report.public()`.
    """
    env = runner_options.get("environ")
    env = os.environ if env is None else env
    mode = normalise_mode(mode)
    report = Report(mode=mode, platform=sys.platform)
    common = dict(budget=budget, privacy=privacy, folders=roots, _sandbox_decided=True, **runner_options)
    try:
        found = loader.find(skill, env) if isinstance(skill, str) else skill
    except loader.LoaderError as exc:
        result = runner.run(skill, inputs, **common)  # fails the same way the runner always does
        report.ran, report.reason = "refused", str(exc)
        result.sandbox = report.public()
        return result, report
    if found.source != "shipped" and not unsafe_home_skills():
        mode = report.mode = "require"
    reason = "the sandbox is switched off" if mode == "off" else ""
    if mode == "auto":
        reason = in_process_reason(found) or ("no isolation is available on this system yet" if sys.platform == "win32" else "")
    if reason:
        report.reason = reason
        result = runner.run(found, inputs, **common)
    else:
        executor = Executor(policy, mode, token)
        try:
            result = runner.run(found, inputs, _executor=executor, **common)
        finally:
            executor.close()
        report = executor.report
    result.sandbox = report.public()
    return result, report
