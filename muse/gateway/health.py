"""In-memory per-provider health and usage counters.

The phase 0 service exposes these through ``GET /v1/providers/health`` and
``GET /v1/providers/usage``. Counters never include secret values. Token
counts are whatever the caller reported (LiteLLM usage, or 0).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitState
from muse.gateway.clock import Clock, SystemClock


@dataclass
class Usage:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    skipped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limited": self.rate_limited,
            "skipped": self.skipped,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "last_error": self.last_error,
        }


@dataclass
class HealthStore:
    breaker: CircuitBreaker
    clock: Clock = field(default_factory=SystemClock)
    _usage: dict[str, Usage] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _bucket(self, provider: str) -> Usage:
        if provider not in self._usage:
            self._usage[provider] = Usage()
        return self._usage[provider]

    def ensure(self, providers: list[str]) -> None:
        with self._lock:
            for name in providers:
                self._bucket(name)
                self.breaker.snapshot(name)

    def record(
        self,
        provider: str,
        *,
        status_code: int | None,
        outcome: str,
        retry_after: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str | None = None,
        touch_breaker: bool = True,
    ) -> None:
        with self._lock:
            usage = self._bucket(provider)
            usage.requests += 1
            usage.tokens_in += max(0, tokens_in)
            usage.tokens_out += max(0, tokens_out)
            if outcome == "success":
                usage.successes += 1
                usage.last_error = None
                if touch_breaker and status_code is not None:
                    self.breaker.on_response(provider, status_code, retry_after)
            elif outcome == "skipped":
                usage.skipped += 1
            else:
                usage.failures += 1
                if status_code in {429, 503}:
                    usage.rate_limited += 1
                usage.last_error = error
                if touch_breaker:
                    if status_code is not None:
                        self.breaker.on_response(provider, status_code, retry_after)
                    else:
                        self.breaker.on_transport_error(provider)

    def health(self) -> dict[str, Any]:
        now = self.clock.now()
        with self._lock:
            names = sorted(set(self._usage) | {s.provider for s in self.breaker.snapshots()})
            providers = []
            for name in names:
                snap = self.breaker.snapshot(name)
                usage = self._bucket(name)
                health = _health_label(snap.state)
                providers.append(
                    {
                        **snap.as_dict(now=now),
                        "health": health,
                        "usage": usage.as_dict(),
                    }
                )
            return {"generated_at": now, "providers": providers}

    def usage(self) -> dict[str, Any]:
        body = self.health()
        return {
            "generated_at": body["generated_at"],
            "providers": [
                {"provider": item["provider"], "usage": item["usage"], "state": item["state"]}
                for item in body["providers"]
            ],
        }

    def reset(self) -> None:
        with self._lock:
            self._usage.clear()
            self.breaker.reset()


def _health_label(state: CircuitState) -> str:
    if state == CircuitState.CLOSED:
        return "healthy"
    if state == CircuitState.HALF_OPEN:
        return "probing"
    return "unavailable"
