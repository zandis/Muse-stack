"""Proxy authentication: which application sent this request.

Wired by scripts/render_config.py as `general_settings.custom_auth:
muse.auth.user_api_key_auth`. In LiteLLM 1.108.5 (proxy/proxy_server.py,
load_config: `user_custom_auth = get_instance_fn(value=custom_auth)`) that
replaces the built-in key check for every route that depends on
`user_api_key_auth` (proxy/auth/user_api_key_auth.py,
user_api_key_auth_builder: `response = await user_custom_auth(request=request,
api_key=api_key)`, then `UserAPIKeyAuth.model_validate(response)` and an early
return). /health/liveliness and /health/readiness have no auth dependency and
are unaffected. With custom auth LiteLLM skips its `common_checks` unless
`custom_auth_run_common_checks` is set; the renderer sets it only when a
database is configured (virtual keys have budgets there).

Rules:
- master key (LITELLM_MASTER_KEY) -> built-in app `owner` (interactive, paid
  allowed), proxy-admin role, so /model/info, /v1/models and the admin UI keep
  working. `x-muse-app: <name>` selects a registered app's policy; an unknown
  name is a 400.
- an app token -> that app; `x-muse-app` naming another app is a 403. App
  tokens may call model endpoints only (no key, team or config management).
- no master key configured -> local-dev mode: an app token still selects its
  app; anything else is app `anonymous` (normal, no paid). No security
  boundary exists in this mode; render_and_start.py warns at startup.
- master key configured and the token is neither -> 401, unless a LiteLLM
  database is configured and the token is one of its virtual keys (admin UI
  keys): then LiteLLM's key object is used with the `owner` policy.

What reaches later stages: the returned UserAPIKeyAuth carries the app's
policy in `.metadata` (muse.apps.App.policy()). The proxy copies it into the
request as `metadata["user_api_key_metadata"]`
(proxy/litellm_pre_call_utils.py, add_litellm_data_to_request), after
stripping any caller-supplied `user_api_key_*` keys, so a request body cannot
forge its app. From there it is visible to async_pre_call_hook (also as the
`user_api_key_dict` argument), async_filter_deployments (`request_kwargs`),
async_pre_call_deployment_hook (`kwargs`), the failure deployment hook
(`request_data`) and async_log_success_event
(`kwargs["litellm_params"]["metadata"]` and
`standard_logging_object["metadata"]["user_api_key_auth_metadata"]`).

A token is never logged, stored or echoed; only its SHA-256 is compared.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from muse import apps as apps_mod
from muse.apps import ANONYMOUS, OWNER, App

log = logging.getLogger("muse.auth")

APP_HEADER = "x-muse-app"
MARK = "[muse]"


class AuthError(Exception):
    """A request that must be turned away. The message never contains the token."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class Identity:
    app: App
    via: str  # master | app | anonymous | virtual_key


