"""Parse the Retry-After header (RFC 9110 §10.2.3).

The header is either a delay in delta-seconds or an HTTP-date. This module
returns a *delay in seconds*, which the breaker then adds to a monotonic
deadline. A missing header uses a default cooldown. An unparseable value
raises ``InvalidRetryAfter`` so an HTTP API can answer 400; callers that
are reading a provider response may catch that and fall back to the default.
A past HTTP-date is treated as "use the default cooldown", not as a zero
delay that would immediately half-open the circuit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class InvalidRetryAfter(ValueError):
    """The header was present but could not be parsed as RFC 9110 Retry-After."""


def parse_retry_after(
    value: str | None,
    *,
    now_wall: float,
    default_seconds: float = 30.0,
    max_seconds: float = 3600.0,
) -> float:
    """Return a delay in seconds until a retry is first allowed.

    ``now_wall`` is Unix time, used only for HTTP-date headers.
    The delay is capped at ``max_seconds``.
    """
    raw = (value or "").strip()
    if not raw:
        return _cap(default_seconds, max_seconds)
    parsed = _parse(raw, now_wall)
    if parsed == "invalid":
        raise InvalidRetryAfter("unparseable Retry-After header")
    if parsed == "past":
        return _cap(default_seconds, max_seconds)
    return _cap(float(parsed), max_seconds)


def _cap(delay: float, max_seconds: float) -> float:
    return max(0.0, min(float(delay), max_seconds))


def _parse(raw: str, now_wall: float) -> float | str:
    # Only ASCII digits: str.isdigit() is true for fullwidth digits, which
    # we do not accept as a delta-seconds value.
    if raw.isascii() and raw.isdigit():
        return float(int(raw))
    try:
        as_float = float(raw)
    except ValueError:
        as_float = None
    if (
        as_float is not None
        and raw.isascii()
        and as_float >= 0
        and " " not in raw
        and ":" not in raw
        and "T" not in raw
        and "," not in raw
    ):
        return as_float
    if as_float is not None and as_float < 0 and " " not in raw and ":" not in raw:
        # A negative number of seconds is not a valid delay; treat as invalid
        # so the API can 400, and provider-side callers can use the default.
        return "invalid"
    try:
        when: datetime = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return "invalid"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = when.timestamp() - now_wall
    if delay <= 0:
        return "past"
    return delay
