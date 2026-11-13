"""The sandboxed child: lock this process down, then run one skill's entry and check for the parent.

Started as `python -I -B -c <boot> <repo root> <read fd> <write fd>` by
muse.sandbox.host (possibly inside bubblewrap). Order matters:

  1. close every descriptor except stdio and the control pipe
  2. import what the child needs from Muse and the standard library
  3. lock down: resource limits, no new privileges, drop capabilities,
     Landlock, seccomp (muse.sandbox.lockdown), and report what was applied
  4. only then receive the skill's files and import them: no skill code,
     not even a module's top level, runs before step 3

The skill is imported from the bytes the parent hashed (sent over the pipe),
never from disk. It gets the same `Context` as in the in-process runner; the
only difference is `ctx.llm`, a proxy that asks the parent to make the call.
The child holds no token and has no route to the router or to anything else.

Nothing a skill raises is sent to the parent as text except the two kinds the
runner already shows people (`ctx.stop(...)`, a needs violation). Every other
failure travels as a type name and `file.py:line`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from muse.sandbox import lockdown
from muse.sandbox.protocol import BlockingChannel, ChannelClosed, ProtocolError, b64, unb64

PARENT_FRAME_MAX = 512 * 1024 * 1024
MAX_NOTE = 2000
NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,79}$")
WHERE_OK = re.compile(r"^[\w.\-]{1,80}:\d{1,7}$")


def _plain(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _where(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    return where if WHERE_OK.match(where) else ""


def _type_name(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if NAME_OK.match(name) else "Exception"


# ---------------------------------------------------------------- lock down


def lock_down(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Apply what the parent planned. Returns what REALLY holds now; the parent builds its report from this."""
    applied: dict[str, Any] = {"platform": sys.platform, "rlimits": {}, "no_new_privs": False, "caps_dropped": False,
                               "landlock": None, "landlock_error": "", "seccomp": False, "pdeathsig": False}
    if not sys.platform.startswith("linux"):
        try:
            applied["rlimits"] = lockdown.set_rlimits(dict(spec.get("rlimits") or {}))
        except Exception:
            pass
        return applied
    applied["pdeathsig"] = lockdown.die_with_parent()
    lockdown.not_dumpable()
    applied["rlimits"] = lockdown.set_rlimits(dict(spec.get("rlimits") or {}))
    applied["no_new_privs"] = lockdown.no_new_privs()
    applied["caps_dropped"] = lockdown.drop_capabilities()
    plan = spec.get("landlock")
    if plan and applied["no_new_privs"]:
        try:
            applied["landlock"] = lockdown.apply_landlock(plan.get("read", []), plan.get("write", []), plan.get("devices", []))
        except OSError as exc:
            applied["landlock_error"] = type(exc).__name__
    if spec.get("seccomp") and applied["no_new_privs"]:
        applied["seccomp"] = lockdown.apply_seccomp()
    return applied


# ---------------------------------------------------------------- ctx.llm in the child


