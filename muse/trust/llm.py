"""`GatewayLLM`: the health bot's only way to reach a model, through the local muse proxy.

Why so strict: the proxy's hook is what keeps health content on local (or
contractually private) models. If the hook is not loaded, the proxy routes to
whatever it likes and says nothing. So by default this client refuses to
return text from a response that does not prove the class was enforced:
`x-muse-privacy` must be present and at least as strict as requested, and
`x-muse-provider` must be present. The text of such a response is discarded.

Other choices:
- The bearer is read from the environment at call time, never stored on the
  object, never logged, and scrubbed from any error text.
- No environment HTTP proxy and no redirects: the request goes to the
  configured gateway URL or nowhere.
- Request and response sizes are capped.
- Probe first: checking headers on the answer only DETECTS an unenforced
  route after the content has already gone wherever the proxy sent it (the
  e2e test shows a plain proxy sending it to a training cloud provider). So in
  strict mode a content-free "ping" with the same class and alias goes first,
  and record content is sent only if the ping comes back with proof of
  enforcement. Cached per (class, alias) for `probe_ttl` seconds; a hook
  switched off inside that window is still caught by the response check, but
  only after the fact.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from muse import policy
from muse.trust.contracts import LLMRefused, LLMResult, LLMUnavailable

DEFAULT_URL = "http://127.0.0.1:4000"
MARK = "[muse]"


class UnverifiedRoute(LLMRefused):
    """The gateway answered but did not prove the privacy class was enforced.

    `content_sent` is True: the request DID leave this process and may have
    reached a provider the class does not allow. The caller must record that.
    """

    content_sent = True


class RequestTooLarge(LLMUnavailable):
    """The prompt exceeds the client's cap; nothing was sent."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # a redirect becomes an HTTPError; never re-send content elsewhere


def _rank(cls: str) -> int:
    return policy.PRIVACY_CLASSES.index(policy.normalise_class(cls))


