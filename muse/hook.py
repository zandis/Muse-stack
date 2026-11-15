"""LiteLLM callback that shares quota blocks across aliases and enforces privacy.

Glue only: rules live in muse.quota (what a failure means), muse.ledger (shared
state) and muse.policy (which deployments a privacy class may use).

Loaded by the proxy through `litellm_settings.callbacks: ["muse.hook.handler"]`
(LiteLLM 1.108.6 `proxy/types_utils/utils.py:get_instance_fn` imports
`muse.hook` and reads `handler`; PYTHONPATH must contain the repo root, which
is /opt/muse in the container).

Hooks used, with the 1.108.6 signatures (litellm/integrations/custom_logger.py):
- async_pre_call_hook: validate x-muse-privacy (unknown -> 400), resolve the class
  (the app's default_privacy replaces MUSE_DEFAULT_PRIVACY when the header is absent).
  Then the routing rules (muse/rules.py, rule 2): a request with `x-muse-task`, or
  for the model `auto`, has `data["model"]` REPLACED by the task's alias here. The
  1.108.6 proxy (proxy/common_request_processing.py) runs this hook after its own
  alias maps and before route_request, which reads data["model"], so the router
  only ever sees a real alias; `auto` needs no entry in the rendered config. The
  task's max_output_tokens is applied as a ceiling; an unknown task is a 400.
- async_filter_deployments: apply the privacy class, drop blocked upstreams, then
  ration (muse/ration.py). Runs for every model group the router tries,
  fallbacks and order levels included, so a fallback can never reach something
  rationing refuses. It sees every order level (LiteLLM's order filter runs
  after it), so the "spend what expires soonest" hint (ration.prefer) only
  narrows the level LiteLLM is about to pick from, only when decisive, and
  never to nothing; other levels, retries and fallbacks are untouched.
- async_pre_call_deployment_hook: second privacy check on the chosen deployment
  (a request that names a deployment id skips async_filter_deployments), rationing
  for such direct requests, the output floor, and the atomic paid-cap hold
  (Ledger.try_hold_paid). The floor: a reasoning model spends output tokens on
  hidden reasoning first, so a small max_tokens returns no visible text. A chat
  call whose cap is below the chosen deployment's model_info.muse_min_output_tokens
  is raised to it for that one upstream call (never lowered, never invented when
  the client set no cap). LiteLLM hands this hook a fresh copy of the request for
  every attempt, so a fallback to a deployment without a floor sends the client's
  own value; the paid hold below it is estimated on the raised figure.
  Next to the floor, the reasoning setting: a deployment whose
  model_info.muse_reasoning names a parameter and a value for the request's
  task (e.g. reasoning_effort: low for light) gets that parameter on this one
  upstream call when the client did not set it (catalogue `reasoning`).
- async_post_call_failure_deployment_hook: one call per failed attempt (retries
  and fallbacks too), unlike async_log_failure_event which fires once per request.
- async_log_success_event: record the call (app, priority, cost, paid) and limits.
- async_post_call_response_headers_hook: x-muse-provider / -deployment / -privacy /
  -app, x-muse-cost-usd, x-muse-ration and Retry-After on a rationing refusal,
  x-muse-task (the task applied), x-muse-handoff: suggest (rule 6), x-muse-cache,
  x-muse-min-output (the output floor below raised this call's cap),
  x-muse-reasoning (the reasoning setting this call was sent with) and
  x-muse-answer: empty-reasoning (a 200 whose visible answer is empty because
  the model spent its output allowance on reasoning; the body is not changed).
  A 429 whose last failure was a provider limit (the provider answered 429 and
  that set or met a quota block, or every allowed deployment was already
  blocked) carries `x-muse-wait: provider-limit` and Retry-After = seconds until
  the soonest such block ends (at least 1). It is a separate header, not an
  x-muse-ration value, because x-muse-ration means "Muse turned this away and
  nothing was sent upstream"; clients tell a provider's 429 by its absence.

Request headers read: x-muse-privacy, x-muse-task, x-muse-allow-oneoff: true (the
user approved spending one-off trial credits), x-muse-allow-paid: true (the user
chose "pay" for a frontier task). A rationing refusal's message ends with
` | options=...` (ration.options_trailer): the choices available right now.

The application comes from muse/auth.py through `metadata.user_api_key_metadata`
(the proxy strips caller-supplied `user_api_key_*` keys, so it cannot be forged
from a request body). Every stage carries `litellm_call_id`; per-request
rationing notes are kept in memory under it for the response headers.

`MUSE_PROVIDER_MODE=record|replay` (muse/cassette.py) sends the provider call
to a local proxy instead of the deployment's own base URL. `live` (the
default, and any other value) leaves the base URL alone. Replay that cannot
start raises, so the call is not sent on to a real provider.

Failure policy: any error in quota bookkeeping or rationing is caught, counted
in `errors` and ignored (LiteLLM's default routing continues), except that a
failed rationing step still removes paid deployments: when in doubt, do not
spend. Privacy enforcement fails closed.
"""

from __future__ import annotations

import sys
import asyncio
import logging
import math
import os
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

from fastapi import HTTPException
from litellm.exceptions import GuardrailRaisedException, RateLimitError
from litellm.integrations.custom_logger import CustomLogger

from muse import apps as apps_mod
from muse import cache as cache_mod
from muse import cost as cost_mod
from muse import quota
from muse import ration
from muse import rules as rules_mod
from muse.ledger import Ledger

log = logging.getLogger("muse.hook")

PRIVACY_HEADER = "x-muse-privacy"
TASK_HEADER = "x-muse-task"
ONEOFF_HEADER = "x-muse-allow-oneoff"
ALLOW_PAID_HEADER = "x-muse-allow-paid"
WAIT_HEADER = "x-muse-wait"  # response: why a 429 that is not a rationing refusal says Retry-After
WAIT_PROVIDER_LIMIT = "provider-limit"
MIN_OUTPUT_HEADER = "x-muse-min-output"  # response: raised:<from>-><to>[;over-ceiling:<task ceiling>]
ANSWER_HEADER = "x-muse-answer"  # response: empty-reasoning
ANSWER_EMPTY_REASONING = "empty-reasoning"
MIN_OUTPUT_INFO = "muse_min_output_tokens"  # model_info field, rendered from the catalogue's min_output_tokens
REASONING_HEADER = "x-muse-reasoning"  # response: the reasoning value the router set for this call (e.g. low)
REASONING_INFO = "muse_reasoning"  # model_info field, rendered from the catalogue's reasoning
REASONING_PARAMS = ("reasoning_effort",)
REASONING_VALUES = ("low", "medium", "high")
OUTPUT_CAP_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
AUTO_MODEL = "auto"
CACHE_PROVIDER = "cache"  # provider/upstream recorded for an answer served from the exact-repeat cache
TRUTHY = {"1", "true", "on", "yes"}
MARK = "[muse]"
NOTE_TTL_SECONDS = 600
NOTE_MAX = 4096
DEFAULT_OUTPUT_ESTIMATE = 1024  # tokens held for a paid call that sets no max_tokens


class MusePrivacyError(GuardrailRaisedException):
    """No deployment allowed for the request's privacy class.

    A GuardrailRaisedException, so the 1.108.6 router neither retries nor falls
    back across aliases (router.py: `is_guardrail_intervention(e)`).
    """

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(
            guardrail_name="muse-privacy",
            message=f"{MARK} {message}",
            should_wrap_with_default_message=False,
            status_code=status_code,
        )