class LlmProxy:
    """`ctx.llm` inside the sandbox: the same calls and error types, made by the parent over the control pipe."""

    def __init__(self, call: Callable[..., Any], settings: Mapping[str, Any]) -> None:
        from muse.skills import llm

        self._llm = llm
        self._call = call
        self.Error, self.Deferred, self.PrivacyRefused = llm.LlmError, llm.Deferred, llm.PrivacyRefused
        self.BudgetExceeded, self.RouterError = llm.BudgetExceeded, llm.RouterError
        self.default_task = str(settings.get("default_task", ""))
        self.privacy = str(settings.get("privacy", ""))
        self.raise_steps = int(settings.get("raise_steps", 0))
        self.allow_paid = bool(settings.get("allow_paid", False))
        self.answers: list[dict[str, Any]] = []
        self.last_deferred: Any = None

    def _rpc(self, op: str, **payload: Any) -> Any:
        reply = self._call(op, payload)
        err = reply.get("err")
        if err is None:
            return reply.get("ok")
        raise self._error(err)

    def _error(self, err: Mapping[str, Any]) -> BaseException:
        llm, kind = self._llm, err.get("kind")
        if kind == "BudgetExceeded":
            exc: BaseException = llm.BudgetExceeded(str(err.get("which", "")), float(err.get("limit", 0)), float(err.get("used", 0)))
        elif kind == "Deferred":
            o = err.get("options") or {}
            options = llm.Options(wait_at=o.get("wait_at"), handoff=bool(o.get("handoff")), pay_usd=o.get("pay_usd"),
                                  offered=bool(o.get("offered")))
            exc = llm.Deferred(err.get("retry_after"), options, reason=str(err.get("reason", "")), task=str(err.get("task", "")),
                               sent_upstream=bool(err.get("sent_upstream")))
            self.last_deferred = exc
        elif kind == "PrivacyRefused":
            exc = llm.PrivacyRefused(str(err.get("privacy", "")), str(err.get("task", "")))
        elif kind == "RouterError":
            exc = llm.RouterError(int(err.get("status", 0)), str(err.get("text", "")), str(err.get("detail", "")))
        elif kind == "ValueError":
            exc = ValueError(str(err.get("message", "")))
        elif kind == "Refused":
            exc = PermissionError(str(err.get("message", "")))
        else:
            exc = llm.LlmError("the call could not be made")
        exc.sandbox_id = err.get("id")  # type: ignore[attr-defined]  # lets the parent find its own copy of this error
        return exc

    def remaining(self) -> dict[str, float]:
        return dict(self._rpc("remaining"))

    def ask(self, messages: Sequence[Mapping[str, Any]], *, task: str | None = None, privacy: str | None = None,
            max_tokens: int | None = None, json_schema: Mapping[str, Any] | None = None, handoff: bool = False) -> Any:
        got = self._rpc("ask", messages=list(messages), task=task, privacy=privacy, max_tokens=max_tokens,
                        json_schema=dict(json_schema) if json_schema is not None else None, handoff=bool(handoff))
        answer = self._llm.Answer(**got)
        self.answers.append(answer.facts())
        return answer

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_with_facts(texts).vectors

    def embed_with_facts(self, texts: Sequence[str]) -> Any:
        got = self._rpc("embed", texts=list(texts))
        return self._llm.Embeddings(got["vectors"], got.get("provider", ""), got.get("deployment", ""), got.get("cost_usd"))


# ---------------------------------------------------------------- the conversation