class GatewayLLM:
    """OpenAI-compatible client for the muse proxy. Implements `contracts.LLM`."""

    def __init__(
        self,
        base_url: str | None = None,
        key_env: str = "LITELLM_MASTER_KEY",
        *,
        strict_route: bool = True,
        timeout: float = 120.0,
        max_request_bytes: int = 200_000,
        max_response_bytes: int = 1_000_000,
        environ: Mapping[str, str] | None = None,
        probe_ttl: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._environ = environ
        env = self._env
        self.base_url = (base_url or env.get("MUSE_PROXY_URL", "").strip() or DEFAULT_URL).rstrip("/")
        self.key_env = key_env
        self.strict_route = strict_route
        self.timeout = timeout
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.probe_ttl = probe_ttl
        self._clock = clock
        self._verified: dict[tuple[str, str], float] = {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:  # names the variable, never its value
        return f"GatewayLLM(base_url={self.base_url!r}, key_env={self.key_env!r}, strict_route={self.strict_route})"

    @property
    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _key(self) -> str:
        return (self._env.get(self.key_env, "") or "").strip() if self.key_env else ""

    def _scrub(self, text: str) -> str:
        key = self._key()
        if key and len(key) >= 4:
            text = text.replace(key, "***")
        text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_\-]{8,}", "sk-***", text)
        return text[:300]

    # ------------------------------------------------------------------ call

    def complete(self, messages: list[dict[str, str]], *, privacy: str, alias: str = "smart", max_tokens: int = 800) -> LLMResult:
        requested = policy.normalise_class(privacy)  # PolicyError (ValueError) on a typo: never default
        body = json.dumps(
            {"model": alias, "messages": messages, "max_tokens": int(max_tokens), "stream": False},
            ensure_ascii=False,
        ).encode("utf-8")
        if len(body) > self.max_request_bytes:
            raise RequestTooLarge(f"prompt is {len(body)} bytes, over the {self.max_request_bytes}-byte cap; nothing was sent")
        if self.strict_route and self.probe_ttl > 0:
            self._probe(requested, alias)
        return self._parse(*self._post(body, requested), requested)

    def _probe(self, requested: str, alias: str) -> None:
        """Content-free request; raises before any record text is sent if the route is unproven."""
        key = (requested, alias)
        now = self._clock()
        if now - self._verified.get(key, float("-inf")) < self.probe_ttl:
            return
        body = json.dumps({"model": alias, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}).encode()
        try:
            self._parse(*self._post(body, requested), requested)
        except UnverifiedRoute as exc:
            err = UnverifiedRoute(f"{exc} (probe; no content was sent)")
            err.content_sent = False
            raise err from None
        except (LLMRefused, LLMUnavailable) as exc:
            exc.content_sent = False  # type: ignore[attr-defined]
            raise
        self._verified[key] = now

    def _post(self, body: bytes, requested: str) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "x-muse-privacy": requested}
        key = self._key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(f"{self.base_url}/v1/chat/completions", data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as err:
            try:
                raw_err = err.read(65_536)
            except Exception:
                raw_err = b""
            err_headers = {k.lower(): v for k, v in (err.headers or {}).items()}
            raise self._map_error(err.code, err_headers, raw_err) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timed out" if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason) else "is unreachable"
            raise LLMUnavailable(f"the model gateway at {self.base_url} {kind} ({type(reason).__name__})") from None
        if len(raw) > self.max_response_bytes:
            raise LLMUnavailable(f"the gateway's answer exceeded the {self.max_response_bytes}-byte cap; discarded")
        return status, resp_headers, raw

    def _parse(self, status: int, headers: dict[str, str], raw: bytes, requested: str) -> LLMResult:
        provider = headers.get("x-muse-provider") or None
        deployment = headers.get("x-muse-deployment") or None
        got = headers.get("x-muse-privacy") or None
        if self.strict_route:
            if not got or not provider:
                self._verified.clear()
                raise UnverifiedRoute(
                    "the gateway did not confirm which model answered or which privacy class it enforced "
                    "(is the muse hook loaded?); the answer was discarded"
                )
            try:
                weaker = _rank(got) < _rank(requested)
            except policy.PolicyError:
                weaker = True
            if weaker:
                self._verified.clear()
                raise UnverifiedRoute(f"the gateway enforced '{self._scrub(got)}', weaker than the requested '{requested}'; the answer was discarded")
        try:
            payload = json.loads(raw.decode("utf-8"))
            message = payload["choices"][0]["message"]
            text = message.get("content")
            if not isinstance(text, str):
                raise TypeError("content is not text")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeDecodeError):
            raise LLMUnavailable(f"the gateway answered HTTP {status} but not with a chat completion") from None
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        return LLMResult(
            text=text,
            provider=provider,
            deployment=deployment,
            privacy=got,
            tokens_in=tin if isinstance(tin, int) else None,
            tokens_out=tout if isinstance(tout, int) else None,
        )

    def _map_error(self, status: int, headers: dict[str, str], raw: bytes) -> Exception:
        text = raw.decode("utf-8", "replace")
        message = text
        try:
            payload: Any = json.loads(text)
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            message = str(err.get("message", err) if isinstance(err, dict) else err)
        except ValueError:
            pass
        message = self._scrub(message)
        if status in (400, 403) and MARK in message:
            return LLMRefused(message)
        if status == 401:
            return LLMUnavailable(
                f"the gateway rejected the credential (HTTP 401); check the key in ${self.key_env}"
                if self.key_env else "the gateway requires a credential (HTTP 401)"
            )
        if status == 403:
            return LLMUnavailable("the gateway refused access (HTTP 403, not a privacy decision); check the credential")
        if status == 429 or status >= 500:
            hint = self._retry_hint(headers, message)
            return LLMUnavailable(f"the model is busy or down (HTTP {status})" + (f"; {hint}" if hint else ""))
        return LLMUnavailable(f"the gateway returned HTTP {status}")

    @staticmethod
    def _retry_hint(headers: Mapping[str, str], message: str) -> str:
        m = re.search(r"soonest reset (\S+)", message)
        if m:
            return f"soonest retry {m.group(1).rstrip(';,).')}"
        after = headers.get("retry-after", "").strip()
        if after and re.fullmatch(r"\d{1,6}", after):
            return f"retry after {after}s"
        return ""
