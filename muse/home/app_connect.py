"""One-tap connection (OAuth PKCE) driven through the home API.

`muse connect openrouter` already does this at a terminal (muse/connect.py): it
makes a PKCE pair, listens on 127.0.0.1 for the browser's redirect, trades the
one-time code for a key over verified https and stores the key. This module runs
the same pieces for an app:

    POST /v1/connect/{provider}/start   -> {state_id, mode, url, expires_at}
    GET  /v1/connect/flows/{state_id}   -> pending | connected | failed
    POST /v1/connect/flows/{state_id}/code   (paste_code mode only)
    DELETE /v1/connect/flows/{state_id}

Two modes.
  loopback    the browser is on the same computer as the home (the desktop app).
              The home starts the same one-shot listener `muse connect` uses and
              the provider sends the browser back to it.
  paste_code  the browser is somewhere else (a phone, a remote client). A redirect
              to `localhost` would reach the phone, not the home, so the address
              carries no callback: the provider shows a code, the person pastes it
              into the app, and the app sends it here. This is the documented
              headless variant (`muse connect openrouter --paste-code`).

What is kept: the PKCE verifier and the listener live only in this process, in the
flow, until it ends. The key is passed straight to the home's key store and never
returned, logged or kept here. At most MAX_FLOWS flows are open at once; a flow
ends after TIMEOUT_S, and finished flows are forgotten after KEEP_DONE_S.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("muse.home")

TIMEOUT_S = 300.0
KEEP_DONE_S = 600.0
MAX_FLOWS = 4
MODES = ("loopback", "paste_code")
# Why a flow failed, as fixed words the app has sentences for.
FAIL_REASONS = ("timeout", "denied", "bad_callback", "exchange_failed", "store_failed", "cancelled", "bad_code")


class FlowError(Exception):
    """A flow could not be started or continued. `code` is an API error code; `message` a fixed sentence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code, self.message = code, message


@dataclass(slots=True)
class Flow:
    id: str
    provider: str
    mode: str
    url: str
    created_at: float
    expires_at: float
    status: str = "pending"  # pending | connected | failed
    reason: str | None = None
    finished_at: float | None = None
    verifier: str = field(default="", repr=False)
    receiver: Any = field(default=None, repr=False)
    endpoints: Any = field(default=None, repr=False)
    cancelled: bool = False

    def public(self) -> dict[str, Any]:
        from muse.home.app_ask import iso

        return {"state_id": self.id, "provider": self.provider, "mode": self.mode, "status": self.status,
                "reason": self.reason, "expires_at": iso(self.expires_at)}


