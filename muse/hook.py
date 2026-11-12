"""LiteLLM callback that shares quota blocks across aliases and enforces privacy.

Glue only: rules live in muse.quota (what a failure means), muse.ledger (shared
state) and muse.policy (which deployments a privacy class may use).

Loaded by the proxy through `litellm_settings.callbacks: ["muse.hook.handler"]`
(LiteLLM 1.108.5 `proxy/types_utils/utils.py:get_instance_fn` imports
`muse.hook` and reads `handler`; PYTHONPATH must contain the repo root, which
is /opt/muse in the container).

Hooks used, with the 1.108.5 signatures (litellm/integrations/custom_logger.py):
- async_pre_call_hook: validate x-muse-privacy (unknown -> 400), resolve the class
  (the app's default_privacy replaces MUSE_DEFAULT_PRIVACY when the header is absent).
- async_filter_deployments: apply the privacy class, drop blocked upstreams, then
  ration (muse/ration.py). Runs for every model group the router tries,
  fallbacks and order levels included, so a fallback can never reach something
  rationing refuses.
- async_pre_call_deployment_hook: second privacy check on the chosen deployment
  (a request that names a deployment id skips async_filter_deployments), rationing
  for such direct requests, and the atomic paid-cap hold (Ledger.try_hold_paid).
- async_post_call_failure_deployment_hook: one call per failed attempt (retries
  and fallbacks too), unlike async_log_failure_event which fires once per request.
- async_log_success_event: record the call (app, priority, cost, paid) and limits.
- async_post_call_response_headers_hook: x-muse-provider / -deployment / -privacy /
  -app, x-muse-cost-usd, x-muse-ration and Retry-After on a rationing refusal.

The application comes from muse/auth.py through `metadata.user_api_key_metadata`
(the proxy strips caller-supplied `user_api_key_*` keys, so it cannot be forged
from a request body). Every stage carries `litellm_call_id`; per-request
rationing notes are kept in memory under it for the response headers.

Failure policy: any error in quota bookkeeping or rationing is caught, counted
in `errors` and ignored (LiteLLM's default routing continues), except that a
failed rationing step still removes paid deployments: when in doubt, do not
spend. Privacy enforcement fails closed.
"""

from __future__ import annotations

import asyncio
import logging
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
from muse import cost as cost_mod
from muse import quota
from muse import ration
from muse.ledger import Ledger

log = logging.getLogger("muse.hook")

PRIVACY_HEADER = "x-muse-privacy"
TRUTHY = {"1", "true", "on", "yes"}
MARK = "[muse]"
NOTE_TTL_SECONDS = 600
NOTE_MAX = 4096
DEFAULT_OUTPUT_ESTIMATE = 1024  # tokens held for a paid call that sets no max_tokens


