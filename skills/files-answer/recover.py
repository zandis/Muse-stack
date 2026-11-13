"""An answer with no visible text is a failed call: ask once more with more room, else say it was not answered.

Standard library only, and no import from Muse: the skills run sandboxed and get `ctx.llm` as a proxy. This is
the skill-side twin of `muse.skills.llm.ask_with_recovery` (same rule, same numbers), working on anything with
`ask(messages, task=..., max_tokens=...)` and `remaining()`.

A reasoning model spends its output allowance on hidden reasoning first; when the allowance is small it comes
back with HTTP 200 and nothing to read. Reading that as "the passages say nothing" is the failure this module
exists to prevent. `ask` retries such an answer ONCE, with the output cap times FACTOR (at most MOST) and one task
step up (light -> everyday -> heavy): the router clamps a `light` call to that task's ceiling again, so a larger
cap alone would change nothing. Both calls are counted against the job's budget like any other call. When the
retry is empty too, or there is no budget for it, `ask` returns an `Unanswered`: the caller must treat its step
as NOT DONE, never as an empty result.

This file is a byte-for-byte copy in every skill that uses it; a test fails when the copies drift.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Sequence

FACTOR = 4
MOST = 4096
ORDER = ("light", "everyday", "heavy")
REASONING = "reasoning"  # the output allowance went on hidden reasoning (router header, length stop, reasoning tokens)
BLANK = "blank"  # no visible text and no sign of why


@dataclasses.dataclass(frozen=True)
class Unanswered:
    """No visible text, also after the one larger retry (or the retry could not be sent: `stopped`)."""

    reason: str = BLANK
    attempts: int = 1
    stopped: str = ""  # "" (the retry was sent and was empty too) | "budget" | "no-cap"
    task: str = ""
    provider: str = ""
    handoff: bool = False
    cost_usd: float = 0.0

    text = ""
    empty = True
    unanswered = True
    recovered = False


def empty_reason(answer: Any) -> str:
    """"" when the answer has visible text; else REASONING or BLANK."""
    if str(getattr(answer, "text", "") or "").strip():
        return ""
    given = str(getattr(answer, "empty_reason", "") or "")
    if given:
        return given
    if str(getattr(answer, "answer", "") or "") == "empty-reasoning" or getattr(answer, "finish_reason", "") == "length":
        return REASONING
    return BLANK


def up(task: str) -> str:
    return ORDER[min(len(ORDER) - 1, ORDER.index(task) + 1)] if task in ORDER else task


def ask(llm: Any, messages: Sequence[Any], *, task: str | None = None, max_tokens: int, keep_requests: int = 0,
        factor: int = FACTOR, most: int = MOST, **kwargs: Any) -> Any:
    """The answer (its `attempts` is 2 when the retry gave the text), or an `Unanswered`.

    `task` None means the job's own tier (`llm.default_task`); the retry is one step above it.
    `keep_requests`: requests the caller keeps back for a later step; the retry is not sent when it would eat
    into them (the result is then `Unanswered(stopped="budget")`). A deferral, a privacy refusal or a router
    error on either call is raised as from `llm.ask`; a budget stop on the retry is not, because the first call
    did happen.
    """
    first = llm.ask(messages, task=task, max_tokens=max_tokens, **kwargs)
    why = empty_reason(first)
    if not why:
        return first
    base = task or str(getattr(llm, "default_task", "") or "")
    lost = {"reason": why, "task": str(getattr(first, "task", "") or base), "provider": str(getattr(first, "provider", "") or ""),
            "handoff": bool(getattr(first, "handoff", False)), "cost_usd": float(getattr(first, "cost_usd", 0.0) or 0.0)}
    if factor <= 1 or not max_tokens:
        return Unanswered(attempts=1, stopped="no-cap", **lost)
    try:
        left = llm.remaining()
        if float(left.get("requests", 0)) <= keep_requests:
            return Unanswered(attempts=1, stopped="budget", **lost)
    except (AttributeError, TypeError, ValueError):
        pass
    larger = max(int(max_tokens), min(int(max_tokens) * int(factor), int(most)))
    try:
        second = llm.ask(messages, task=up(base) if base else None, max_tokens=larger, **kwargs)
    except getattr(llm, "BudgetExceeded", ()):
        return Unanswered(attempts=1, stopped="budget", **lost)
    why = empty_reason(second)
    if why:
        return Unanswered(reason=why, attempts=2, task=str(getattr(second, "task", "") or up(base)),
                          provider=str(getattr(second, "provider", "") or ""), handoff=bool(getattr(second, "handoff", False)),
                          cost_usd=lost["cost_usd"] + float(getattr(second, "cost_usd", 0.0) or 0.0))
    if dataclasses.is_dataclass(second) and any(f.name == "attempts" for f in dataclasses.fields(second)):
        return dataclasses.replace(second, attempts=2)
    return second
