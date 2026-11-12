"""Detect a quota-exhausted error, as distinct from a short rate limit.

LiteLLM already cools a deployment for a 429 using the provider's
Retry-After (or the router default). A *quota* exhaustion (daily or
weekly cap, "insufficient_quota", "resource exhausted") should be
treated as a longer, explicit exhausted state, and should cool every
alias that shares the same upstream model.

This module is pure and is imported by both the Muse package and the
standalone LiteLLM callback (which duplicates the matcher so the
callback has no ``muse`` dependency).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Phrases that indicate a cap has been used up, not a short RPM window.
_EXHAUSTED = re.compile(
    r"(insufficient[_\s-]?quota|quota[_ ]?(exceeded|exhausted|used up)|"
    r"exceeded[_\s]?(your )?quotas?|resource[_ ]?exhaust(?:ed|ion)|"
    r"daily[_ ]?(limit|quota|cap)|weekly[_ ]?(limit|quota|cap)|"
    r"monthly[_ ]?(limit|quota|cap)|"
    r"you[ ]+have[ ]+exceeded|"
    r"tokens?[ ]+per[ ]+(day|week|month)|"
    r"free[ ]+tier[ ]+(limit|exhausted|exceeded)|"
    r"credit[s]?[ ]+(exhausted|exceeded|used up)|"
    r"billing[_\s]?hard[_\s]?limit)",
    re.IGNORECASE,
)

# Phrases that are ordinary short rate limits — do not promote these to
# the exhausted state; LiteLLM's Retry-After path is the right one.
_RATE_LIMIT = re.compile(
    r"(rate[_ ]?limit|too[ ]+many[ ]+requests|tpm|rpm|"
    r"tokens?[ ]+per[ ]+minute|requests?[ ]+per[ ]+(minute|second)|"
    r"slow[ ]*down)",
    re.IGNORECASE,
)


def is_quota_exhausted(
    *,
    status_code: int | None = None,
    message: str | None = None,
    body: Mapping[str, Any] | str | None = None,
    headers: Mapping[str, str] | None = None,
) -> bool:
    """Return True when the failure looks like a used-up quota, not a 429 window."""
    parts: list[str] = []
    if message:
        parts.append(message)
    if isinstance(body, str):
        parts.append(body)
    elif isinstance(body, Mapping):
        parts.append(_flatten(body))
    if headers:
        for key, value in headers.items():
            if key.lower() in {"x-ratelimit-remaining", "x-ratelimit-remaining-requests",
                               "x-ratelimit-remaining-tokens", "retry-after"}:
                parts.append(f"{key}:{value}")
            # Some providers advertise a daily remaining counter.
            if "remaining" in key.lower() and "day" in key.lower():
                parts.append(f"{key}:{value}")
                try:
                    if float(value) <= 0:
                        return True
                except (TypeError, ValueError):
                    pass
    blob = " ".join(parts)
    if not blob:
        # A bare 429 without a body is a rate limit, not a quota verdict.
        return False
    if _EXHAUSTED.search(blob):
        return True
    return False


def _flatten(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        return " ".join(f"{k} {_flatten(v)}" for k, v in payload.items())
    if isinstance(payload, (list, tuple)):
        return " ".join(_flatten(item) for item in payload)
    return str(payload)