class MusePrivacyError(GuardrailRaisedException):
    """No deployment allowed for the request's privacy class.

    A GuardrailRaisedException, so the 1.108.5 router neither retries nor falls
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

    def __init__(self, message: str, model: str) -> None:
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
    """Rough (input, output) token estimate for a paid hold: 1 token per 3 characters
    (generous for English, about right for CJK), output = max_tokens or 1024."""
    chars = len(_request_text(data))
    out = 0
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out = value
            break
    return chars // 3 + 1, out or DEFAULT_OUTPUT_ESTIMATE


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
        return ration.Settings.from_env(self.env)

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
        if ration.reserve_fraction(app.priority, settings) > 0:
            pairs = [(_upstream(i), i) for i in infos]
            pairs = [(pair, i) for pair, i in pairs if pair is not None]
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
        return state

    async def _apply_ration(
        self,
        model: str,
        deployments: list[dict],
        data: Mapping[str, Any] | None,
        prior: list[ration.Removed],
        *,
        direct: bool,
    ) -> ration.Outcome:
        """Rules 1-6 for one group. Errors degrade to no rationing, minus paid deployments."""
        now = self.clock()
        app = app_of(data)
        try:
            settings = self.settings()
            state = await self._ration_state(app, deployments, settings, now)
            outcome = ration.ration(ration.AppPolicy.of(app), deployments, state, settings, now, direct=direct, prior=prior)
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
        if note is not None:
            note.codes.update(r.code for r in outcome.removed)
            note.checked.update(str((d.get("model_info") or {}).get("id", "")) for d in outcome.kept)
            if outcome.decision is not None:
                note.decisions.append(outcome.decision)
        if outcome.decision is not None:
            await self._record_ration(app, model, outcome.decision, now)
        return outcome

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
        app = app_of(data)
        metadata["muse_app"] = app.name  # informational; rules read user_api_key_metadata
        metadata["muse_priority"] = app.priority
        if findings:
            metadata["muse_privacy_findings"] = sorted({f.kind for f in findings})  # kinds only, never text
        return data

    # ------------------------------------------------------------ routing

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None = None,
        parent_otel_span: Any = None,
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
        if not allowed:
            raise MusePrivacyError(
                f"no deployment of '{model}' may receive a '{privacy}' request "
                f"({len(healthy_deployments)} not cooling right now, none allowed: "
                + ("only local models qualify" if privacy == "local" else
                   "it needs a local model or a provider that does not train on inputs or allow human review")
                + "). Not falling back to other providers."
            )

        # Then quota blocks. Errors here degrade to LiteLLM's own behaviour.
        kept: list[dict] = list(allowed)
        prior: list[ration.Removed] = []
        try:
            blocks = await self._run(self.ledger.active_blocks, self.clock())
        except Exception as exc:
            self._fault("active_blocks", exc)
            blocks = {}
        if blocks:
            kept, hit = [], []
            for deployment in allowed:
                info = deployment.get("model_info") or {}
                pair = _upstream(info)
                block = None
                if pair is not None:
                    block = blocks.get(pair[1]) or blocks.get(f"provider:{pair[0]}")
                if block is None:
                    kept.append(deployment)
                else:
                    hit.append(block)
                    prior.append(ration.Removed(str(info.get("id", "")), pair[1] if pair else "", ration.BLOCKED, ration.DEFER,
                                                float(block["until"]), f"blocked by a provider quota until {quota.iso(block['until'])}"))
            if not kept:
                soonest = min(hit, key=lambda b: b["until"])
                raise MuseBlockedError(
                    f"every '{privacy}'-allowed deployment of '{model}' is blocked by a provider quota; "
                    f"soonest reset {quota.iso(soonest['until'])} ({soonest['kind']}: {soonest['reason']})",
                    model=model,
                )

        # Then rationing between apps (muse/ration.py).
        direct = len(kept) == 1 and str((kept[0].get("model_info") or {}).get("id", "")) == model
        outcome = await self._apply_ration(model, kept, request_kwargs, prior, direct=direct)
        if outcome.kept:
            return outcome.kept
        decision = outcome.decision or ration.Decision(ration.REFUSE, "unknown", f"{MARK} rationing left nothing for '{model}'", None)
        if decision.code in (ration.APP_REQUESTS, ration.APP_TOKENS):
            raise MuseAppCapError(decision)
        raise MuseRationError(decision, model=model)

    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any) -> dict | None:
        """Last check on the deployment actually chosen (covers requests by deployment id)."""
        from muse import policy

        info = _model_info(kwargs)
        if not info:
            return None  # not a router call (e.g. a health probe); nothing to judge
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

        # Rationing for requests that named a deployment id (the filter did not run).
        call_id = _call_id(kwargs)
        note = self._note(call_id, create=False)
        dep_id = str(info.get("id", ""))
        if note is None or dep_id not in note.checked:
            outcome = await self._apply_ration(dep_id, [{"model_info": info}], kwargs, [], direct=True)
            if not outcome.kept:
                assert outcome.decision is not None
                raise MuseDirectRationError(outcome.decision)

        # The paid cap, atomically across workers, right before money is spent.
        if ration.is_paid(info):
            await self._hold_paid(info, kwargs, call_id)
        return None

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
                return
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
                await self._run(self.ledger.set_block, block.key(provider, upstream), block.until, block.kind, block.reason, now)
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

    def _privacy_for_record(self, data: Mapping[str, Any]) -> str:
        try:
            return self.privacy_of(data)
        except Exception:
            return "unknown"

    async def async_log_success_event(self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
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
            if response is not None:
                info = (litellm_call_info or {}).get("model_info") or _model_info(data)
                if info.get("provider"):
                    out["x-muse-provider"] = str(info["provider"])
                if info.get("id"):
                    out["x-muse-deployment"] = str(info["id"])
                cost = self._response_cost(info, response)
                if cost is not None:
                    out["x-muse-cost-usd"] = f"{cost:.6f}"
                if note is not None and note.codes:
                    out["x-muse-ration"] = ",".join(sorted(note.codes))
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
