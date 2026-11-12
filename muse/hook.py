"""LiteLLM callback that shares quota blocks across aliases and enforces privacy.

Glue only: rules live in muse.quota (what a failure means), muse.ledger (shared
state) and muse.policy (which deployments a privacy class may use).

Loaded by the proxy through `litellm_settings.callbacks: ["muse.hook.handler"]`
(LiteLLM 1.108.5 `proxy/types_utils/utils.py:get_instance_fn` imports
`muse.hook` and reads `handler`; PYTHONPATH must contain the repo root, which
is /opt/muse in the container).

Hooks used, with the 1.108.5 signatures (litellm/integrations/custom_logger.py):
- async_pre_call_hook: validate x-muse-privacy (unknown -> 400), resolve the class.
- async_filter_deployments: drop blocked upstreams, then apply the privacy class.
  Runs for every model group the router tries, fallbacks included.
- async_pre_call_deployment_hook: second privacy check on the chosen deployment;
  a request that names a deployment id skips async_filter_deployments.
- async_post_call_failure_deployment_hook: one call per failed attempt (retries
  and fallbacks too), unlike async_log_failure_event which fires once per request.
- async_log_success_event: record the call and observed limits.
- async_post_call_response_headers_hook: x-muse-provider / -deployment / -privacy.

Failure policy: any error in quota bookkeeping is caught, counted and ignored
(LiteLLM's default routing continues). Privacy enforcement fails closed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Mapping

from fastapi import HTTPException
from litellm.exceptions import GuardrailRaisedException, RateLimitError
from litellm.integrations.custom_logger import CustomLogger

from muse import quota
from muse.ledger import Ledger

log = logging.getLogger("muse.hook")

PRIVACY_HEADER = "x-muse-privacy"
TRUTHY = {"1", "true", "on", "yes"}
MARK = "[muse]"


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

    def _default_class(self) -> str:
        from muse import policy

        raw = self.env.get("MUSE_DEFAULT_PRIVACY", "").strip() or "standard"
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
        return policy.normalise_class(header, self._default_class())

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

    # ------------------------------------------------------------ proxy pre-call

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        from muse import policy

        try:
            requested = _headers(data).get(PRIVACY_HEADER)
            default = self._default_class()
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
                f"({len(healthy_deployments)} available, none allowed: "
                + ("only local models qualify" if privacy == "local" else
                   "it needs a local model or a provider that does not train on inputs or allow human review")
                + "). Not falling back to other providers."
            )

        # Then quota blocks. Errors here degrade to LiteLLM's own behaviour.
        try:
            blocks = await self._run(self.ledger.active_blocks, self.clock())
        except Exception as exc:
            self._fault("active_blocks", exc)
            return allowed
        if not blocks:
            return allowed
        kept: list[dict] = []
        hit: list[dict[str, Any]] = []
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
        if kept:
            return kept
        soonest = min(hit, key=lambda b: b["until"])
        raise MuseBlockedError(
            f"every '{privacy}'-allowed deployment of '{model}' is blocked by a provider quota; "
            f"soonest reset {quota.iso(soonest['until'])} ({soonest['kind']}: {soonest['reason']})",
            model=model,
        )

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
        return None

    # ------------------------------------------------------------ outcomes

    async def async_post_call_failure_deployment_hook(
        self,
        request_data: Mapping[str, Any],
        exception: Exception,
        call_type: Any,
        fallback_depth: int | None = None,
    ) -> None:
        try:
            if isinstance(exception, (MusePrivacyError, MuseBlockedError)) or MARK in str(getattr(exception, "message", "")):
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
            if response is not None:
                info = (litellm_call_info or {}).get("model_info") or _model_info(data)
                if info.get("provider"):
                    out["x-muse-provider"] = str(info["provider"])
                if info.get("id"):
                    out["x-muse-deployment"] = str(info["id"])
            return out or None
        except Exception as exc:
            self._fault("response_headers", exc)
            return None


handler = MuseHook()
