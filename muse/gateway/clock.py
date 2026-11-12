"""Injectable clocks so circuit-breaker tests do not sleep.

The breaker stores deadlines on a *monotonic* clock so NTP steps cannot
close or extend a cooldown. HTTP-date Retry-After values are converted
using a wall clock, then applied as a monotonic delay.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds, used for cooldown deadlines."""
        ...

    def monotonic(self) -> float:
        ...

    def wall(self) -> float:
        """Unix time, used only to interpret HTTP-date Retry-After values."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()


class FakeClock:
    """A clock tests can advance. ``now`` / ``monotonic`` share one timeline;
    ``wall`` is a parallel Unix-time timeline that advances by the same
    amounts, starting at a fixed epoch so HTTP-date tests are deterministic.
    """

    def __init__(self, start: float = 1_700_000_000.0, *, wall: float | None = None) -> None:
        self._now = start
        self._wall = wall if wall is not None else start

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the fake clock backwards")
        self._now += seconds
        self._wall += seconds

    def set(self, when: float) -> None:
        # Keep the wall/mono offset stable when a test rewinds the mono time.
        delta = when - self._now
        self._now = when
        self._wall += delta
