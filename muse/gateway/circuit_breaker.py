"""Per-provider circuit breaker that honours Retry-After.

States:

* ``closed`` — traffic flows.
* ``open`` — the provider just rate-limited us (or failed the last probe);
  calls raise ``CircuitOpen`` until ``retry_at``.
* ``half_open`` — ``retry_at`` has passed; the next call is a probe. Success
  closes the circuit. Failure opens it again, using a new Retry-After if the
  provider sent one.

LiteLLM's own router uses a fixed ``cooldown_time`` after ``allowed_fails``.
This breaker is the layer that actually reads the provider's Retry-After, so a
60-second 429 stays dark for 60 seconds, and a 5-second 429 is not held for
the full default cooldown.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from muse.gateway.clock import Clock, SystemClock
from muse.gateway.retry_after import parse_retry_after


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """Raised when a call is refused because the circuit is open."""

    def __init__(self, provider: str, retry_at: float, retry_after: float) -> None:
        self.provider = provider
        self.retry_at = retry_at
        self.retry_after = retry_after
        super().__init__(
            f"circuit open for {provider}; retry after {retry_after:.1f}s"
        )


@dataclass
class CircuitSnapshot:
    provider: str
    state: CircuitState
    retry_at: float | None = None
    opened_at: float | None = None
    last_status: int | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0

    def as_dict(self, *, now: float) -> dict[str, Any]:
        retry_after = None
        if self.retry_at is not None and self.state != CircuitState.CLOSED:
            retry_after = max(0.0, self.retry_at - now)
        return {
            "provider": self.provider,
            "state": self.state.value,
            "retry_at": self.retry_at,
            "retry_after_seconds": retry_after,
            "opened_at": self.opened_at,
            "last_status": self.last_status,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
        }


@dataclass
class _Cell:
    state: CircuitState = CircuitState.CLOSED
    retry_at: float | None = None
    opened_at: float | None = None
    last_status: int | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    probe_in_flight: bool = False


@dataclass
class CircuitBreaker:
    """Thread-safe map of provider id → circuit.

    ``on_response`` is the only mutator a caller needs: pass the HTTP status
    and the raw Retry-After header (or ``None``). 429 and 503 open the
    circuit. 2xx close it. Other 5xx increment failures and open after
    ``open_after_fails`` consecutive errors, using the default cooldown
    unless a header is present.
    """

    default_cooldown_seconds: float = 30.0
    max_cooldown_seconds: float = 3600.0
    open_after_fails: int = 2
    clock: Clock = field(default_factory=SystemClock)
    _cells: dict[str, _Cell] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _cell(self, provider: str) -> _Cell:
        if provider not in self._cells:
            self._cells[provider] = _Cell()
        return self._cells[provider]

    def _sync(self, cell: _Cell, now: float) -> CircuitState:
        if cell.state == CircuitState.OPEN and cell.retry_at is not None and now >= cell.retry_at:
            cell.state = CircuitState.HALF_OPEN
            cell.probe_in_flight = False
        return cell.state

    def state(self, provider: str) -> CircuitState:
        with self._lock:
            now = self.clock.now()
            return self._sync(self._cell(provider), now)

    def snapshot(self, provider: str) -> CircuitSnapshot:
        with self._lock:
            now = self.clock.now()
            cell = self._cell(provider)
            self._sync(cell, now)
            return CircuitSnapshot(
                provider=provider,
                state=cell.state,
                retry_at=cell.retry_at,
                opened_at=cell.opened_at,
                last_status=cell.last_status,
                consecutive_failures=cell.consecutive_failures,
                consecutive_successes=cell.consecutive_successes,
            )

    def snapshots(self) -> list[CircuitSnapshot]:
        with self._lock:
            now = self.clock.now()
            out = []
            for name, cell in sorted(self._cells.items()):
                self._sync(cell, now)
                out.append(
                    CircuitSnapshot(
                        provider=name,
                        state=cell.state,
                        retry_at=cell.retry_at,
                        opened_at=cell.opened_at,
                        last_status=cell.last_status,
                        consecutive_failures=cell.consecutive_failures,
                        consecutive_successes=cell.consecutive_successes,
                    )
                )
            return out

    def allow(self, provider: str) -> None:
        """Raise ``CircuitOpen`` if this provider must be skipped."""
        with self._lock:
            now = self.clock.now()
            cell = self._cell(provider)
            state = self._sync(cell, now)
            if state == CircuitState.OPEN:
                retry_at = cell.retry_at or (now + self.default_cooldown_seconds)
                raise CircuitOpen(provider, retry_at, max(0.0, retry_at - now))
            if state == CircuitState.HALF_OPEN:
                if cell.probe_in_flight:
                    retry_at = cell.retry_at or now
                    raise CircuitOpen(provider, retry_at, max(0.0, retry_at - now))
                cell.probe_in_flight = True

    def on_response(
        self,
        provider: str,
        status_code: int,
        retry_after: str | None = None,
    ) -> CircuitState:
        """Update the circuit from an HTTP response (or a synthesized one)."""
        with self._lock:
            now = self.clock.now()
            cell = self._cell(provider)
            self._sync(cell, now)
            cell.last_status = status_code
            cell.probe_in_flight = False
            if 200 <= status_code < 300:
                cell.state = CircuitState.CLOSED
                cell.retry_at = None
                cell.opened_at = None
                cell.consecutive_failures = 0
                cell.consecutive_successes += 1
                return cell.state
            rate_limited = status_code in {429, 503}
            cell.consecutive_successes = 0
            cell.consecutive_failures += 1
            should_open = rate_limited or cell.consecutive_failures >= self.open_after_fails
            if should_open:
                retry_at = parse_retry_after(
                    retry_after if rate_limited or retry_after else None,
                    now=now,
                    default_seconds=self.default_cooldown_seconds,
                    max_seconds=self.max_cooldown_seconds,
                )
                cell.state = CircuitState.OPEN
                cell.retry_at = retry_at
                cell.opened_at = now
            return cell.state

    def on_transport_error(self, provider: str) -> CircuitState:
        """A timeout or connection error counts as a failure without a header."""
        return self.on_response(provider, 599, retry_after=None)

    def reset(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._cells.clear()
            elif provider in self._cells:
                self._cells[provider] = _Cell()

    def record_headers(
        self,
        provider: str,
        status_code: int,
        headers: Mapping[str, str] | None,
    ) -> CircuitState:
        retry = None
        if headers:
            for key, value in headers.items():
                if key.lower() == "retry-after":
                    retry = value
                    break
        return self.on_response(provider, status_code, retry)
