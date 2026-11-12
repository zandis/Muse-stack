"""A small façade: fallback chain + health counters in one place."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from muse.gateway.circuit_breaker import CircuitBreaker
from muse.gateway.clock import Clock, SystemClock
from muse.gateway.fallback import Attempt, CallFailed, FallbackChain, FallbackExhausted, FallbackResult
from muse.gateway.health import HealthStore

T = TypeVar("T")


@dataclass
class GatewayRouter:
    providers: list[str]
    default_cooldown_seconds: float = 30.0
    clock: Clock = field(default_factory=SystemClock)
    breaker: CircuitBreaker = field(init=False)
    health: HealthStore = field(init=False)

    def __post_init__(self) -> None:
        self.breaker = CircuitBreaker(
            default_cooldown_seconds=self.default_cooldown_seconds,
            clock=self.clock,
        )
        self.health = HealthStore(self.breaker, clock=self.clock)
        self.health.ensure(list(self.providers))

    def execute(self, call: Callable[[str], T], *, providers: Sequence[str] | None = None) -> FallbackResult[T]:
        chain = list(providers) if providers is not None else list(self.providers)
        fallback = FallbackChain(self.breaker, chain)
        try:
            result = fallback.execute(call)
        except FallbackExhausted as exc:
            self._record_attempts(exc.attempts)
            raise
        self._record_attempts(result.attempts)
        return result

    def _record_attempts(self, attempts: list[Attempt]) -> None:
        # The fallback chain already updated the breaker. Only count usage here.
        for attempt in attempts:
            self.health.record(
                attempt.provider,
                status_code=attempt.status_code,
                outcome=attempt.outcome,
                error=attempt.detail if attempt.outcome != "success" else None,
                touch_breaker=False,
            )

    def report_event(
        self,
        provider: str,
        *,
        status_code: int | None,
        outcome: str,
        retry_after: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        error: str | None = None,
    ) -> None:
        if provider not in self.providers:
            self.providers.append(provider)
        self.health.record(
            provider,
            status_code=status_code,
            outcome=outcome,
            retry_after=retry_after,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error=error,
        )


# Re-export so callers can raise a typed failure from their callables.
__all__ = ["GatewayRouter", "CallFailed", "FallbackExhausted", "FallbackResult"]
