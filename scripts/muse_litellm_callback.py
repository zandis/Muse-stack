"""Thin LiteLLM callback: quota-exhausted state and cross-alias cooldown.

This file is loaded by the LiteLLM 1.108.5 proxy. It has **no** dependency
on the ``muse`` package so it can be imported inside the official
``ghcr.io/berriai/litellm:v1.108.5`` image.

What LiteLLM already does (and this callback does not redo)
------------------------------------------------------------
In LiteLLM 1.108.5 (tag ``v1.108.5``, commit ``2a61f2114d``),
``Router.deployment_callback_on_failure`` (``litellm/router.py``) picks a
cooldown with this priority:

1. The deployment's own ``cooldown_time`` (``model_info`` or
   ``litellm_params``), if set and ``>= 0``.
2. The provider's ``Retry-After`` header, if present and ``>= 0``
   (``litellm.utils._get_retry_after_from_exception_header``).
3. The router's ``cooldown_time`` (``router_settings.cooldown_time``),
   which Muse Stack sets to 30 seconds, as the *fallback* when there is
   no header.

Setting ``cooldown_time: 30`` therefore does **not** disable Retry-After.
The header wins over the router default. The same priority is applied in
``litellm/router_utils/fallback_event_handlers.py``.

Caveats this callback *does* address
------------------------------------
* Muse Stack renders one LiteLLM deployment per alias (``{pin_id}__{alias}``).
  LiteLLM cooldowns are keyed by that deployment id, so a 429 on
  ``groq-gpt-oss-20b__fast`` does not automatically cool
  ``groq-gpt-oss-20b__smart``. This callback copies the cooldown to every
  sibling deployment that shares the same upstream ``model`` string.
* A *quota-exhausted* 429 (daily/weekly cap, ``insufficient_quota``, …)
  is given a long cooldown (default 3600s, or the header if it is
  longer) on every sibling, even if LiteLLM's ``allowed_fails`` has not
  yet tripped the primary cooldown.

Failures in this callback are swallowed: it must never break the proxy.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping

log = logging.getLogger("muse.litellm_callback")

# Duplicated on purpose from muse.gateway.quota so this file is standalone.
_EXHAUSTED = re.compile(
    r"(insufficient[_\s-]?quota|quota[_ ]?(exceeded|exhausted|used up)|"
    r"exceeded[_\s]?(your )?quotas?|resource[_ ]?exhaust(?:ed|ion)|"
    r"daily[_ ]?(limit|quota|cap)|weekly[_ ]?(limit|quota|cap)|"
    r"monthly[_ ]?(limit|quota|cap)|"
    r"you[ ]+have[ ]+exceeded|"
    r"tokens?[ ]+per[ ]+(day|week|month)|"
    r"free[ ]+tier[ ]+(limit|exhausted|exceeded)|"
    r"credit[s]?[ ]+(exhausted|exceeded|used up)|"
    r"billing[_\s]?hard[_\s]?limit)",
    re.IGNORECASE,
)

QUOTA_COOLDOWN_SECONDS = float(os.environ.get("MUSE_QUOTA_COOLDOWN_SECONDS", "3600"))


def is_quota_exhausted_text(text: str | None) -> bool:
    if not text:
        return False
    return bool(_EXHAUSTED.search(text))


def sibling_ids(model_list: list[Mapping[str, Any]] | None, deployment_id: str) -> list[str]:
    """Return other rendered deployment ids that share this one's upstream model."""
    if not model_list:
        return []
    upstream = None
    for entry in model_list:
        info = entry.get("model_info") or {}
        params = entry.get("litellm_params") or {}
        eid = info.get("id") or entry.get("id")
        if eid == deployment_id:
            upstream = params.get("model")
            break
    if not upstream:
        return []
    out: list[str] = []
    for entry in model_list:
        info = entry.get("model_info") or {}
        params = entry.get("litellm_params") or {}
        eid = info.get("id") or entry.get("id")
        if eid and eid != deployment_id and params.get("model") == upstream:
            out.append(str(eid))
    return out


def _exception_text(exception: Any) -> str:
    parts = [str(exception) if exception is not None else ""]
    body = getattr(exception, "body", None) or getattr(exception, "message", None)
    if body is not None:
        parts.append(str(body))
    return " ".join(parts)


