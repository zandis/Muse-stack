"""Parse the Retry-After header (RFC 9110 §10.2.3).

The header is either a delay in delta-seconds or an HTTP-date. This module
turns that into a unix timestamp: the earliest moment the circuit may close.
A missing or unparseable value falls back to a default cooldown, so a 429
without a header still trips the breaker.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def parse_retry_after(
    value: str | None,
    *,
    now: float,
    default_seconds: float = 30.0,
    max_seconds: float = 3600.0,
) -> float:
    """Return the unix time at which a retry is first allowed.

    ``default_seconds`` is used when the header is missing or invalid.
    The delay is capped at ``max_seconds`` so a hostile or huge value
    cannot park a provider for longer than an hour (override in the caller
    if you need a different cap).
    """
    delay = default_seconds
    raw = (value or "").strip()
    if raw:
        parsed = _parse(raw, now)
        if parsed is not None:
            delay = parsed
    delay = max(0.0, min(float(delay), max_seconds))
    return now + delay


def _parse(raw: str, now: float) -> float | None:
    if raw.isdigit():
        return float(int(raw))
    # Some servers send a float number of seconds.
    try:
        as_float = float(raw)
    except ValueError:
        as_float = None
    if as_float is not None and as_float >= 0 and " " not in raw and ":" not in raw:
        return as_float
    try:
        when: datetime = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, when.timestamp() - now)