def _strip_bearer(token: str | None) -> str:
    text = (token or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def authenticate(
    token: str | None,
    header_app: str | None,
    *,
    master_key: str | None,
    environ: Mapping[str, str] | None = None,
) -> Identity:
    """Pure decision (no LiteLLM import). Raises AuthError(status, message)."""
    token = _strip_bearer(token)
    wanted = (header_app or "").strip() or None
    master = master_key or None

    if master and token and hmac.compare_digest(token.encode("utf-8"), master.encode("utf-8")):
        if wanted is None or wanted == OWNER.name:
            return Identity(OWNER, "master")
        app = apps_mod.resolve(wanted, environ)
        if app is None:
            raise AuthError(400, f"{MARK} unknown app '{_safe(wanted)}' in {APP_HEADER}; register it with `muse apps add`.")
        return Identity(app, "master")

    app = apps_mod.find_by_token(token, environ) if token else None
    if app is not None:
        if wanted is not None and wanted != app.name:
            raise AuthError(403, f"{MARK} an app token cannot switch app with {APP_HEADER}; only the master key can.")
        return Identity(app, "app")

    if not master:
        return Identity(ANONYMOUS, "anonymous")
    if not token:
        raise AuthError(401, f"{MARK} missing API key. Send the master key or an app token from `muse apps add`.")
    raise AuthError(401, f"{MARK} unknown API key. Send the master key or an app token from `muse apps add`.")


def _safe(name: str) -> str:
    """Echo a header value only if it looks like an app name (never a pasted secret)."""
    return name if apps_mod.NAME_RE.fullmatch(name) else "?"


# ---------------------------------------------------------------- LiteLLM glue


def _master_key() -> str | None:
    """The proxy's resolved master key (config may name os.environ/...), else the env var."""
    try:
        from litellm.proxy import proxy_server

        value = getattr(proxy_server, "master_key", None)
        if isinstance(value, str) and value:
            return value
    except Exception:  # pragma: no cover - only outside the proxy
        pass
    value = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    return value or None


def _is_public(route: str) -> bool:
    from litellm.proxy._types import LiteLLMRoutes
    from litellm.proxy.auth.auth_utils import route_in_additonal_public_routes

    return route in LiteLLMRoutes.public_routes.value or bool(route_in_additonal_public_routes(current_route=route))


def _model_route(route: str) -> bool:
    from litellm.proxy._types import LiteLLMRoutes
    from litellm.proxy.auth.route_checks import RouteChecks

    return RouteChecks.is_llm_api_route(route) or route in LiteLLMRoutes.model_info_routes.value or route in ("/models", "/v1/models")


def _http(status: int, message: str) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=status, detail=message)


async def _virtual_key(token: str) -> Any | None:
    """A LiteLLM database virtual key (admin UI keys), when a database is configured."""
    try:
        from litellm.proxy import proxy_server
        from litellm.proxy._types import hash_token
        from litellm.proxy.auth.auth_checks import get_key_object
    except Exception:
        return None
    prisma = getattr(proxy_server, "prisma_client", None)
    if prisma is None:
        return None
    try:
        obj = await get_key_object(
            hashed_token=hash_token(token), prisma_client=prisma, user_api_key_cache=proxy_server.user_api_key_cache
        )
    except Exception:
        return None
    if obj is None or getattr(obj, "blocked", False):
        return None
    expires = getattr(obj, "expires", None)
    if expires is not None:
        from datetime import datetime, timezone

        when = expires if isinstance(expires, datetime) else None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < datetime.now(timezone.utc):
                return None
    return obj


async def user_api_key_auth(request: Any, api_key: str) -> Any:
    """LiteLLM custom auth entry point. Returns a UserAPIKeyAuth; raises HTTPException to refuse."""
    from litellm.constants import LITELLM_PROXY_MASTER_KEY_ALIAS
    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

    route = str(getattr(getattr(request, "url", None), "path", "") or "")
    if _is_public(route):
        return UserAPIKeyAuth(user_role=LitellmUserRoles.INTERNAL_USER_VIEW_ONLY)
    headers = getattr(request, "headers", {}) or {}
    master = _master_key()
    token = _strip_bearer(api_key)
    try:
        ident = authenticate(token, headers.get(APP_HEADER), master_key=master)
    except AuthError as exc:
        if exc.status == 401 and token:
            vk = await _virtual_key(token)
            if vk is not None:
                vk.metadata = {**(getattr(vk, "metadata", None) or {}), **OWNER.policy()}
                return vk
        raise _http(exc.status, exc.message) from None

    policy = ident.app.policy()
    if ident.via == "master":
        try:
            from litellm.proxy.proxy_server import litellm_proxy_admin_name
        except Exception:  # pragma: no cover
            litellm_proxy_admin_name = "default_user_id"
        return UserAPIKeyAuth(
            api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
            user_role=LitellmUserRoles.PROXY_ADMIN,
            user_id=litellm_proxy_admin_name,
            key_alias=f"muse-app:{ident.app.name}",
            metadata=policy,
        )
    if ident.via == "app" and route and not _model_route(route):
        raise _http(403, f"{MARK} app tokens may only call model endpoints; {route} needs the master key.")
    return UserAPIKeyAuth(
        api_key=f"muse-app:{ident.app.name}",
        user_role=LitellmUserRoles.INTERNAL_USER,
        user_id=f"muse-app:{ident.app.name}",
        key_alias=f"muse-app:{ident.app.name}",
        metadata=policy,
    )
