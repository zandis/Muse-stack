"""The only way a skill reaches a model: the local router's OpenAI-compatible endpoint.

`Client.ask` and `Client.embed` post with the runner's app token and the
`x-muse-task` / `x-muse-privacy` headers, so the router picks the allowance,
rations it and adds the provider key. A skill never holds a provider key.

Every call is counted against the job's budget BEFORE it is sent; a call that
would exceed it raises `BudgetExceeded` and nothing is sent. A 429 becomes a
typed `Deferred` carrying exactly what the router offered (`Retry-After`, the
`x-muse-ration` / `x-muse-wait` reason, the `options=` trailer); the client
never sleeps or retries by itself: the runner decides. A 403 privacy refusal
raises `PrivacyRefused` and is final: nothing here ever lowers a privacy class.

A 200 with no visible text is a FAILED call, not an empty result: `Answer.empty`
is True and `Answer.empty_reason` says `reasoning` (the router's
`x-muse-answer: empty-reasoning` header, a `length` stop, or reasoning tokens in
the usage) or `blank`. `ask_with_recovery` retries such a call once with a
larger output cap one task step up, and returns a typed `Unanswered` when that
is empty too. A skill must never read an empty answer as "nothing found".

No message, prompt or answer text is put in an exception message or an event.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from wardmuse import policy, rules
from wardmuse.ration import parse_options

CHAT_PATH = "/v1/chat/completions"
EMBED_PATH = "/v1/embeddings"
RAISE_ORDER: tuple[str, ...] = ("light", "everyday", "heavy")  # one step up on a failed check; heavy stays heavy
REQUEST_TIMEOUT_S = 120.0
DEFAULT_OUTPUT_TOKENS = 1024
MAX_EMBED_BATCH = 64
EMPTY_REASONING = "empty-reasoning"
EMPTY_REASON_REASONING = "reasoning"  # the output allowance went on hidden reasoning (router header, length stop, or usage)
EMPTY_REASON_BLANK = "blank"  # no visible text and no sign of why
RECOVERY_FACTOR = 4  # ask_with_recovery: the retry's output cap is this many times the first one ...
RECOVERY_MAX_TOKENS = 4096  # ... but never above this (the caller may pass a lower bound)


# ---------------------------------------------------------------- errors


class LlmError(Exception):
    """Base for everything the client raises."""


class BudgetExceeded(LlmError):
    """The job's budget does not allow this call. Nothing was sent."""

    def __init__(self, which: str, limit: float, used: float) -> None:
        self.which, self.limit, self.used = which, limit, used
        super().__init__(f"budget exceeded: {which} (limit {limit:g}, used {used:g})")


@dataclass(frozen=True, slots=True)
class Options:
    """What the router said is possible now. `offered` is False when its message had no `options=` trailer."""

    wait_at: str | None = None  # ISO-8601 UTC, the soonest reset
    handoff: bool = False
    pay_usd: float | None = None
    offered: bool = False

    def as_list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.wait_at:
            out.append({"choice": "wait", "until": self.wait_at})
        if self.handoff:
            out.append({"choice": "handoff"})
        if self.pay_usd is not None:
            out.append({"choice": "pay", "about_usd": self.pay_usd})
        return out


class Deferred(LlmError):
    """HTTP 429 from the router. `reason` is the x-muse-ration code or `provider-limit`; never content."""

    def __init__(self, retry_after: float | None, options: Options, *, reason: str = "", task: str = "",
                 sent_upstream: bool = False) -> None:
        self.retry_after, self.options, self.reason, self.task, self.sent_upstream = retry_after, options, reason, task, sent_upstream
        super().__init__(f"deferred by the router ({reason or 'no reason given'})")


class PrivacyRefused(LlmError):
    """HTTP 403: no service allowed for this privacy class could take the request. Final."""

    def __init__(self, privacy: str, task: str = "") -> None:
        self.privacy, self.task = privacy, task
        super().__init__(f"refused by privacy class '{privacy}'")


class RouterError(LlmError):
    """Anything else: the router is unreachable (status 0), rejected the token, or answered an error."""

    def __init__(self, status: int, kind: str, detail: str = "") -> None:
        self.status, self.kind, self.detail = status, kind, detail
        super().__init__(f"router error: {kind} (HTTP {status})" if status else f"router error: {kind}")