class MuseBlockedError(RateLimitError):
    """Every allowed deployment in this group is blocked by a quota; LiteLLM may fall back."""

    def __init__(self, message: str, model: str, until: float | None = None) -> None:
        self.until = until  # when the soonest quota block ends; None when no quota block was involved
        super().__init__(message=f"{MARK} {message}", llm_provider="muse", model=model)


class MuseRationError(RateLimitError):
    """Rationing left nothing in this group (defer, or a paid refusal).

    A RateLimitError raised before any deployment is chosen: LiteLLM may try its
    fallback groups (each filtered again by the same rules), and it sets no
    cooldown because no deployment failed. No response headers are attached on
    purpose: the router would sleep on a Retry-After before its own retry. The
    client's Retry-After comes from async_post_call_response_headers_hook.
    """

    def __init__(self, decision: ration.Decision, model: str) -> None:
        self.decision = decision
        super().__init__(message=decision.message, llm_provider="muse", model=model)
        # Retrying the same group cannot change the answer, and the router would back off
        # for seconds first (router.py async_function_with_retries reads e.num_retries).
        self.num_retries = 0


class MuseAppCapError(GuardrailRaisedException):
    """The app reached its own daily cap: 429, and no fallback (every group would say the same)."""

    def __init__(self, decision: ration.Decision) -> None:
        self.decision = decision
        super().__init__(
            guardrail_name="muse-ration",
            message=decision.message,
            should_wrap_with_default_message=False,
            status_code=429,
        )


class MuseDirectRationError(GuardrailRaisedException):
    """Rationing refused the one deployment a request named, or the paid cap filled
    between routing and sending. Raised in the deployment hook, before the upstream
    is contacted; a guardrail intervention, so the router neither retries nor falls
    back, and the status is 429."""

    def __init__(self, decision: ration.Decision) -> None:
        self.decision = decision
        super().__init__(
            guardrail_name="muse-ration",
            message=decision.message,
            should_wrap_with_default_message=False,
            status_code=429,
        )


@dataclass
class _Note:
    """What rationing did for one request (one litellm_call_id), across groups and workers' stages."""

    at: float
    codes: set[str] = field(default_factory=set)
    decisions: list[ration.Decision] = field(default_factory=list)
    checked: set[str] = field(default_factory=set)  # deployment ids the filter allowed
    sent: dict[str, dict] = field(default_factory=dict)  # deployment id -> model_info, per attempt
    details: list[str] = field(default_factory=list)  # one line per rule that acted (no content)
    handoff: bool = False  # suggest the hand-off on this response
    cache: str | None = None  # hit | miss | bypass, decided once per request
    cache_key: str | None = None
    cache_privacy: str | None = None
    cache_value: str | None = None  # the answer pinned at lookup, so a later expiry cannot route it upstream unrationed
    # Provider limits met by this request: when the soonest quota block ends, and whether the
    # most recent failure was one (a later privacy refusal, rationing refusal or other error clears it).
    provider_until: float | None = None
    provider_limit_last: bool = False
    # The output floor, per deployment id: the x-muse-min-output value for the attempt sent to it.
    min_output: dict[str, str] = field(default_factory=dict)
    # What the latest attempt's raise changed (key -> (from, to)), so a request dict that is
    # reused for the next deployment gets the client's own value back first.
    raised_last: dict[str, tuple[int, int]] = field(default_factory=dict)
    # The reasoning setting, per deployment id (the x-muse-reasoning value), and what the latest attempt set.
    reasoning: dict[str, str] = field(default_factory=dict)
    reasoning_last: tuple[str, str] | None = None