def _retry_after(exception: Any) -> float | None:
    try:
        import litellm  # type: ignore
        from litellm.litellm_core_utils.exception_mapping_utils import (  # type: ignore
            _get_response_headers,
        )

        headers = _get_response_headers(original_exception=exception)
        if headers is None:
            return None
        value = litellm.utils._get_retry_after_from_exception_header(response_headers=headers)
        if value is not None and value >= 0:
            return float(value)
    except Exception:  # noqa: BLE001
        return None
    return None


def _status(exception: Any) -> int:
    for attr in ("status_code", "status", "http_status"):
        raw = getattr(exception, attr, None)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return 0


class MuseQuotaCallback:
    """LiteLLM CustomLogger-compatible callback.

    Subclasses CustomLogger when litellm is importable; otherwise is a
    plain object so unit tests can import this file without LiteLLM.
    """

    def __init__(self) -> None:
        self._quota_cooldown = QUOTA_COOLDOWN_SECONDS

    def _handle_failure(self, kwargs: Mapping[str, Any] | None, exception: Any) -> None:
        try:
            self._handle_failure_inner(kwargs or {}, exception)
        except Exception as exc:  # noqa: BLE001
            log.debug("muse callback ignored a failure: %s", exc)

    def _handle_failure_inner(self, kwargs: Mapping[str, Any], exception: Any) -> None:
        status = _status(exception)
        text = _exception_text(exception)
        exhausted = is_quota_exhausted_text(text)
        # Cross-alias copy is useful for any 429, not only quota exhaustion:
        # LiteLLM has already cooled *this* deployment (with Retry-After);
        # we only copy that decision to siblings.
        if status not in {429, 503} and not exhausted:
            return
        try:
            from litellm.proxy.proxy_server import llm_router  # type: ignore
            from litellm.router_utils.cooldown_handlers import (  # type: ignore
                set_cooldown_deployments,
            )
        except Exception:
            return
        if llm_router is None:
            return
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        deployment_id = None
        if isinstance(metadata, dict) and isinstance(metadata.get("model_info"), dict):
            deployment_id = metadata["model_info"].get("id")
        if not deployment_id and isinstance(kwargs.get("model_info"), dict):
            deployment_id = kwargs["model_info"].get("id")
        if not deployment_id:
            params = kwargs.get("litellm_params") or {}
            info = params.get("model_info") if isinstance(params, dict) else None
            if isinstance(info, dict):
                deployment_id = info.get("id")
        if not deployment_id:
            return
        model_list = getattr(llm_router, "model_list", None) or []
        siblings = sibling_ids(model_list, str(deployment_id))
        if not siblings:
            return
        header = _retry_after(exception)
        if exhausted:
            time_to_cooldown = max(header or 0.0, self._quota_cooldown)
        else:
            # Honour the same Retry-After LiteLLM used, else the router default.
            time_to_cooldown = header if header is not None else getattr(
                llm_router, "cooldown_time", 30
            )
        for sib in siblings:
            try:
                set_cooldown_deployments(
                    litellm_router_instance=llm_router,
                    original_exception=exception,
                    exception_status=status or 429,
                    deployment=sib,
                    time_to_cooldown=time_to_cooldown,
                    request_kwargs=kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("could not cool sibling %s: %s", sib, exc)

    # CustomLogger hooks (sync + async). Names must match LiteLLM's API.
    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ARG002
        self._handle_failure(kwargs, kwargs.get("exception") if isinstance(kwargs, dict) else None)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ARG002
        exception = None
        if isinstance(kwargs, dict):
            exception = kwargs.get("exception") or kwargs.get("original_exception")
        if exception is None:
            exception = response_obj
        self._handle_failure(kwargs, exception)

    def log_event(self, kwargs, response_obj, start_time, end_time, level=None):  # noqa: ARG002
        if str(level).lower() in {"error", "failure", "failed"}:
            self._handle_failure(kwargs, response_obj)


# LiteLLM loads "muse_litellm_callback.proxy_handler_instance".
try:
    from litellm.integrations.custom_logger import CustomLogger  # type: ignore

    class _Handler(CustomLogger, MuseQuotaCallback):  # type: ignore[misc]
        def __init__(self) -> None:
            CustomLogger.__init__(self)
            MuseQuotaCallback.__init__(self)

    proxy_handler_instance = _Handler()
except Exception:  # noqa: BLE001 - LiteLLM is absent in unit tests
    proxy_handler_instance = MuseQuotaCallback()