# ---------------------------------------------------------------- budget


@dataclass(frozen=True, slots=True)
class Budget:
    requests: int
    tokens: int
    seconds: float
    paid_usd: float = 0.0

    @classmethod
    def of(cls, base: Any, override: Mapping[str, Any] | None = None) -> "Budget":
        """A budget from a skill's `budget` (anything with those four attributes), with the caller's changes."""
        values = {k: getattr(base, k) for k in ("requests", "tokens", "seconds", "paid_usd")}
        for key, value in (override or {}).items():
            if key not in values:
                raise ValueError(f"unknown budget field {key!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"budget {key} must be a number, zero or more")
            values[key] = value
        return cls(int(values["requests"]), int(values["tokens"]), float(values["seconds"]), float(values["paid_usd"]))


def estimate_tokens(text: str) -> int:
    """Deliberately high, like the router's own estimate: one token per 3 ASCII characters, one per other character."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 3 + (len(text) - ascii_chars) + 1


@dataclass
class Meter:
    """What a job has used. `reserve` runs before a call is sent; `settle` after the answer."""

    budget: Budget
    clock: Callable[[], float] = time.time
    started: float = 0.0
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    reserved: int = 0
    cost_usd: float = 0.0
    deferred: int = 0
    cache_hits: int = 0
    empty_answers: int = 0  # 200s with no visible text
    by_provider: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.started = self.started or self.clock()

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def elapsed(self) -> float:
        return max(0.0, self.clock() - self.started)

    def remaining(self) -> dict[str, float]:
        b = self.budget
        return {"requests": max(0, b.requests - self.requests), "tokens": max(0, b.tokens - self.tokens - self.reserved),
                "seconds": max(0.0, b.seconds - self.elapsed()), "paid_usd": max(0.0, b.paid_usd - self.cost_usd)}

    def reserve(self, estimate: int) -> int:
        b = self.budget
        if self.requests + 1 > b.requests:
            raise BudgetExceeded("requests", b.requests, self.requests)
        if self.tokens + self.reserved + estimate > b.tokens:
            raise BudgetExceeded("tokens", b.tokens, self.tokens + self.reserved)
        if self.elapsed() >= b.seconds:
            raise BudgetExceeded("seconds", b.seconds, self.elapsed())
        if self.cost_usd > b.paid_usd + 1e-12:
            raise BudgetExceeded("paid_usd", b.paid_usd, self.cost_usd)
        self.requests += 1
        self.reserved += estimate
        return estimate

    def release(self, estimate: int) -> None:
        self.reserved = max(0, self.reserved - estimate)

    def settle(self, estimate: int, tokens_in: int, tokens_out: int, cost: float | None, provider: str) -> None:
        self.release(estimate)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost or 0.0
        row = self.by_provider.setdefault(provider or "unknown", {"requests": 0, "tokens": 0, "cost_usd": 0.0})
        row["requests"] += 1
        row["tokens"] += tokens_in + tokens_out
        row["cost_usd"] += cost or 0.0

    def used(self) -> dict[str, Any]:
        return {"requests": self.requests, "tokens": self.tokens, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "cost_usd": round(self.cost_usd, 6), "seconds": round(self.elapsed(), 3), "deferred": self.deferred,
                "cache_hits": self.cache_hits, "empty_answers": self.empty_answers,
                "providers": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in sorted(self.by_provider.items())}}


# ---------------------------------------------------------------- transport


@dataclass(slots=True)
class Reply:
    status: int  # 0 = the router could not be reached, or the call timed out
    headers: dict[str, str]
    body: bytes


def http_post(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> Reply:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
    except urllib.error.HTTPError as err:
        try:
            data = err.read()
        except OSError:
            data = b""
        return Reply(err.code, {k.lower(): v for k, v in err.headers.items()}, data)
    except (urllib.error.URLError, OSError, ValueError):
        return Reply(0, {}, b"")


# ---------------------------------------------------------------- answers


@dataclass(frozen=True, slots=True)
class Answer:
    """A model's answer plus the route facts the router put in its response headers."""

    text: str
    provider: str = ""
    cost_usd: float | None = None
    cache: str = ""  # hit | miss | ""
    min_output: str = ""  # x-muse-min-output: the output floor raised this call's cap
    answer: str = ""  # "empty-reasoning": a 200 with no visible text
    task: str = ""  # the task the router applied
    privacy: str = ""
    deployment: str = ""
    handoff: bool = False  # the router suggested the hand-off (x-muse-handoff: suggest)
    tokens_in: int = 0
    tokens_out: int = 0
    empty_reason: str = ""  # "" when there is text; else "reasoning" or "blank"
    finish_reason: str = ""  # the first choice's finish_reason, as the provider gave it
    reasoning_tokens: int = 0  # usage.completion_tokens_details.reasoning_tokens, when reported
    attempts: int = 1  # 2 when ask_with_recovery had to ask again
    reasoning: str = ""  # x-muse-reasoning: the reasoning effort the router set for this call
    flagged: bool = False  # the router itself said x-muse-answer: empty-reasoning (an older router never does)
    ms: int = 0  # how long the HTTP call took, in milliseconds (router-side retries and fallbacks included)

    @property
    def empty(self) -> bool:
        """True when there is no visible text. An empty answer is a failed call, never a valid empty result."""
        return not self.text.strip()

    @property
    def unanswered(self) -> bool:
        return False  # an Answer always came from a model; see Unanswered

    @property
    def recovered(self) -> bool:
        return self.attempts > 1 and not self.empty

    def facts(self) -> dict[str, Any]:
        """Everything but the text: safe for events."""
        return {"provider": self.provider, "cost_usd": self.cost_usd, "cache": self.cache, "min_output": self.min_output,
                "answer": self.answer, "task": self.task, "privacy": self.privacy, "deployment": self.deployment,
                "handoff": self.handoff, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                "empty": self.empty, "empty_reason": self.empty_reason, "finish_reason": self.finish_reason,
                "reasoning": self.reasoning, "flagged": self.flagged, "ms": self.ms}


@dataclass(frozen=True, slots=True)
class Unanswered:
    """What `ask_with_recovery` returns when the model gave no visible text, also after the one larger retry.

    It is not an answer: a caller must treat the step as NOT DONE (passages not read, question not answered),
    never as "the model found nothing". `stopped` says why no second answer exists when the retry was not sent
    or did not come back: "" (it was sent and was empty too), "budget", or "no-retry".
    """

    reason: str = EMPTY_REASON_BLANK  # reasoning | blank: why the last answer was empty
    attempts: int = 1
    stopped: str = ""
    task: str = ""
    provider: str = ""
    cost_usd: float | None = None
    handoff: bool = False

    @property
    def unanswered(self) -> bool:
        return True

    @property
    def text(self) -> str:
        return ""

    @property
    def empty(self) -> bool:
        return True

    @property
    def recovered(self) -> bool:
        return False

    @property
    def empty_reason(self) -> str:
        return self.reason

    @property
    def answer(self) -> str:
        return EMPTY_REASONING


@dataclass(frozen=True, slots=True)
class Embeddings:
    vectors: list[list[float]]
    provider: str = ""
    deployment: str = ""
    cost_usd: float | None = None


def raised(task: str, steps: int) -> str:
    """`task` moved `steps` up light -> everyday -> heavy. Other tasks (frontier, embed, named ones) are unchanged."""
    if steps <= 0 or task not in RAISE_ORDER:
        return task
    return RAISE_ORDER[min(len(RAISE_ORDER) - 1, RAISE_ORDER.index(task) + steps)]


def ask_with_recovery(client: Any, messages: Sequence[Mapping[str, Any]], *, task: str | None = None,
                      max_tokens: int | None = None, max_retry_tokens: int = RECOVERY_MAX_TOKENS,
                      factor: int = RECOVERY_FACTOR, **kwargs: Any) -> "Answer | Unanswered":
    """One chat call; when it comes back with no visible text, ONE retry with more room. Opt-in: `ask` is unchanged.

    The retry's output cap is `max_tokens * factor`, at most `max_retry_tokens`, and its task is one step up
    (light -> everyday -> heavy): the router clamps a `light` call to that task's ceiling again, so a larger
    cap alone would change nothing. Both calls are counted against the job's budget like any other call.

    Returns the `Answer` (with `attempts == 2` and `recovered` True when the retry is the one that has text),
    or an `Unanswered` when there is still no text: the caller must treat its step as not done. A deferral,
    a privacy refusal or a router error on either call is raised as from `ask`; a budget stop on the retry is
    not raised: the result is `Unanswered(stopped="budget")`, because the first call did happen.

    `client` is anything with `ask` (the runner's Client, or the sandbox's proxy for it).
    """
    first = client.ask(messages, task=task, max_tokens=max_tokens, **kwargs)
    if not first.empty:
        return first
    base = task or getattr(client, "default_task", "") or ""
    lost = dict(reason=first.empty_reason or EMPTY_REASON_BLANK, task=first.task, provider=first.provider,
                cost_usd=first.cost_usd, handoff=first.handoff)
    if max_tokens is None or factor <= 1:
        return Unanswered(attempts=1, stopped="no-retry", **lost)  # no cap was set, so there is no larger one to try
    larger = max(int(max_tokens), min(int(max_tokens) * int(factor), int(max_retry_tokens)))
    try:
        second = client.ask(messages, task=raised(base, 1) if base else None, max_tokens=larger, **kwargs)
    except BudgetExceeded:
        return Unanswered(attempts=1, stopped="budget", **lost)
    if second.empty:
        cost = (first.cost_usd or 0.0) + (second.cost_usd or 0.0)
        return Unanswered(reason=second.empty_reason or EMPTY_REASON_BLANK, attempts=2, task=second.task,
                          provider=second.provider, cost_usd=cost, handoff=second.handoff)
    return replace(second, attempts=2)


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _router_message(reply: Reply) -> str:
    try:
        payload = json.loads(reply.body or b"{}")
    except ValueError:
        return ""
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    return str(error.get("message", "") if isinstance(error, dict) else error)


def _own_words(message: str) -> str:
    """The router's own reason (after its `[muse]` mark), without the options trailer. Empty for provider text."""
    if "[muse]" not in message:
        return ""
    return message[message.index("[muse]"):].split(" | options=", 1)[0][:300]


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text_of(messages: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        parts.append(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str))
    return "\n".join(parts)


class Client:
    """One job's connection to the router. Built by the runner; a skill sees it as `ctx.llm`.

    The error types are attributes (`ctx.llm.Deferred`, `ctx.llm.Error`, ...) so a
    skill can catch them without importing anything from Muse.
    """

    Error = LlmError
    Deferred = Deferred
    PrivacyRefused = PrivacyRefused
    BudgetExceeded = BudgetExceeded
    RouterError = RouterError

    def __init__(
        self,
        base_url: str,
        token: str,
        meter: Meter,
        *,
        default_task: str = "everyday",
        privacy: str = "standard",
        raise_steps: int = 0,
        allow_paid: bool = False,
        timeout: float = REQUEST_TIMEOUT_S,
        on_event: Callable[..., None] | None = None,
        on_deferred: Callable[[Deferred], bool] | None = None,
        refresh_token: Callable[[], str] | None = None,
        post: Callable[[str, Mapping[str, str], bytes, float], Reply] = http_post,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self.meter = meter
        self.default_task = default_task
        self.privacy = policy.normalise_class(privacy)
        self.raise_steps = raise_steps
        self.allow_paid = allow_paid
        self.timeout = timeout
        self._emit = on_event or (lambda kind, **facts: None)
        self._on_deferred = on_deferred
        self._refresh = refresh_token
        self._post = post
        self.answers: list[dict[str, Any]] = []  # route facts of every 200, never text
        self.last_deferred: Deferred | None = None
        self._last_ms = 0  # the duration of the latest HTTP call, for the `call` event

    # ------------------------------------------------------------ public

    def remaining(self) -> dict[str, float]:
        """What is left of the job's budget (requests, tokens, seconds, paid_usd)."""
        return self.meter.remaining()

    def ask(self, messages: Sequence[Mapping[str, Any]], *, task: str | None = None, privacy: str | None = None,
            max_tokens: int | None = None, json_schema: Mapping[str, Any] | None = None, handoff: bool = False) -> Answer:
        """One chat call. `task` defaults to the job's tier; `privacy` may only be stricter than the job's class.

        `handoff=True` marks the step as one the person could do in their own subscription app instead: the
        request carries `x-muse-handoff: suggest`. Whether a hand-off is offered is still the router's decision.
        """
        task = raised(task or self.default_task, self.raise_steps)
        cls = self._class(privacy)
        body: dict[str, Any] = {"model": "auto", "messages": list(messages)}
        if max_tokens is not None:
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
                raise ValueError("max_tokens must be a whole number, 1 or more")
            body["max_tokens"] = max_tokens
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "schema": dict(json_schema), "strict": True}}
        estimate = estimate_tokens(_text_of(messages)) + (max_tokens or self._ceiling(task))
        reply = self._send(CHAT_PATH, body, task, cls, estimate, {"x-muse-handoff": "suggest"} if handoff else None)
        try:
            payload = json.loads(reply.body)
            message = payload["choices"][0]["message"]
            text = message.get("content")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.meter.release(estimate)
            self._emit("call", task=task, status=reply.status, ok=False, error="not-a-completion")
            raise RouterError(reply.status, "the answer was not a chat completion") from None
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        text = text if isinstance(text, str) else ""
        h = reply.headers
        flagged = h.get("x-muse-answer", "").strip().lower()
        choice = payload["choices"][0]
        finish = str(choice.get("finish_reason") or "") if isinstance(choice, dict) else ""
        details = usage.get("completion_tokens_details")
        reasoning_tokens = _int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
        empty_reason = ""
        if not text.strip():
            by_reasoning = flagged == EMPTY_REASONING or finish == "length" or reasoning_tokens > 0
            empty_reason = EMPTY_REASON_REASONING if by_reasoning else EMPTY_REASON_BLANK
        answer = Answer(
            text=text, provider=h.get("x-muse-provider", ""), cost_usd=_float(h.get("x-muse-cost-usd")),
            cache=h.get("x-muse-cache", ""), min_output=h.get("x-muse-min-output", ""),
            answer=flagged or (EMPTY_REASONING if not text.strip() else ""),
            task=h.get("x-muse-task", "") or task, privacy=h.get("x-muse-privacy", "") or cls,
            deployment=h.get("x-muse-deployment", ""), handoff=h.get("x-muse-handoff", "").strip().lower() == "suggest",
            tokens_in=_int(usage.get("prompt_tokens")), tokens_out=_int(usage.get("completion_tokens")),
            empty_reason=empty_reason, finish_reason=finish[:40], reasoning_tokens=reasoning_tokens,
            reasoning=h.get("x-muse-reasoning", "").strip().lower()[:20],
            flagged=flagged == EMPTY_REASONING, ms=self._last_ms,
        )
        if answer.empty:
            self.meter.empty_answers += 1
        self._settle(estimate, answer.tokens_in, answer.tokens_out, answer.cost_usd, answer.provider, answer.cache)
        self.answers.append(answer.facts())
        self._emit("call", status=200, ok=True, **answer.facts())
        return answer

    def ask_with_recovery(self, messages: Sequence[Mapping[str, Any]], **kwargs: Any) -> "Answer | Unanswered":
        """`ask`, with one larger retry when the answer has no visible text. See the module's `ask_with_recovery`."""
        return ask_with_recovery(self, messages, **kwargs)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Vectors for `texts` through the `embed` alias, in order."""
        return self.embed_with_facts(texts).vectors

    def embed_with_facts(self, texts: Sequence[str]) -> Embeddings:
        texts = list(texts)
        if not texts or not all(isinstance(t, str) and t for t in texts):
            raise ValueError("embed needs a non-empty list of non-empty strings")
        if len(texts) > MAX_EMBED_BATCH:
            raise ValueError(f"embed takes at most {MAX_EMBED_BATCH} texts a call")
        estimate = sum(estimate_tokens(t) for t in texts)
        reply = self._send(EMBED_PATH, {"model": "embed", "input": texts}, "embed", self.privacy, estimate)
        try:
            payload = json.loads(reply.body)
            rows = sorted(payload["data"], key=lambda row: row.get("index", 0))
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
            if len(vectors) != len(texts) or not all(vectors) or len({len(v) for v in vectors}) != 1:
                raise ValueError("wrong shape")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.meter.release(estimate)
            self._emit("call", task="embed", status=reply.status, ok=False, error="not-embeddings")
            raise RouterError(reply.status, "the answer was not a list of embeddings") from None
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        h = reply.headers
        provider, cost = h.get("x-muse-provider", ""), _float(h.get("x-muse-cost-usd"))
        used = _int(usage.get("prompt_tokens")) or _int(usage.get("total_tokens"))
        self._settle(estimate, used, 0, cost, provider, h.get("x-muse-cache", ""))
        self._emit("call", status=200, ok=True, task="embed", provider=provider, cost_usd=cost, tokens_in=used, count=len(texts),
                   ms=self._last_ms)
        return Embeddings(vectors, provider, h.get("x-muse-deployment", ""), cost)

    # ------------------------------------------------------------ internals

    def _class(self, privacy: str | None) -> str:
        if privacy is None:
            return self.privacy
        return policy.stricter(self.privacy, policy.normalise_class(privacy))  # a step may tighten, never loosen

    @staticmethod
    def _ceiling(task: str) -> int:
        """The most output the router's rules allow this task; only used to reserve budget."""
        try:
            value = rules.load_rules().tasks[task].max_output_tokens
        except Exception:
            value = None
        return int(value) if value else DEFAULT_OUTPUT_TOKENS

    def _settle(self, estimate: int, tin: int, tout: int, cost: float | None, provider: str, cache: str) -> None:
        if not tin and not tout:
            tin = estimate  # the router reported no usage: keep the high estimate rather than count nothing
        self.meter.settle(estimate, tin, tout, cost, provider)
        if cache == "hit":
            self.meter.cache_hits += 1

    def _send(self, path: str, body: Mapping[str, Any], task: str, cls: str, estimate: int,
              extra: Mapping[str, str] | None = None) -> Reply:
        """Count, send, and turn every non-200 into a typed error. Returns only a 200."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        refreshed = False
        while True:
            self.meter.reserve(estimate)  # raises BudgetExceeded: nothing is sent
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._token}",
                       "x-muse-task": task, "x-muse-privacy": cls, **(extra or {})}
            if self.allow_paid:
                headers["x-muse-allow-paid"] = "true"
            timeout = max(1.0, min(self.timeout, self.meter.remaining()["seconds"]))
            began = time.monotonic()
            reply = self._post(self._base + path, headers, data, timeout)
            self._last_ms = int((time.monotonic() - began) * 1000)
            if reply.status == 200:
                return reply
            self.meter.release(estimate)
            if reply.status == 401 and self._refresh is not None and not refreshed:
                refreshed = True  # the runner's own app token was rotated under it: repair once
                self._token = self._refresh()
                continue
            error = self._error(reply, task, cls)
            if isinstance(error, Deferred):
                self.meter.deferred += 1
                self.last_deferred = error
                self._emit("deferred", task=task, reason=error.reason, retry_after=error.retry_after,
                           options=error.options.as_list(), sent_upstream=error.sent_upstream)
                if self._on_deferred is not None and self._on_deferred(error):
                    continue  # the runner chose to wait and try again
            else:
                self._emit("call", task=task, status=reply.status, ok=False, error=type(error).__name__, ms=self._last_ms)
            raise error

    def _error(self, reply: Reply, task: str, cls: str) -> LlmError:
        if reply.status == 0:
            return RouterError(0, "the router could not be reached")
        message = _router_message(reply)
        h = reply.headers
        if reply.status == 429:
            ration = h.get("x-muse-ration", "").strip().lower()
            wait = h.get("x-muse-wait", "").strip().lower()
            parsed = _parse_options(message)
            return Deferred(_retry_after(h.get("retry-after")), parsed, reason=ration or wait, task=task,
                            sent_upstream=not ration)
        if reply.status == 403:
            low = message.lower()
            if "privacy" in low or "may receive" in low or f"'{cls}'" in low:
                return PrivacyRefused(cls, task)
            return RouterError(403, "the router refused this request", _own_words(message))
        if reply.status == 401:
            return RouterError(401, "the router rejected the skills app token", _own_words(message))
        return RouterError(reply.status, "the router answered an error", _own_words(message))


def _retry_after(value: str | None) -> float | None:
    number = _float(value)
    return max(0.0, number) if number is not None else None


def _parse_options(message: str) -> Options:
    if " | options=" not in message:
        return Options()
    try:
        raw = parse_options(message)
    except ValueError:
        return Options()
    pay = raw.get("pay")
    return Options(wait_at=str(raw["wait"]) if raw.get("wait") else None, handoff=bool(raw.get("handoff")),
                   pay_usd=float(pay) if isinstance(pay, (int, float)) and math.isfinite(pay) else None, offered=True)
