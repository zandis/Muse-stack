"""Injectable clocks so circuit-breaker tests do not sleep."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Unix time in seconds (float)."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move the fake clock backwards")
        self._now += seconds

    def set(self, when: float) -> None:
        self._now = when
