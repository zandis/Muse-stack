"""Per-provider / per-upstream circuit breaker.

This is *not* "the layer that reads Retry-After in front of LiteLLM".
LiteLLM 1.108.5 already honours a provider's Retry-After when it cooldowns
a deployment (priority: deployment ``cooldown_time`` > Retry-After header >
router ``cooldown_time``). See ``docs/ARCHITECTURE.md``.

This breaker is used for:

* Muse-side calls (the useful-work runner) that must skip an open circuit.
* A quota-exhausted state, distinct from a short rate-limit cooldown.
* Cross-alias cooldown: the same upstream model under different LiteLLM
  aliases shares one circuit, so a quota exhaustion on ``groq/…`` cools
  every alias that points at it.

States:

* ``closed`` — traffic flows.
* ``open`` — rate-limited (429/503) or too many 5xx; calls raise
  ``CircuitOpen`` until ``retry_at``.
* ``exhausted`` — like ``open``, but for a quota-exhausted response;
  the cooldown is longer and the health label is distinct.
* ``half_open`` — the deadline has passed; one probe is allowed.
  A hung probe times out and the circuit re-opens with backoff.
  Refusing a second probe reports a *positive* ``retry_after``.

A late success from a request that started before the circuit opened
does not close it. Only a successful probe (or a success while closed)
does. Client 4xx other than 429 never open the circuit. Deadlines use
a monotonic clock, with jitter so a herd of callers does not retry at
the same instant.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from muse.gateway.clock import Clock, SystemClock
from muse.gateway.retry_after import InvalidRetryAfter, parse_retry_after


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    EXHAUSTED = "exhausted"


class CircuitOpen(Exception):
    """Raised when a call is refused because the circuit is open or exhausted."""

    def __init__(self, provider: str, retry_at: float, retry_after: float) -> None:
        self.provider = provider
        self.retry_at = retry_at
        self.retry_after = retry_after
        super().__init__(
            f"circuit open for {provider}; retry after {retry_after:.1f}s"
        )


@dataclass(frozen=True)
class ProbeTicket:
    """Issued by ``allow``; passed back to ``on_response``.

    ``generation`` increments every time the circuit opens. A stale
    in-flight success carries an older generation and cannot close an
    already-open circuit.
    """

    provider: str
    generation: int
    is_probe: bool


@dataclass
class CircuitSnapshot:
    provider: str
    state: CircuitState
    retry_at: float | None = None
    opened_at: float | None = None
    last_status: int | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    reason: str | None = None

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
            "reason": self.reason,
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
    probe_deadline: float | None = None
    generation: int = 0
    reason: str | None = None


# Client errors that must never trip the breaker. 429 is handled separately.
_IGNORABLE_4XX = set(range(400, 500)) - {429}


@dataclass
class CircuitBreaker:
    """Thread-safe map of circuit key → cell.

    Keys are upstream model ids when an alias map is provided (so two
    aliases that share ``groq/openai/gpt-oss-20b`` share a circuit), and
    otherwise the name the caller passed (a provider id or an alias).

    ``on_response`` is the only mutator a caller needs: pass the HTTP
    status, the raw Retry-After header (or ``None``), and the ticket
    from ``allow`` when you have one. 429 and 503 open the circuit.
    A quota-exhausted 429 goes to ``exhausted``. Other 5xx increment
    failures and open after ``open_after_fails``. Other 4xx are ignored.
    """

    default_cooldown_seconds: float = 30.0
    max_cooldown_seconds: float = 3600.0
    open_after_fails: int = 2
    probe_timeout_seconds: float = 30.0
    jitter_ratio: float = 0.1
    quota_exhausted_seconds: float = 3600.0
    clock: Clock = field(default_factory=SystemClock)
    # alias -> list of upstream keys (e.g. "groq/openai/gpt-oss-20b")
    alias_upstreams: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _cells: dict[str, _Cell] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _rng: random.Random = field(default_factory=random.Random)

    def _cell(self, key: str) -> _Cell:
        if key not in self._cells:
            self._cells[key] = _Cell()
        return self._cells[key]

    def keys_for(self, name: str) -> tuple[str, ...]:
        """Circuit keys that must be free for ``name`` (alias or upstream) to be called.

        An alias is blocked only when *every* one of its upstreams is
        open/exhausted (LiteLLM can still pick a healthy sibling). A
        bare name with no alias mapping is a single key.
        """
        upstreams = self.alias_upstreams.get(name)
        if upstreams:
            return tuple(upstreams)
        return (name,)

    def siblings(self, name: str) -> tuple[str, ...]:
        """All aliases that share at least one upstream with ``name``, plus ``name``."""
        keys = set(self.keys_for(name))
        out = {name}
        for alias, upstreams in self.alias_upstreams.items():
            if keys.intersection(upstreams):
                out.add(alias)
        return tuple(sorted(out))

    def _sync(self, cell: _Cell, now: float) -> CircuitState:
        if cell.state in {CircuitState.OPEN, CircuitState.EXHAUSTED}:
            if cell.retry_at is not None and now >= cell.retry_at:
                cell.state = CircuitState.HALF_OPEN
                cell.probe_in_flight = False
                cell.probe_deadline = None
        if cell.state == CircuitState.HALF_OPEN and cell.probe_in_flight:
            if cell.probe_deadline is not None and now >= cell.probe_deadline:
                # Hung probe: re-open with the default cooldown so we do
                # not stay half-open forever, and so the next allow() is
                # not a spin with retry_after=0.
                self._open(
                    cell,
                    now,
                    delay=self.default_cooldown_seconds,
                    status=cell.last_status,
                    reason="probe_timeout",
                    exhausted=False,
                )
        return cell.state

    def _jittered(self, delay: float) -> float:
        if self.jitter_ratio <= 0 or delay <= 0:
            return delay
        # Full jitter in [delay, delay * (1 + jitter_ratio)].
        return delay * (1.0 + self._rng.uniform(0.0, self.jitter_ratio))

    def _open(
        self,
        cell: _Cell,
        now: float,
        *,
        delay: float,
        status: int | None,
        reason: str | None,
        exhausted: bool,
    ) -> None:
        cell.state = CircuitState.EXHAUSTED if exhausted else CircuitState.OPEN
        cell.retry_at = now + self._jittered(delay)
        cell.opened_at = now
        cell.probe_in_flight = False
        cell.probe_deadline = None
        cell.generation += 1
        cell.reason = reason
        if status is not None:
            cell.last_status = status
        cell.consecutive_successes = 0

    def state(self, provider: str) -> CircuitState:
        with self._lock:
            now = self.clock.now()
            # An alias is "open" if every upstream key is open/exhausted.
            states = []
            for key in self.keys_for(provider):
                states.append(self._sync(self._cell(key), now))
            if all(s in {CircuitState.OPEN, CircuitState.EXHAUSTED} for s in states):
                return CircuitState.EXHAUSTED if all(
                    s == CircuitState.EXHAUSTED for s in states
                ) else CircuitState.OPEN
            if any(s == CircuitState.HALF_OPEN for s in states) and all(
                s != CircuitState.CLOSED for s in states
            ):
                return CircuitState.HALF_OPEN
            if any(s in {CircuitState.OPEN, CircuitState.EXHAUSTED, CircuitState.HALF_OPEN} for s in states):
                # At least one upstream is still callable (closed).
                if all(s == CircuitState.CLOSED for s in states[0:1]) or any(
                    s == CircuitState.CLOSED for s in states
                ):
                    return CircuitState.CLOSED
            return states[0] if len(states) == 1 else CircuitState.CLOSED

    def _snapshot_cell(self, name: str, cell: _Cell) -> CircuitSnapshot:
        return CircuitSnapshot(
            provider=name,
            state=cell.state,
            retry_at=cell.retry_at,
            opened_at=cell.opened_at,
            last_status=cell.last_status,
            consecutive_failures=cell.consecutive_failures,
            consecutive_successes=cell.consecutive_successes,
            reason=cell.reason,
        )

    def snapshot(self, provider: str) -> CircuitSnapshot:
        with self._lock:
            now = self.clock.now()
            # Prefer a snapshot of the "worst" key for this name.
            worst: _Cell | None = None
            worst_name = provider
            rank = {
                CircuitState.CLOSED: 0,
                CircuitState.HALF_OPEN: 1,
                CircuitState.OPEN: 2,
                CircuitState.EXHAUSTED: 3,
            }
            for key in self.keys_for(provider):
                cell = self._cell(key)
                self._sync(cell, now)
                if worst is None or rank[cell.state] > rank[worst.state]:
                    worst = cell
                    worst_name = key
            assert worst is not None
            snap = self._snapshot_cell(provider, worst)
            # Keep the provider field as the name the caller asked about.
            snap.provider = provider
            return snap

    def snapshots(self) -> list[CircuitSnapshot]:
        with self._lock:
            now = self.clock.now()
            out = []
            for name, cell in sorted(self._cells.items()):
                self._sync(cell, now)
                out.append(self._snapshot_cell(name, cell))
            return out

    def allow(self, provider: str) -> ProbeTicket:
        """Raise ``CircuitOpen`` if this provider / alias must be skipped.

        Returns a ticket the caller must pass to ``on_response``. For an
        alias with several upstreams, the call is allowed if *any*
        upstream is callable; the ticket is issued against a callable
        upstream (a half-open probe if that is all that is left).
        """
        with self._lock:
            now = self.clock.now()
            keys = self.keys_for(provider)
            # Prefer a closed upstream; then a half-open one we can probe.
            closed: str | None = None
            probe: str | None = None
            blocked: list[tuple[str, _Cell]] = []
            for key in keys:
                cell = self._cell(key)
                state = self._sync(cell, now)
                if state == CircuitState.CLOSED:
                    closed = key
                elif state == CircuitState.HALF_OPEN and not cell.probe_in_flight:
                    probe = key
                elif state == CircuitState.HALF_OPEN and cell.probe_in_flight:
                    blocked.append((key, cell))
                else:
                    blocked.append((key, cell))
            if closed is not None:
                cell = self._cell(closed)
                return ProbeTicket(provider=closed, generation=cell.generation, is_probe=False)
            if probe is not None:
                cell = self._cell(probe)
                cell.probe_in_flight = True
                cell.probe_deadline = now + self.probe_timeout_seconds
                return ProbeTicket(provider=probe, generation=cell.generation, is_probe=True)
            # Everything is blocked. Report a positive retry_after: the
            # soonest deadline among open cells, or remaining probe time,
            # never 0 if we can help it. Do *not* initialise retry_at to
            # now+default and then take min() — that would cap a longer
            # Retry-After or a quota-exhausted cooldown down to 30s.
            retry_at: float | None = None
            for key, cell in blocked:
                candidate = None
                if cell.state == CircuitState.HALF_OPEN and cell.probe_deadline is not None:
                    candidate = cell.probe_deadline
                elif cell.retry_at is not None and cell.retry_at > now:
                    candidate = cell.retry_at
                if candidate is not None:
                    retry_at = candidate if retry_at is None else min(retry_at, candidate)
            if retry_at is None:
                retry_at = now + self.default_cooldown_seconds
            retry_after = max(0.001, retry_at - now)
            raise CircuitOpen(provider, retry_at, retry_after)

    def on_response(
        self,
        provider: str,
        status_code: int,
        retry_after: str | None = None,
        *,
        ticket: ProbeTicket | None = None,
        quota_exhausted: bool = False,
        wall: float | None = None,
    ) -> CircuitState:
        """Update the circuit from an HTTP response (or a synthesized one).

        ``retry_after`` is the raw header. An unparseable value falls back
        to the default cooldown (this is a provider response, not an API
        input — the HTTP layer returns 400 before calling us when a
        client-supplied header is bad).
        """
        with self._lock:
            now = self.clock.now()
            key = ticket.provider if ticket is not None else self.keys_for(provider)[0]
            cell = self._cell(key)
            self._sync(cell, now)
            cell.last_status = status_code

            if 200 <= status_code < 300:
                return self._on_success(cell, ticket)

            if status_code in _IGNORABLE_4XX:
                # A 401/404 is the caller's problem, not a sick upstream.
                # Do not increment consecutive_failures and do not open.
                if ticket is not None and ticket.is_probe:
                    cell.probe_in_flight = False
                    cell.probe_deadline = None
                return cell.state

            rate_limited = status_code in {429, 503}
            cell.consecutive_successes = 0
            if rate_limited or 500 <= status_code:
                cell.consecutive_failures += 1
            should_open = rate_limited or (
                500 <= status_code and cell.consecutive_failures >= self.open_after_fails
            )
            if should_open:
                delay = self._delay_from_header(
                    retry_after,
                    now_wall=wall if wall is not None else self.clock.wall(),
                    exhausted=quota_exhausted,
                )
                self._open(
                    cell,
                    now,
                    delay=delay,
                    status=status_code,
                    reason="quota_exhausted" if quota_exhausted else (
                        "rate_limited" if rate_limited else "server_error"
                    ),
                    exhausted=quota_exhausted,
                )
                # Cross-alias: open the same upstream key is enough —
                # aliases that list this upstream already share the cell
                # when they go through keys_for(). When the caller used
                # an alias name as the key and we have a map, also open
                # each shared upstream so siblings cool together.
                if not ticket:
                    self._cool_shared_upstreams(
                        provider,
                        now,
                        delay=delay,
                        status=status_code,
                        exhausted=quota_exhausted,
                        skip=key,
                    )
            elif ticket is not None and ticket.is_probe:
                cell.probe_in_flight = False
                cell.probe_deadline = None
            return cell.state

    def _cool_shared_upstreams(
        self,
        name: str,
        now: float,
        *,
        delay: float,
        status: int,
        exhausted: bool,
        skip: str,
    ) -> None:
        for key in self.keys_for(name):
            if key == skip:
                continue
            cell = self._cell(key)
            self._sync(cell, now)
            self._open(
                cell,
                now,
                delay=delay,
                status=status,
                reason="cross_alias",
                exhausted=exhausted,
            )

    def _delay_from_header(self, retry_after: str | None, *, now_wall: float, exhausted: bool) -> float:
        default = self.quota_exhausted_seconds if exhausted else self.default_cooldown_seconds
        if exhausted and not (retry_after or "").strip():
            return default
        try:
            return parse_retry_after(
                retry_after,
                now_wall=now_wall,
                default_seconds=default,
                max_seconds=self.max_cooldown_seconds,
            )
        except InvalidRetryAfter:
            return default

    def _on_success(self, cell: _Cell, ticket: ProbeTicket | None) -> CircuitState:
        # A late success must not close an already-open (or exhausted) circuit.
        if cell.state in {CircuitState.OPEN, CircuitState.EXHAUSTED}:
            return cell.state
        if ticket is not None and ticket.generation != cell.generation:
            return cell.state
        if cell.state == CircuitState.HALF_OPEN:
            if ticket is not None and not ticket.is_probe:
                return cell.state
            # Probe success (or a test that did not pass a ticket but is
            # completing the in-flight probe).
            if ticket is None and not cell.probe_in_flight:
                # A random success while half-open with no probe in flight
                # is not a probe result; ignore it.
                return cell.state
        cell.state = CircuitState.CLOSED
        cell.retry_at = None
        cell.opened_at = None
        cell.consecutive_failures = 0
        cell.consecutive_successes += 1
        cell.probe_in_flight = False
        cell.probe_deadline = None
        cell.reason = None
        return cell.state

    def on_transport_error(
        self,
        provider: str,
        *,
        ticket: ProbeTicket | None = None,
    ) -> CircuitState:
        """A timeout or connection error counts as a 5xx-class failure."""
        return self.on_response(provider, 599, retry_after=None, ticket=ticket)

    def on_quota_exhausted(
        self,
        provider: str,
        retry_after: str | None = None,
        *,
        ticket: ProbeTicket | None = None,
    ) -> CircuitState:
        return self.on_response(
            provider,
            429,
            retry_after,
            ticket=ticket,
            quota_exhausted=True,
        )

    def reset(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._cells.clear()
            else:
                for key in self.keys_for(provider):
                    self._cells[key] = _Cell()
                if provider in self._cells:
                    self._cells[provider] = _Cell()

    def record_headers(
        self,
        provider: str,
        status_code: int,
        headers: Mapping[str, str] | None,
        *,
        ticket: ProbeTicket | None = None,
        quota_exhausted: bool = False,
    ) -> CircuitState:
        retry = None
        if headers:
            for key, value in headers.items():
                if key.lower() == "retry-after":
                    retry = value
                    break
        return self.on_response(
            provider,
            status_code,
            retry,
            ticket=ticket,
            quota_exhausted=quota_exhausted,
        )
