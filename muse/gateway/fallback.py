"""Ordered fallback chains that skip open circuits and stop on first success.

A chain is a list of provider ids, tried in order. The circuit breaker decides
whether a provider is callable. The caller supplies a function
``call(provider) -> result``. If that function raises ``CallFailed`` (or a
subclass) the chain records the failure, trips the breaker when a status is
attached, and moves on. ``CircuitOpen`` is skipped without calling.

This sits *next to* LiteLLM's alias fallbacks, not in place of them. LiteLLM
already walks deployments inside ``fast`` / ``smart`` with a fixed cooldown.
Use this chain when Muse Stack itself is choosing a provider (useful-work
jobs, health probes, or a sidecar in front of the proxy) and needs to honour
Retry-After.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitOpen

T = TypeVar("T")


class CallFailed(Exception):
    """A provider was reached and failed. The chain should try the next one."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
        self.cause = cause
        super().__init__(message)


class FallbackExhausted(Exception):
    """Every provider in the chain was skipped or failed."""

    def __init__(self, chain: list[str], errors: list[str], attempts: list["Attempt"] | None = None) -> None:
        self.chain = list(chain)
        self.errors = list(errors)
        self.attempts = list(attempts or [])
        detail = "; ".join(errors) if errors else "no providers"
        super().__init__(f"fallback chain exhausted ({', '.join(chain)}): {detail}")


@dataclass
class Attempt:
    provider: str
    outcome: str  # "success" | "failed" | "skipped"
    detail: str = ""
    status_code: int | None = None


@dataclass
class FallbackResult(Generic[T]):
    value: T
    provider: str
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class FallbackChain:
    breaker: CircuitBreaker
    providers: list[str]

    def execute(self, call: Callable[[str], T]) -> FallbackResult[T]:
        if not self.providers:
            raise FallbackExhausted([], ["empty chain"], [])
        attempts: list[Attempt] = []
        errors: list[str] = []
        for provider in self.providers:
            try:
                self.breaker.allow(provider)
            except CircuitOpen as exc:
                attempts.append(
                    Attempt(provider, "skipped", detail=str(exc), status_code=429)
                )
                errors.append(str(exc))
                continue
            try:
                value = call(provider)
            except CallFailed as exc:
                status = exc.status_code if exc.status_code is not None else 599
                self.breaker.on_response(provider, status, exc.retry_after)
                attempts.append(
                    Attempt(provider, "failed", detail=str(exc), status_code=status)
                )
                errors.append(f"{provider}: {exc}")
                continue
            except CircuitOpen as exc:
                attempts.append(
                    Attempt(provider, "skipped", detail=str(exc), status_code=429)
                )
                errors.append(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - record and move on
                self.breaker.on_transport_error(provider)
                attempts.append(
                    Attempt(provider, "failed", detail=f"{type(exc).__name__}: {exc}")
                )
                errors.append(f"{provider}: {type(exc).__name__}: {exc}")
                continue
            self.breaker.on_response(provider, 200, None)
            attempts.append(Attempt(provider, "success", status_code=200))
            return FallbackResult(value=value, provider=provider, attempts=attempts)
        raise FallbackExhausted(self.providers, errors, attempts)