class Flows:
    """The open one-tap flows of one home. `on_key(provider, key)` stores the key and returns nothing."""

    def __init__(self, on_key: Callable[[Any, str], None], clock: Callable[[], float] = time.time,
                 endpoints_for: Callable[[Any], Any] | None = None) -> None:
        self.on_key = on_key
        self.clock = clock
        self.endpoints_for = endpoints_for
        self._lock = threading.Lock()
        self._flows: dict[str, Flow] = {}

    def _prune(self) -> None:
        now = self.clock()
        for flow_id, flow in list(self._flows.items()):
            if flow.status == "pending" and now >= flow.expires_at:
                self._end(flow, "failed", "timeout")
            if flow.status != "pending" and flow.finished_at is not None and now - flow.finished_at > KEEP_DONE_S:
                del self._flows[flow_id]

    def _end(self, flow: Flow, status: str, reason: str | None = None) -> None:
        flow.status, flow.reason, flow.finished_at = status, reason, self.clock()
        flow.verifier = ""
        flow.receiver = None  # the listener thread holds its own reference and closes it

    def start(self, provider: Any, mode: str) -> Flow:
        from muse import connect

        conn = provider.connection
        if conn is None or conn.method != "oauth_pkce" or not conn.auth_url or not conn.exchange_url or not provider.env:
            raise FlowError("invalid_request", "That service is not connected in one tap. Use the guided steps and paste its key.")
        if mode not in MODES:
            raise FlowError("invalid_request", "mode must be loopback or paste_code.")
        endpoints = self.endpoints_for(provider) if self.endpoints_for else connect.Endpoints(conn.auth_url, conn.exchange_url)
        try:
            endpoints.check()
        except connect.ConnectError:
            raise FlowError("unavailable", "The service's sign-in address is not https, so Muse will not use it.") from None
        with self._lock:
            self._prune()
            if sum(1 for f in self._flows.values() if f.status == "pending") >= MAX_FLOWS:
                raise FlowError("conflict", "Too many connections are being made at once. Finish or cancel one, then try again.")
            verifier, challenge = connect.pkce_pair()
            now = self.clock()
            receiver = None
            if mode == "loopback":
                try:
                    receiver = connect._Receiver()  # noqa: SLF001 - the same one-shot listener `muse connect` uses
                except OSError:
                    raise FlowError("unavailable", "The home could not listen for the service's answer on this computer. "
                                                   "Try again, or paste the code instead.") from None
                url = connect.build_auth_url(endpoints.auth_url, challenge, callback_url=receiver.callback_url, state=receiver.state)
            else:
                url = connect.build_auth_url(endpoints.auth_url, challenge, callback_url=None, state=None)
            flow = Flow(secrets.token_hex(8), provider.id, mode, url, now, now + TIMEOUT_S, verifier=verifier, receiver=receiver,
                        endpoints=endpoints)
            self._flows[flow.id] = flow
        if receiver is not None:
            threading.Thread(target=self._listen, args=(flow, provider, receiver), name=f"connect-{flow.id}", daemon=True).start()
        return flow

    def _listen(self, flow: Flow, provider: Any, receiver: Any) -> None:
        try:
            kind, code = receiver.wait(TIMEOUT_S, time.monotonic, None)
        except Exception as exc:
            log.warning("connect: the listener stopped (%s)", type(exc).__name__)
            kind, code = "bad", None
        finally:
            receiver.server_close()  # stop listening the moment there is an answer or the time is up
        with self._lock:
            if flow.status != "pending":
                return
            if kind != "code" or not code:
                reason = {"timeout": "timeout", "denied": "denied"}.get(kind, "bad_callback")
                self._end(flow, "failed", "cancelled" if flow.cancelled else reason)
                return
        self._finish(flow, provider, code)

    def _finish(self, flow: Flow, provider: Any, code: str) -> None:
        """Trade the code for the key and store it. The key never leaves this function."""
        from muse import connect

        try:
            key = connect.exchange_code(flow.endpoints, code, flow.verifier)
        except connect.ConnectError:
            with self._lock:
                self._end(flow, "failed", "exchange_failed")
            return
        try:
            self.on_key(provider, key)
        except Exception as exc:
            log.warning("connect: the key could not be stored (%s)", type(exc).__name__)
            with self._lock:
                self._end(flow, "failed", "store_failed")
            return
        finally:
            key = ""
        with self._lock:
            self._end(flow, "connected")

    def get(self, flow_id: str) -> Flow | None:
        with self._lock:
            self._prune()
            return self._flows.get(flow_id)

    def give_code(self, flow_id: str, provider_of: Callable[[str], Any], code: str) -> Flow:
        from muse import connect

        flow = self.get(flow_id)
        if flow is None:
            raise FlowError("not_found", "There is no such connection in progress.")
        if flow.mode != "paste_code":
            raise FlowError("conflict", "This connection waits for the service to send the browser back; it takes no code.")
        if flow.status != "pending":
            raise FlowError("conflict", "This connection has already ended. Start again.")
        if not connect._CODE_RE.fullmatch(code):  # noqa: SLF001 - the shape the listener accepts too
            raise FlowError("invalid_request", "That does not look like the code the service showed. Copy it again.")
        self._finish(flow, provider_of(flow.provider), code)
        return flow

    def cancel(self, flow_id: str) -> bool:
        with self._lock:
            flow = self._flows.get(flow_id)
            if flow is None:
                return False
            if flow.status == "pending":
                flow.cancelled = True
                if flow.mode == "loopback" and flow.receiver is not None:
                    flow.receiver.outcome = ("denied", None)  # the listener's loop sees it within half a second and closes
                self._end(flow, "failed", "cancelled")
            return True

    def close(self) -> None:
        for flow_id in list(self._flows):
            self.cancel(flow_id)