def _metadata(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """The proxy's metadata bucket ("metadata" for chat, "litellm_metadata" for some routes)."""
    if not isinstance(data, Mapping):
        return {}
    for key in ("metadata", "litellm_metadata"):
        value = data.get(key)
        if isinstance(value, dict) and (value.get("headers") is not None or "model_info" in value or "muse_privacy" in value):
            return value
    for key in ("metadata", "litellm_metadata"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _headers(data: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(data, Mapping):
        return {}
    found: dict[str, str] = {}
    sources = [_metadata(data).get("headers")]
    psr = data.get("proxy_server_request")
    if isinstance(psr, Mapping):
        sources.append(psr.get("headers"))
    for source in sources:
        if isinstance(source, Mapping):
            for key, value in source.items():
                found.setdefault(str(key).lower(), str(value))
    return found


def _request_text(data: Mapping[str, Any]) -> str:
    """Text the auto-privacy detector scans. Held only in memory for this call."""
    parts: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    add(item.get("text") if item.get("type") in (None, "text", "input_text") else None)
                    add(item.get("content"))
                else:
                    add(item)
        elif isinstance(value, Mapping):
            add(value.get("text"))
            add(value.get("content"))

    for message in data.get("messages") or []:
        if isinstance(message, Mapping):
            add(message.get("content"))
    for key in ("input", "prompt", "query", "documents", "system"):
        add(data.get(key))
    return "\n".join(parts)


def _auth_policy(data: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The app policy muse/auth.py attached, wherever this stage keeps it."""
    if not isinstance(data, Mapping):
        return None
    buckets: list[Any] = [data.get("metadata"), data.get("litellm_metadata")]
    params = data.get("litellm_params")
    if isinstance(params, Mapping):
        buckets += [params.get("metadata"), params.get("litellm_metadata")]
    for bucket in buckets:
        if isinstance(bucket, Mapping):
            policy = bucket.get("user_api_key_metadata")
            if isinstance(policy, Mapping) and policy.get("muse_app"):
                return policy
    slo = data.get("standard_logging_object")
    if isinstance(slo, Mapping):
        policy = (slo.get("metadata") or {}).get("user_api_key_auth_metadata")
        if isinstance(policy, Mapping) and policy.get("muse_app"):
            return policy
    return None


def app_of(data: Mapping[str, Any] | None) -> apps_mod.App:
    """The authenticated app; `anonymous` (no paid) when the request carries none."""
    return apps_mod.from_policy(_auth_policy(data)) or apps_mod.ANONYMOUS


def _call_id(data: Mapping[str, Any] | None) -> str | None:
    if not isinstance(data, Mapping):
        return None
    value = data.get("litellm_call_id")
    if not value:
        params = data.get("litellm_params")
        if isinstance(params, Mapping):
            value = params.get("litellm_call_id")
    return str(value) if value else None


def _estimate_tokens(data: Mapping[str, Any]) -> tuple[int, int]:
    """Rough, deliberately high (input, output) token estimate for a paid hold: one
    token per 3 ASCII characters (English averages about 4) plus one per other
    character (CJK is about 1-1.5 characters a token); output = max_tokens or 1024."""
    text = _request_text(data)
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    chars = ascii_chars + 3 * (len(text) - ascii_chars)
    out = 0
    for key in OUTPUT_CAP_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out = value
            break
    return chars // 3 + 1, out or DEFAULT_OUTPUT_ESTIMATE


def _truthy_header(data: Mapping[str, Any] | None, name: str) -> bool:
    return str(_headers(data).get(name, "") or "").strip().lower() in TRUTHY


def _call_mode(call_type: Any) -> str | None:
    """chat | embedding | rerank for a LiteLLM call type; None when it cannot be told."""
    text = str(getattr(call_type, "value", call_type) or "").lower()
    if "embedding" in text:
        return "embedding"
    if "rerank" in text:
        return "rerank"
    if "completion" in text or "responses" in text or "messages" in text:
        return "chat"
    return None


def apply_ceiling(data: dict[str, Any], ceiling: int) -> None:
    """Cap the answer length: a lower limit the client set is kept, a higher one is lowered."""
    present = False
    for key in OUTPUT_CAP_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            present = True
            if value > ceiling:
                data[key] = ceiling
    if not present:
        data["max_tokens"] = ceiling


def min_output_of(info: Mapping[str, Any] | None) -> int | None:
    """The deployment's output floor (model_info.muse_min_output_tokens); None when it has none."""
    value = info.get(MIN_OUTPUT_INFO) if isinstance(info, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def apply_floor(data: dict[str, Any], floor: int | None) -> dict[str, tuple[int, int]]:
    """Raise an output cap the request set that is below `floor`; the mirror of apply_ceiling.

    Returns {key: (before, after)} for what changed. Never lowers a value, and
    never adds a cap to a request that set none (the provider's default applies).
    """
    changed: dict[str, tuple[int, int]] = {}
    if not floor or floor <= 0:
        return changed
    for key in OUTPUT_CAP_KEYS:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < floor:
            data[key] = floor
            changed[key] = (value, floor)
    return changed


def min_output_header(changed: Mapping[str, tuple[int, int]], ceiling: Any = None) -> str | None:
    """`raised:<from>-><to>`, plus `;over-ceiling:<n>` when the task's ceiling n is below the floor."""
    if not changed:
        return None
    before = min(pair[0] for pair in changed.values())
    after = max(pair[1] for pair in changed.values())
    text = f"raised:{before}->{after}"
    if isinstance(ceiling, int) and not isinstance(ceiling, bool) and 0 < ceiling < after:
        text += f";over-ceiling:{ceiling}"
    return text


def reasoning_of(info: Mapping[str, Any] | None, task: Any) -> tuple[str, str] | None:
    """(parameter, value) the deployment's model_info.muse_reasoning names for this task; None when none applies."""
    raw = info.get(REASONING_INFO) if isinstance(info, Mapping) else None
    if not isinstance(raw, Mapping) or not isinstance(task, str):
        return None
    param, value = raw.get("param"), raw.get(task)
    if param in REASONING_PARAMS and value in REASONING_VALUES:
        return str(param), str(value)
    return None


def apply_reasoning(data: dict[str, Any], setting: tuple[str, str] | None) -> str | None:
    """Set the reasoning parameter when the request did not set it. Returns the value set, or None."""
    if not setting:
        return None
    param, value = setting
    if data.get(param) is not None:
        return None  # the client's own choice wins
    data[param] = value
    return value


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def empty_reasoning(response: Any) -> bool:
    """A chat answer with no visible text because the output allowance went on reasoning.

    True when every choice has empty content and no tool call, and either a
    choice stopped at the length limit or the usage reports reasoning tokens.
    Reads lengths and flags only; nothing from the answer is kept or logged.
    """
    choices = _get(response, "choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        return False
    cut = False
    for choice in choices:
        message = _get(choice, "message")
        if message is None:
            return False  # not a chat completion (a stream chunk, a text completion)
        content = _get(message, "content")
        if isinstance(content, str):
            if content.strip():
                return False
        elif content:  # a list of parts, or anything else that is not empty
            return False
        if _get(message, "tool_calls") or _get(message, "function_call"):
            return False
        if _get(choice, "finish_reason") == "length":
            cut = True
    if cut:
        return True
    details = _get(_get(response, "usage"), "completion_tokens_details")
    spent = _get(details, "reasoning_tokens")
    return isinstance(spent, int) and not isinstance(spent, bool) and spent > 0


def _model_info(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    info = data.get("model_info")
    if isinstance(info, Mapping) and info.get("id"):
        return dict(info)
    info = _metadata(data).get("model_info")
    if isinstance(info, Mapping):
        return dict(info)
    params = data.get("litellm_params")
    if isinstance(params, Mapping):
        return _model_info(params)
    return {}


def _upstream(info: Mapping[str, Any]) -> tuple[str, str] | None:
    """(provider, upstream) for a muse-rendered deployment, else None."""
    provider = info.get("provider")
    upstream = info.get("muse_upstream")
    if not provider or not upstream:
        return None
    return str(provider), str(upstream)


def _seconds(start: Any, end: Any) -> float | None:
    try:
        if isinstance(start, datetime) and isinstance(end, datetime):
            return (end - start).total_seconds()
        return float(end) - float(start)
    except (TypeError, ValueError):
        return None


class MuseHook(CustomLogger):
    """See module docstring."""

    def __init__(
        self,
        ledger: Ledger | None = None,
        clock: Callable[[], float] = time.time,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(turn_off_message_logging=True)
        self._ledger = ledger
        self.clock = clock
        self.environ = environ
        self.errors: Counter[str] = Counter()
        self._notes: OrderedDict[str, _Note] = OrderedDict()
        self._notes_lock = threading.Lock()
        self._warned: set[str] = set()
        self._cache: cache_mod.ExactCache | None = None

    # ------------------------------------------------------------ helpers

    @property
    def env(self) -> Mapping[str, str]:
        return os.environ if self.environ is None else self.environ

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = Ledger(clock=self.clock, environ=self.environ)
        return self._ledger

    def _fault(self, where: str, exc: BaseException) -> None:
        """Count and log a bookkeeping error. Type only: messages may echo request text."""
        self.errors[where] += 1
        log.warning("muse hook: %s failed (%s); continuing with LiteLLM defaults", where, type(exc).__name__)

    def _default_class(self, data: Mapping[str, Any] | None = None) -> str:
        from muse import policy

        app_default = app_of(data).default_privacy if data is not None else None
        raw = app_default or self.env.get("MUSE_DEFAULT_PRIVACY", "").strip() or "standard"
        try:
            return policy.normalise_class(raw)
        except policy.PolicyError:
            # A broken default must not silently widen; the strictest class is safe.
            log.error("muse hook: MUSE_DEFAULT_PRIVACY is not a privacy class; using 'local'")
            return "local"

    def _requested_class(self, data: Mapping[str, Any] | None) -> str:
        """Class from the header (or the default). Raises PolicyError on a bad value."""
        from muse import policy

        header = _headers(data).get(PRIVACY_HEADER)
        return policy.normalise_class(header, self._default_class(data))

    def privacy_of(self, data: Mapping[str, Any] | None) -> str:
        """The class to enforce: the stricter of what the pre-call hook resolved
        and what the header says now, so request metadata cannot lower it."""
        from muse import policy

        requested = self._requested_class(data)
        resolved = _metadata(data).get("muse_privacy")
        if isinstance(resolved, str) and resolved:
            try:
                return policy.stricter(policy.normalise_class(resolved), requested)
            except policy.PolicyError:
                return requested
        return requested

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    def settings(self) -> ration.Settings:
        settings = ration.Settings.from_env(self.env)
        for warning in settings.warnings:
            if warning not in self._warned:
                self._warned.add(warning)
                log.warning("muse hook: %s", warning)
        return settings

    def rules(self) -> rules_mod.Rules:
        """The routing rules in force; a broken file is reported once per problem, then defaults are used."""
        current = rules_mod.load_rules(self.env)
        for problem in current.problems:
            if problem not in self._warned:
                self._warned.add(problem)
                log.error("muse hook: routing rules not used, built-in rules in force: %s", problem)
        return current

    @property
    def cache(self) -> cache_mod.ExactCache:
        settings = cache_mod.CacheSettings.from_env(self.env)
        if self._cache is None or self._cache.settings != settings:
            self._cache = cache_mod.ExactCache(settings, self.environ)
        return self._cache

    async def _cache_lookup(self, alias: str, data: Mapping[str, Any] | None, privacy: str) -> str | None:
        """The cached answer for this request, decided once per request (see muse/cache.py)."""
        if not cache_mod.CacheSettings.from_env(self.env).enabled or not isinstance(data, Mapping):
            return None
        note = self._note(_call_id(data))
        if note is None:
            return None
        if note.cache is not None:
            return note.cache_value if note.cache == "hit" else None
        ok, _why = cache_mod.cacheable(data, deterministic=bool(_metadata(data).get("muse_deterministic")))
        if not ok:
            note.cache = "bypass"
            return None
        key = cache_mod.cache_key(alias, data, privacy, self._default_class(data))
        try:
            value = await self._run(self.cache.get, key, privacy, self.clock())
        except Exception as exc:
            self._fault("cache_get", exc)
            note.cache = "bypass"
            return None
        note.cache, note.cache_key, note.cache_privacy, note.cache_value = ("hit" if value is not None else "miss"), key, privacy, value
        if value is not None:
            note.details.append("cache: exact repeat answered from the cache")
        return value

    def _note(self, call_id: str | None, create: bool = True) -> _Note | None:
        if not call_id:
            return None
        now = time.monotonic()
        with self._notes_lock:
            note = self._notes.get(call_id)
            if note is None and create:
                note = self._notes[call_id] = _Note(at=now)
                while len(self._notes) > NOTE_MAX:
                    self._notes.popitem(last=False)
            while self._notes:
                oldest_key, oldest = next(iter(self._notes.items()))
                if now - oldest.at <= NOTE_TTL_SECONDS:
                    break
                self._notes.popitem(last=False)
            return note

    def _failed(self, data: Mapping[str, Any] | None, provider_until: float | None = None) -> None:
        """Remember how the latest failure of this request ended, for the response headers.

        `provider_until` is when the quota block behind a provider's 429 ends; None
        means the failure was something else (a privacy or rationing refusal, a
        server error), which must never be answered with the provider's wait.
        """
        try:
            note = self._note(_call_id(data))
            if note is None:
                return
            if provider_until is None:
                note.provider_limit_last = False
                return
            note.provider_limit_last = True
            if note.provider_until is None or provider_until < note.provider_until:
                note.provider_until = float(provider_until)
        except Exception as exc:
            self._fault("failed_note", exc)

    async def _ration_state(
        self, app: apps_mod.App, deployments: list[dict], settings: ration.Settings, now: float
    ) -> ration.State:
        """Read only what the rules need for this app and these deployments."""
        db = self.ledger
        tz = settings.tz
        start, end = ration.local_day_bounds(now, tz)
        state = ration.State(day_end=end)
        if app.daily_requests is not None or app.daily_tokens is not None:
            usage = await self._run(db.app_usage, app.name, now=now, tz=tz)
            state.app_requests_today, state.app_tokens_today = usage["requests"], usage["tokens"]
        infos = [dep.get("model_info") or {} for dep in deployments]
        if app.allow_paid and settings.paid_cap_usd > 0 and any(ration.is_paid(i) for i in infos):
            try:
                spend = await self._run(db.paid_spend, now=now, tz=tz)
                state.paid_spend_usd = Decimal(str(spend["usd"] + spend["held_usd"]))
            except Exception as exc:
                self._fault("paid_spend", exc)
                state.paid_spend_usd = None  # the paid gate closes
        if app.priority == "background" and settings.active_window_s > 0:
            state.last_interactive = await self._run(db.last_interactive_success, now - settings.active_window_s)
        # Limits for every app: the reserve and pacing read them for normal and
        # background apps, the ordering hint (ration.prefer) for every app.
        pairs = [(_upstream(i), i) for i in infos]
        pairs = [(pair, i) for pair, i in pairs if pair is not None]
        if pairs:
            scopes = sorted({p[1] for p, _ in pairs} | {f"provider:{p[0]}" for p, _ in pairs})
            observed = await self._run(db.limits_many, scopes)
            usage_by_provider: dict[str, Any] = {}
            limits: dict[str, list[ration.Limit]] = {}
            for (provider, upstream), info in pairs:
                if info.get("muse_quota") and provider not in usage_by_provider:
                    usage_by_provider[provider] = await self._run(db.usage, provider=provider, now=now)
                rows = [*observed.get(upstream, []), *observed.get(f"provider:{provider}", [])]
                limits[upstream] = ration.known_limits(info, rows, usage_by_provider.get(provider, {}), now)
            state.limits = limits
            if len(limits) >= 2 and any(limits.values()):
                burn_scopes = sorted({lim.scope or upstream for upstream, items in limits.items() for lim in items})
                state.burn = await self._run(db.burn, burn_scopes, now - settings.burn_window_s, now=now)
        return state

    async def _apply_ration(
        self,
        model: str,
        deployments: list[dict],
        data: Mapping[str, Any] | None,
        prior: list[ration.Removed],
        *,
        direct: bool,
        messages: list | None = None,
        prefer: bool = False,
    ) -> ration.Outcome:
        """The rules for one group, then the ordering hint. Errors degrade to no
        rationing, minus paid and one-off deployments."""
        now = self.clock()
        app = app_of(data)
        settings = ration.Settings()
        state: ration.State | None = None
        handoff_task = bool(_metadata(data).get("muse_handoff"))
        allow_oneoff = _truthy_header(data, ONEOFF_HEADER)
        handoff_hold = handoff_task and not _truthy_header(data, ALLOW_PAID_HEADER)
        try:
            settings = self.settings()
            state = await self._ration_state(app, deployments, settings, now)
            outcome = ration.ration(ration.AppPolicy.of(app), deployments, state, settings, now, direct=direct, prior=prior,
                                    allow_oneoff=allow_oneoff, handoff_hold=handoff_hold)
        except Exception as exc:
            self._fault("ration", exc)
            kept = ration.fail_closed(deployments)
            removed = [
                ration.Removed(str((d.get("model_info") or {}).get("id", "")), "", ration.PAID_SPEND_UNKNOWN, ration.REFUSE, now + 60,
                               "the paid check failed, so paid providers are off for now")
                for d in deployments if d not in kept
            ]
            decision = None if kept else ration.decide(ration.AppPolicy.of(app), [*prior, *removed], now, ration.Settings())
            outcome = ration.Outcome(kept, removed, decision)
        note = self._note(_call_id(data))
        if outcome.kept and prefer and state is not None:
            try:
                target = data.get("_target_order") if isinstance(data, Mapping) else None
                narrowed, reason = ration.prefer(outcome.kept, state, settings, now, target_order=target,
                                                 tried=list(note.sent) if note is not None else ())
                if reason is not None:
                    outcome.kept = narrowed
                    if note is not None:
                        if reason.startswith(ration.PACE_PREFER):
                            note.codes.add(ration.PACE_PREFER)
                        note.details.append(reason)
            except Exception as exc:
                self._fault("prefer", exc)
        if outcome.decision is not None:
            handoff = handoff_task or self._strong(model, data)
            estimates = self._pay_estimates(deployments, outcome.removed, data, messages)
            outcome.decision = ration.with_options(outcome.decision, [*prior, *outcome.removed], handoff=handoff,
                                                   pay_estimates=estimates)
            if note is not None and handoff:
                note.handoff = True
        if note is not None:
            note.codes.update(r.code for r in outcome.removed)
            note.checked.update(str((d.get("model_info") or {}).get("id", "")) for d in outcome.kept)
            note.details.extend(f"{r.code}: {r.upstream or r.deployment_id}: {r.detail}" for r in outcome.removed[:20])
            if outcome.decision is not None:
                note.decisions.append(outcome.decision)
        if outcome.decision is not None:
            await self._record_ration(app, model, outcome.decision, now)
        return outcome

    def _cooling(self, model: str, healthy: list, privacy: str) -> list[dict]:
        """Deployments of this group that the proxy is cooling down and that this privacy class may use.

        Read-only look at the router's own list. Any problem means "none", which
        is the behaviour from before this helper existed.
        """
        from muse import policy

        try:
            # Only inside a running proxy; never import it from here (slow, and it has side effects).
            proxy_server = sys.modules.get("litellm.proxy.proxy_server")
            router = getattr(proxy_server, "llm_router", None)
            if router is None:
                return []
            present = {str((d.get("model_info") or {}).get("id", "")) for d in healthy if isinstance(d, Mapping)}
            rows = router.get_model_list(model_name=model) or []
            missing = [dict(d) for d in rows
                       if isinstance(d, Mapping) and str((d.get("model_info") or {}).get("id", "")) not in present]
            return policy.filter_deployments(missing, privacy) if missing else []
        except Exception as exc:
            self._fault("cooling", exc)
            return []

    def _strong(self, model: str, data: Mapping[str, Any] | None) -> bool:
        """Whether a refusal for this group means the strong allowance is unavailable (offer the hand-off)."""
        group = _metadata(data).get("model_group") or (data.get("model") if isinstance(data, Mapping) else None) or model
        try:
            return str(group) in self.rules().handoff_aliases or str(model) in self.rules().handoff_aliases
        except Exception as exc:
            self._fault("rules", exc)
            return False

    def _pay_estimates(self, deployments: list[dict], removed: list[ration.Removed], data: Mapping[str, Any] | None,
                       messages: list | None) -> dict[str, float]:
        """Estimated USD per paid deployment held for the hand-off: the same estimate the paid hold uses."""
        held = {r.deployment_id for r in removed if r.code == ration.HANDOFF_FIRST}
        if not held or not isinstance(data, Mapping):
            return {}
        view = dict(data)
        if messages is not None and not view.get("messages"):
            view["messages"] = messages
        tin, tout = _estimate_tokens(view)
        caps = {key: view.get(key) for key in OUTPUT_CAP_KEYS}
        out: dict[str, float] = {}
        for dep in deployments:
            info = dep.get("model_info") or {}
            dep_id = str(info.get("id", ""))
            price = ration.price_of(info)
            if dep_id in held and price is not None:
                floored = dict(caps)
                # The paid hold is estimated on the raised cap (see _output_floor), so the offer is too.
                dep_out = _estimate_tokens(floored)[1] if apply_floor(floored, min_output_of(info)) else tout
                est = cost_mod.from_price(price, tin, dep_out)
                if est is not None:
                    out[dep_id] = float(est)
        return out

    async def _record_ration(self, app: apps_mod.App, model: str, decision: ration.Decision, now: float) -> None:
        try:
            await self._run(self.ledger.record_ration, app=app.name, priority=app.priority, alias=model,
                            kind=decision.kind, code=decision.code, now=now)
        except Exception as exc:
            self._fault("record_ration", exc)

    # ------------------------------------------------------------ proxy pre-call

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        from muse import policy

        try:
            requested = _headers(data).get(PRIVACY_HEADER)
            default = self._default_class(data)
            auto = self.env.get("MUSE_AUTO_PRIVACY", "").strip().lower() in TRUTHY
            if requested is not None and not requested.strip():
                requested = None
            try:
                text = _request_text(data) if auto else ""
                cls, findings = policy.effective_class(requested, default, text, auto)
            except policy.PolicyError:
                raise
            except Exception as exc:  # detector crashed: fail closed to at least private
                self._fault("auto_privacy", exc)
                cls = policy.normalise_class(requested, default)
                if auto:
                    cls = policy.stricter(cls, "private")
                findings = []
        except policy.PolicyError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{MARK} invalid {PRIVACY_HEADER}: {exc}. Use one of {', '.join(policy.PRIVACY_CLASSES)}.",
            ) from None
        metadata = _metadata(data)
        if not any(isinstance(data.get(key), dict) for key in ("metadata", "litellm_metadata")):
            metadata = data["metadata"] = {}
        metadata["muse_privacy"] = cls
        metadata.pop("muse_output_ceiling", None)  # only _apply_task may say a task ceiling was applied
        app = app_of(data)
        metadata["muse_app"] = app.name  # informational; rules read user_api_key_metadata
        metadata["muse_priority"] = app.priority
        if findings:
            metadata["muse_privacy_findings"] = sorted({f.kind for f in findings})  # kinds only, never text
        self._apply_task(data, call_type, app, metadata)
        return data

    def _apply_task(self, data: dict, call_type: Any, app: apps_mod.App, metadata: dict) -> None:
        """Rule 2: replace the model with the task's alias and cap the answer length."""
        header = str(_headers(data).get(TASK_HEADER, "") or "").strip()
        if not header and data.get("model") != AUTO_MODEL:
            return
        current = self.rules()
        tokens_in, _ = _estimate_tokens(data)
        try:
            route = current.resolve(header or None, app=app.name, input_tokens_estimate=tokens_in)
        except rules_mod.UnknownTask as exc:
            raise HTTPException(
                status_code=400,
                detail=f"{MARK} invalid {TASK_HEADER}: unknown task '{exc.name}'. Valid tasks: {', '.join(exc.valid)}.",
            ) from None
        mode = _call_mode(call_type)
        if mode is not None and mode != route.mode:
            raise HTTPException(
                status_code=400,
                detail=f"{MARK} task '{route.task}' is a {route.mode} task, but this is a {mode} request.",
            )
        data["model"] = route.alias
        if route.mode == "chat" and route.max_output_tokens:
            apply_ceiling(data, route.max_output_tokens)
            metadata["muse_output_ceiling"] = int(route.max_output_tokens)  # for x-muse-min-output's over-ceiling
        metadata["muse_task"] = route.task
        metadata["muse_handoff"] = route.handoff
        metadata["muse_deterministic"] = route.deterministic
        metadata["muse_task_reason"] = route.reason

    # ------------------------------------------------------------ routing

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None = None,
        parent_otel_span: Any = None,
    ) -> list[dict]:
        try:
            return await self._filter_deployments(model, healthy_deployments, messages, request_kwargs)
        except MuseBlockedError as exc:
            self._failed(request_kwargs, exc.until)  # None when no quota block was behind it
            raise
        except Exception:
            self._failed(request_kwargs)
            raise

    async def _filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None,
    ) -> list[dict]:
        from muse import policy

        # Privacy first decides what is allowed at all. Fails closed.
        try:
            privacy = self.privacy_of(request_kwargs)
            allowed = policy.filter_deployments(list(healthy_deployments), privacy)
        except policy.PolicyError as exc:
            raise MusePrivacyError(f"invalid {PRIVACY_HEADER}: {exc}", status_code=400) from None
        except Exception as exc:
            self.errors["privacy_filter"] += 1
            raise MusePrivacyError(f"privacy check failed ({type(exc).__name__}); refusing to route") from None
        # LiteLLM hands over only the deployments it is not cooling down. One it
        # cooled after a provider error is still part of the picture: it is why
        # the list is short, and its quota block says when to come back.
        cooling = self._cooling(model, healthy_deployments, privacy)
        if not allowed and not cooling:
            raise MusePrivacyError(
                f"no deployment of '{model}' may receive a '{privacy}' request "
                f"({len(healthy_deployments)} not cooling right now, none allowed: "
                + ("only local models qualify" if privacy == "local" else
                   "it needs a local model or a provider that does not train on inputs or allow human review")
                + "). Not falling back to other providers."
            )

        # An exact repeat needs no allowance: answer it from the cache (deployment hook).
        try:
            view = dict(request_kwargs or {})
            if messages is not None and not view.get("messages"):
                view["messages"] = messages
            if await self._cache_lookup(model, view, privacy) is not None:
                return allowed
        except Exception as exc:
            self._fault("cache", exc)

        # Then quota blocks. Errors here degrade to LiteLLM's own behaviour.
        kept: list[dict] = list(allowed)
        prior: list[ration.Removed] = []
        try:
            blocks = await self._run(self.ledger.active_blocks, self.clock())
        except Exception as exc:
            self._fault("active_blocks", exc)
            blocks = {}
        if blocks or not allowed:
            kept, hit = [], []
            for deployment, usable in [(d, True) for d in allowed] + [(d, False) for d in cooling]:
                info = deployment.get("model_info") or {}
                pair = _upstream(info)
                block = None
                if pair is not None:
                    block = blocks.get(pair[1]) or blocks.get(f"provider:{pair[0]}")
                if block is None:
                    if usable:
                        kept.append(deployment)
                else:
                    hit.append(block)
                    prior.append(ration.Removed(str(info.get("id", "")), pair[1] if pair else "", ration.BLOCKED, ration.DEFER,
                                                float(block["until"]), f"blocked by a provider quota until {quota.iso(block['until'])}"))
            if not kept and (hit or not allowed):
                handoff = bool(_metadata(request_kwargs).get("muse_handoff")) or self._strong(model, request_kwargs)
                note = self._note(_call_id(request_kwargs))
                if note is not None and handoff:
                    note.handoff = True
                if not hit:
                    # Cooled by the proxy after a provider error that set no quota block (a server error, say).
                    raise MuseBlockedError(
                        f"every '{privacy}'-allowed deployment of '{model}' is resting after a provider error; "
                        f"try again shortly | " + ration.options_trailer(None, handoff, None),
                        model=model,
                    )
                soonest = min(hit, key=lambda b: b["until"])
                raise MuseBlockedError(
                    f"every '{privacy}'-allowed deployment of '{model}' is blocked by a provider quota; "
                    f"soonest reset {quota.iso(soonest['until'])} ({soonest['kind']}: {soonest['reason']}) | "
                    + ration.options_trailer(float(soonest["until"]), handoff, None),
                    model=model,
                    until=float(soonest["until"]),
                )

        # Then rationing between apps (muse/ration.py).
        direct = len(kept) == 1 and str((kept[0].get("model_info") or {}).get("id", "")) == model
        outcome = await self._apply_ration(model, kept, request_kwargs, prior, direct=direct, messages=messages, prefer=not direct)
        if outcome.kept:
            return outcome.kept
        decision = outcome.decision or ration.Decision(ration.REFUSE, "unknown", f"{MARK} rationing left nothing for '{model}'", None)
        if decision.code in (ration.APP_REQUESTS, ration.APP_TOKENS):
            raise MuseAppCapError(decision)
        raise MuseRationError(decision, model=model)

    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> dict | None:
        """Last check on the deployment actually chosen (covers requests by deployment id)."""
        try:
            return await self._pre_call_deployment(kwargs, call_type)
        except Exception:
            self._failed(kwargs)  # a refusal here is never a provider's wait
            raise

    async def _pre_call_deployment(self, kwargs: dict, call_type: Any) -> dict | None:
        from muse import policy

        info = _model_info(kwargs)
        if not info:
            return None  # not a router call (e.g. a health probe); nothing to judge
        sent = self._note(_call_id(kwargs))
        if sent is not None and info.get("id"):
            sent.sent[str(info["id"])] = dict(info)  # after a fallback, request data holds a stale model_info
        try:
            privacy = self.privacy_of(kwargs)
            ok = policy.allowed(info, privacy)
        except policy.PolicyError as exc:
            raise MusePrivacyError(f"invalid {PRIVACY_HEADER}: {exc}", status_code=400) from None
        except Exception as exc:
            self.errors["privacy_deployment"] += 1
            raise MusePrivacyError(f"privacy check failed ({type(exc).__name__}); refusing to send") from None
        if not ok:
            raise MusePrivacyError(
                f"deployment '{info.get('id')}' may not receive a '{privacy}' request: {policy.explain(info, privacy)}"
            )

        # An exact repeat: answer from the cache, nothing is sent and nothing is held.
        call_id = _call_id(kwargs)
        dep_id = str(info.get("id", ""))
        try:
            alias = _metadata(kwargs).get("model_group") or dep_id
            cached = await self._cache_lookup(str(alias), kwargs, privacy)
        except Exception as exc:
            self._fault("cache", exc)
            cached = None
        if cached is not None:
            kwargs["mock_response"] = cache_mod.mock_response(cached, str(kwargs.get("model") or ""))
            return kwargs

        # Rationing for requests that named a deployment id (the filter did not run).
        note = self._note(call_id, create=False)
        if note is None or dep_id not in note.checked:
            outcome = await self._apply_ration(dep_id, [{"model_info": info}], kwargs, [], direct=True)
            if not outcome.kept:
                assert outcome.decision is not None
                raise MuseDirectRationError(outcome.decision)

        # The output floor for this deployment, before the paid hold so the hold counts the raised cap.
        raised = self._output_floor(info, kwargs, call_type, call_id)
        reasoned = self._reasoning(info, kwargs, call_type, call_id)

        # Record/replay sits on this one call, after the floor, so the cassette
        # sees the cap the provider will see. Live mode returns without touching
        # api_base. The returned dict is what LiteLLM sends, so a reroute has to
        # be returned or the original base URL is what gets called.
        from muse import cassette

        routed = cassette.reroute(kwargs, self.env)

        # The paid cap, atomically across workers, right before money is spent.
        if ration.is_paid(info):
            await self._hold_paid(info, kwargs, call_id)
        return kwargs if raised or reasoned or routed else None

    def _reasoning(self, info: Mapping[str, Any], kwargs: dict, call_type: Any, call_id: str | None) -> bool:
        """Set the deployment's reasoning parameter for this task on this attempt. True when it changed the request.

        Chat calls only, and only when the client did not set the parameter. As with the floor, a value the
        previous attempt set is taken back first, so a fallback deployment without the setting gets the client's
        own request. An error here is counted and the request goes out as the client sent it.
        """
        try:
            note = self._note(call_id, create=False)
            dep_id = str(info.get("id", ""))
            if note is not None:
                if note.reasoning_last is not None and kwargs.get(note.reasoning_last[0]) == note.reasoning_last[1]:
                    kwargs.pop(note.reasoning_last[0], None)
                note.reasoning_last = None
                note.reasoning.pop(dep_id, None)
            if _call_mode(call_type) != "chat":
                return False
            setting = reasoning_of(info, _metadata(kwargs).get("muse_task"))
            value = apply_reasoning(kwargs, setting)
            if value is None or setting is None:
                return False
            note = self._note(call_id)
            if note is not None:
                note.reasoning_last = (setting[0], value)
                note.reasoning[dep_id] = value
                note.details.append(f"reasoning: {dep_id}: {setting[0]}={value}")
            return True
        except Exception as exc:
            self._fault("reasoning", exc)
            return False

    def _output_floor(self, info: Mapping[str, Any], kwargs: dict, call_type: Any, call_id: str | None) -> bool:
        """Raise this attempt's output cap to the chosen deployment's floor. True when it changed.

        Chat calls only. `kwargs` is this attempt's own copy of the request
        (litellm/utils.py async_pre_call_deployment_hook copies it), so the raise
        reaches this upstream call and no other. Should a caller ever reuse one
        dict across deployments, what the previous attempt raised is put back
        first: a deployment without a floor gets the client's own value.
        An error here is counted and the request goes out as the client sent it.
        """
        try:
            note = self._note(call_id, create=False)
            dep_id = str(info.get("id", ""))
            if note is not None:
                for key, (before, after) in note.raised_last.items():
                    if kwargs.get(key) == after:
                        kwargs[key] = before
                note.raised_last = {}
                note.min_output.pop(dep_id, None)
            if _call_mode(call_type) != "chat":
                return False
            changed = apply_floor(kwargs, min_output_of(info))
            if not changed:
                return False
            header = min_output_header(changed, _metadata(kwargs).get("muse_output_ceiling"))
            note = self._note(call_id)
            if note is not None and header:
                note.raised_last = dict(changed)
                note.min_output[dep_id] = header
                note.details.append(f"min-output: {dep_id}: {header}")
            return True
        except Exception as exc:
            self._fault("min_output", exc)
            return False

    async def _hold_paid(self, info: Mapping[str, Any], kwargs: Mapping[str, Any], call_id: str | None) -> None:
        settings = self.settings()
        app = app_of(kwargs)
        now = self.clock()
        refuse = None
        if not app.allow_paid or settings.paid_cap_usd <= 0:
            refuse = ration.paid_gate(info, ration.AppPolicy.of(app), ration.State(), settings, direct=True)
        else:
            price = ration.price_of(info)
            est = 0.0
            if price is not None:
                tin, tout = _estimate_tokens(kwargs)
                est = cost_mod.from_price(price, tin, tout) or 0.0
            try:
                admitted, spend = await self._run(
                    self.ledger.try_hold_paid, call_id or f"anon-{id(kwargs)}", str(info.get("id", "")), est,
                    float(settings.paid_cap_usd), now=now, tz=settings.tz,
                )
            except Exception as exc:
                self._fault("paid_hold", exc)
                admitted = False
                refuse = (ration.PAID_SPEND_UNKNOWN, "today's paid spend could not be read, so paid providers are off for now")
            if not admitted and refuse is None:
                refuse = (ration.PAID_CAP, f"daily paid cap of {ration.format_usd(settings.paid_cap_usd)} reached; resets at local midnight")
        if refuse is None:
            return
        code, detail = refuse
        _, day_end = ration.local_day_bounds(now, settings.tz)
        decision = ration.Decision(ration.REFUSE, code, f"{MARK} {detail}", day_end if code == ration.PAID_CAP else None)
        note = self._note(call_id)
        if note is not None:
            note.codes.add(code)
            note.decisions.append(decision)
        await self._record_ration(app, str(info.get("id", "")), decision, now)
        raise MuseDirectRationError(decision)

    # ------------------------------------------------------------ outcomes

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, Any],
        exception: Exception,
        call_type: Any,
        fallback_depth: int | None = None,
    ) -> None:
        try:
            if isinstance(exception, (MusePrivacyError, MuseBlockedError, MuseRationError, MuseAppCapError, MuseDirectRationError)) \
                    or MARK in str(getattr(exception, "message", "")):
                self._failed(request_data)
                return
            self._failed(request_data)  # until the lines below show this was a provider limit
            info = _model_info(request_data)
            pair = _upstream(info)
            if pair is None:
                return
            provider, upstream = pair
            now = self.clock()
            status = getattr(exception, "status_code", None)
            status = status if isinstance(status, int) else None
            headers = getattr(exception, "litellm_response_headers", None)
            if not headers:
                response = getattr(exception, "response", None)
                headers = getattr(response, "headers", None)
            body = getattr(exception, "body", None)
            body_text = ""
            if body is not None:
                try:
                    import json

                    body_text = json.dumps(body, default=str)
                except (TypeError, ValueError):
                    body_text = str(body)
            body_text = f"{body_text} {getattr(exception, 'message', '') or exception}"
            block = quota.classify_failure(provider, status, dict(headers or {}), body_text, now)
            if block is not None:
                written = await self._run(self.ledger.set_block, block.key(provider, upstream), block.until, block.kind, block.reason, now)
                if status == 429:
                    self._failed(request_data, await self._block_until(provider, upstream, now, None if not written else block.until))
            elif status == 429:
                # Not understood well enough to block, but a block already in force still says when to return.
                until = await self._block_until(provider, upstream, now, None)
                if until is not None:
                    self._failed(request_data, until)
            latency = None
            logging_obj = request_data.get("litellm_logging_obj")
            started = getattr(logging_obj, "start_time", None)
            if isinstance(started, datetime):
                latency = (datetime.now(started.tzinfo) - started).total_seconds()
            app = app_of(request_data)
            await self._run(
                self.ledger.record_call,
                provider=provider,
                deployment_id=str(info.get("id", "")),
                upstream=upstream,
                alias=_metadata(request_data).get("model_group"),
                ok=False,
                status=status,
                latency_ms=int(latency * 1000) if latency is not None else None,
                privacy=self._privacy_for_record(request_data),
                now=now,
                app=app.name,
                priority=app.priority,
                cost_usd=None,
                paid=ration.is_paid(info),
                release_hold=_call_id(request_data),
            )
        except Exception as exc:
            self._fault("failure_event", exc)

    async def _block_until(self, provider: str, upstream: str, now: float, known: float | None) -> float | None:
        """When the block on this upstream ends: `known` if this failure just wrote it,
        else what the ledger holds (set_block keeps a longer block that was already there)."""
        if known is not None:
            return float(known)
        blocks = await self._run(self.ledger.active_blocks, now)
        found = [float(b["until"]) for b in (blocks.get(upstream), blocks.get(f"provider:{provider}")) if b]
        return max(found) if found else None

    def _privacy_for_record(self, data: Mapping[str, Any]) -> str:
        try:
            return self.privacy_of(data)
        except Exception:
            return "unknown"

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
            note = self._note(_call_id(kwargs), create=False)
            if note is not None and note.cache == "hit":
                await self._record_cached(kwargs)
                return
            info = _model_info(kwargs)
            pair = _upstream(info)
            if pair is None:
                return
            provider, upstream = pair
            now = self.clock()
            slo = kwargs.get("standard_logging_object") or {}
            tokens_in = slo.get("prompt_tokens")
            tokens_out = slo.get("completion_tokens")
            usage = getattr(response_obj, "usage", None)
            if tokens_in is None and usage is not None:
                tokens_in = getattr(usage, "prompt_tokens", None)
                tokens_out = getattr(usage, "completion_tokens", None)
            hidden = getattr(response_obj, "_hidden_params", None) or {}
            headers = hidden.get("additional_headers") or (slo.get("hidden_params") or {}).get("additional_headers") or {}
            latency = _seconds(start_time, end_time)
            metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
            app = app_of(kwargs)
            try:
                cost_usd, paid = cost_mod.call_cost(info, tokens_in, tokens_out, slo, kwargs)
            except Exception as exc:
                self._fault("cost", exc)
                cost_usd, paid = None, ration.is_paid(info)
            if note is not None and note.cache == "miss" and note.cache_key and note.cache_privacy:
                # Stored before the ledger row, so "the call is recorded" also means "a repeat can hit".
                text = cache_mod.answer_text(response_obj)
                if text is not None:
                    try:
                        await self._run(self.cache.put, note.cache_key, note.cache_privacy, text, now)
                    except Exception as exc:
                        self._fault("cache_put", exc)
            await self._run(
                self.ledger.record_call,
                provider=provider,
                deployment_id=str(info.get("id", "")),
                upstream=upstream,
                alias=slo.get("model_group") or metadata.get("model_group"),
                ok=True,
                status=200,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=int(latency * 1000) if latency is not None else None,
                privacy=self._privacy_for_record(kwargs.get("litellm_params") or kwargs),
                now=now,
                app=app.name,
                priority=app.priority,
                cost_usd=cost_usd,
                paid=paid,
                release_hold=_call_id(kwargs),
            )
            limits = quota.parse_limit_headers(provider, headers, now)
            if limits:
                await self._run(self.ledger.record_limits, upstream, limits, now)
            # Only an expired cooldown is cleared. A success from a request sent
            # before a 429 must not reopen an upstream that is still blocked.
            await self._run(self.ledger.clear_block, upstream, only_expired_cooldown=True, now=now)
        except Exception as exc:
            self._fault("success_event", exc)

    async def _record_cached(self, kwargs: Mapping[str, Any]) -> None:
        """A call answered from the cache: zero cost, zero tokens, marked cached (never counts toward quotas or caps)."""
        slo = kwargs.get("standard_logging_object") or {}
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        app = app_of(kwargs)
        await self._run(
            self.ledger.record_call,
            provider=CACHE_PROVIDER, deployment_id=CACHE_PROVIDER, upstream=CACHE_PROVIDER,
            alias=slo.get("model_group") or metadata.get("model_group"), ok=True, status=200, tokens_in=0, tokens_out=0,
            latency_ms=0, privacy=self._privacy_for_record(kwargs.get("litellm_params") or kwargs), now=self.clock(),
            app=app.name, priority=app.priority, cost_usd=0.0, paid=False, release_hold=_call_id(kwargs), cached=True,
        )

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        try:
            out: dict[str, str] = {}
            try:
                out["x-muse-privacy"] = self.privacy_of(data)
            except Exception:
                pass
            if _auth_policy(data) is not None:
                out["x-muse-app"] = app_of(data).name
            note = self._note(_call_id(data), create=False)
            metadata = _metadata(data)
            if metadata.get("muse_task"):
                out["x-muse-task"] = str(metadata["muse_task"])
            if metadata.get("muse_handoff") or (note is not None and note.handoff):
                out["x-muse-handoff"] = "suggest"
            if note is not None and note.cache in ("hit", "miss"):
                out["x-muse-cache"] = note.cache
            if response is not None and note is not None and note.cache == "hit":
                out["x-muse-provider"] = CACHE_PROVIDER
                out["x-muse-cost-usd"] = f"{0.0:.6f}"
                if note.codes:
                    out["x-muse-ration"] = ",".join(sorted(note.codes))
            elif response is not None:
                call_info = litellm_call_info or {}
                info = call_info.get("model_info") or _model_info(data)
                answered = call_info.get("model_id") or (getattr(response, "_hidden_params", None) or {}).get("model_id")
                if note is not None and answered and str(answered) in note.sent:
                    info = note.sent[str(answered)]
                if info.get("provider"):
                    out["x-muse-provider"] = str(info["provider"])
                if info.get("id"):
                    out["x-muse-deployment"] = str(info["id"])
                cost = self._response_cost(info, response)
                if cost is not None:
                    out["x-muse-cost-usd"] = f"{cost:.6f}"
                if note is not None and note.codes:
                    out["x-muse-ration"] = ",".join(sorted(note.codes))
                # The floor of the deployment that answered, not of one tried before it.
                if note is not None and info.get("id") and str(info["id"]) in note.min_output:
                    out[MIN_OUTPUT_HEADER] = note.min_output[str(info["id"])]
                if note is not None and info.get("id") and str(info["id"]) in note.reasoning:
                    out[REASONING_HEADER] = note.reasoning[str(info["id"])]
                if empty_reasoning(response):
                    out[ANSWER_HEADER] = ANSWER_EMPTY_REASONING
            elif note is not None and note.provider_limit_last and note.provider_until is not None:
                # The request ended on a provider's limit (see _failed). Checked before the rationing
                # notes: an earlier group may have been rationed, but the last word was the provider's.
                out[WAIT_HEADER] = WAIT_PROVIDER_LIMIT
                out["retry-after"] = str(max(1, int(math.ceil(note.provider_until - self.clock()))))
            elif note is not None and note.decisions:
                now = self.clock()
                defers = [d for d in note.decisions if d.kind == ration.DEFER and d.retry_at is not None]
                chosen = min(defers, key=lambda d: d.retry_at or 0) if defers else note.decisions[-1]
                out["x-muse-ration"] = f"{chosen.kind}:{chosen.code}"
                retry = chosen.retry_after(now)
                if retry is not None:
                    out["retry-after"] = str(retry)
            return out or None
        except Exception as exc:
            self._fault("response_headers", exc)
            return None


    @staticmethod
    def _response_cost(info: Mapping[str, Any], response: Any) -> float | None:
        """Cost for the x-muse-cost-usd header, from the response's own usage (same rule as the ledger)."""
        if not info:
            return None
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", None) if usage is not None else None
        tokens_out = getattr(usage, "completion_tokens", None) if usage is not None else None
        hidden = getattr(response, "_hidden_params", None) or {}
        slo = {"response_cost": hidden.get("response_cost")} if isinstance(hidden, Mapping) else None
        cost, _ = cost_mod.call_cost(info, tokens_in, tokens_out, slo)
        return cost


handler = MuseHook()