class Session:
    def __init__(self, chan: BlockingChannel) -> None:
        self.chan = chan
        self.skill: Any = None
        self.entry: Callable[..., Any] | None = None
        self.check: Callable[..., Any] | None = None
        self.workspace: Any = None
        self.data_dir: Path | None = None
        self.attempts: dict[int, tuple[Any, Any]] = {}  # attempt number -> (ctx, output)

    def call(self, op: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.chan.send({"op": op, **payload})
        reply = self.chan.recv()
        if reply.get("op") != "reply":
            raise ProtocolError("unexpected-frame")
        return reply

    def emit(self, kind: str, /, **facts: Any) -> None:
        safe: dict[str, Any] = {}
        for key, value in facts.items():  # the same rule as the runner's events: numbers, booleans, short codes
            if value is None or isinstance(value, (bool, int, float)):
                safe[str(key)[:40]] = value
            elif isinstance(value, str):
                safe[str(key)[:40]] = value if len(value) <= 80 else "(long text removed)"
            elif isinstance(value, (list, tuple)) and len(value) <= 64 and all(isinstance(x, (bool, int, float)) for x in value):
                safe[str(key)[:40]] = list(value)
        self.chan.send({"op": "event", "kind": str(kind)[:60], "facts": safe})

    def load(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        from muse.skills import loader
        from muse.skills.format import FormatError, parse_skill_md
        from muse.skills.workspace import Workspace

        files = {str(rel): unb64(data) for rel, data in dict(frame["files"]).items()}
        try:
            skill = parse_skill_md(files[loader.SKILL_FILE].decode("utf-8"), folder_name=str(frame["name"]))
        except (KeyError, UnicodeDecodeError, FormatError):
            return {"op": "loaded", "ok": False, "message": "the skill's files did not arrive whole"}
        digest = loader.content_hash(files)
        if digest != frame.get("hash"):
            return {"op": "loaded", "ok": False, "message": "the skill's files did not arrive whole"}
        self.skill = replace(skill, path=str(frame.get("path", "")), source=str(frame.get("source", "")), hash=digest,
                             state="ready", files=files)
        try:
            self.entry, self.check = loader.entry_points(self.skill)
        except loader.LoaderError as exc:
            return {"op": "loaded", "ok": False, "message": _plain(exc, 300)}
        except BaseException as exc:  # SystemExit and friends at import time
            return {"op": "loaded", "ok": False, "message": f"skill '{skill.name}' could not be imported ({_type_name(exc)})"}
        self.workspace = Workspace(list(frame.get("read_roots") or []), frame.get("write_root"), may_write=bool(frame.get("may_write")))
        self.data_dir = Path(frame["data_dir"]) if frame.get("data_dir") else None
        return {"op": "loaded", "ok": True}

    def attempt(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        from muse.skills.llm import LlmError
        from muse.skills.runner import Context, SkillStop
        from muse.skills.workspace import NeedsViolation

        number = int(frame["n"])
        llm = LlmProxy(self.call, frame)
        self.workspace.proposed.clear()
        ctx = Context(self.skill, dict(frame.get("inputs") or {}), llm, self.workspace, self.data_dir, str(frame.get("privacy", "")),
                      number, self.emit)
        out: dict[str, Any] = {"op": "attempt_done", "n": number, "ok": True}
        output: Any = None
        try:
            output = self.entry(ctx)  # type: ignore[misc]
            out["output"] = json.loads(json.dumps(output, ensure_ascii=False, default=str))
        except BaseException as exc:
            out["ok"] = False
            output = None
            out.pop("output", None)
            if isinstance(exc, LlmError) and getattr(exc, "sandbox_id", None) is not None:
                out["error"] = {"kind": "llm", "id": exc.sandbox_id}  # type: ignore[attr-defined]
            elif isinstance(exc, SkillStop):
                out["error"] = {"kind": "stop", "message": _plain(exc, 300)}
            elif isinstance(exc, NeedsViolation):
                out["error"] = {"kind": "needs", "message": _plain(exc, 300)}
            else:
                out["error"] = {"kind": "error", "type": _type_name(exc), "where": _where(exc)}
        self.attempts[number] = (ctx, output)
        out["notes"] = [_plain(n, MAX_NOTE) for n in ctx.notes[:500]]
        out["incomplete"] = bool(ctx.incomplete)
        if ctx.kept is not None:
            try:
                out["kept"] = json.loads(json.dumps(ctx.kept, ensure_ascii=False, default=str))
            except (TypeError, ValueError, RecursionError):
                ctx.kept = None
        out["proposed"] = [{"path": w.path, "data": b64(w.data), "reason": w.reason} for w in self.workspace.proposed]
        out["skipped_links"] = int(self.workspace.skipped_links)
        out["answers"] = len(llm.answers)
        return out

    def run_check(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        from muse.skills.runner import _reasons

        ctx, output = self.attempts.get(int(frame["n"]), (None, None))
        if ctx is None:
            return {"op": "check_done", "error": "Exception"}
        subject = ctx.kept if frame.get("which") == "kept" else output
        try:
            return {"op": "check_done", "reasons": [_plain(r, 300) for r in _reasons(self.check(subject, ctx))][:200]}  # type: ignore[misc]
        except BaseException as exc:
            return {"op": "check_done", "error": _type_name(exc)}

    def serve(self) -> None:
        while True:
            frame = self.chan.recv()
            op = frame["op"]
            if op == "load":
                self.chan.send(self.load(frame))
            elif op == "attempt":
                self.chan.send(self.attempt(frame))
            elif op == "check":
                self.chan.send(self.run_check(frame))
            elif op == "exit":
                return
            else:
                raise ProtocolError("unexpected-frame")


def selftest(kind: str) -> int:
    """Used by muse.sandbox.detect in a throw-away process: 0 = the layer really works here."""
    if kind == "landlock":
        if not lockdown.no_new_privs():
            return 4
        lockdown.apply_landlock([], [], [])  # allow nothing
        try:
            os.open(sys.executable, os.O_RDONLY)
        except PermissionError:
            return 0
        return 3
    if kind == "seccomp":
        if not lockdown.no_new_privs():
            return 4
        return 0 if lockdown.apply_seccomp() else 3
    return 0  # "start": the interpreter came up inside the wrapper


def main(argv: Sequence[str]) -> None:
    if argv and argv[0] == "--selftest":
        code = 5
        try:
            code = selftest(argv[1])
        except BaseException:
            code = 5
        os._exit(code)
    read_fd, write_fd = int(argv[0]), int(argv[1])
    lockdown.close_other_fds({read_fd, write_fd})
    chan = BlockingChannel(read_fd, write_fd, PARENT_FRAME_MAX)
    code = 0
    try:
        init = chan.recv()
        if init.get("op") != "init":
            raise ProtocolError("unexpected-frame")
        import sqlite3  # noqa: F401  (shared libraries a skill is likely to need: resolve them before the lock-down)

        import muse.skills.runner  # noqa: F401
        try:
            os.chdir(str(init.get("tmp") or "/"))
        except OSError:
            pass
        applied = lock_down(init)
        chan.send({"op": "ready", "applied": applied, "pid": os.getpid()})
        Session(chan).serve()
    except (ChannelClosed, ProtocolError):
        code = 70
    except BaseException:
        code = 71
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)
