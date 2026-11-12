"""Ask a provider how much quota is left, where it offers a way to ask.

Only OpenRouter does (GET /api/v1/key). Every other provider in the catalogue
has to be tracked from response headers and 429s, which the ledger handles.
A probe costs no model quota. It is never run in the background by the proxy;
`muse status --probe` and the dashboard's manual refresh call it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

Fetch = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URL)
            return response.status, response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_utc_midnight(now: datetime) -> datetime:
    day = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return int(value)


def parse_openrouter_key(payload: Any, now: datetime) -> list[dict[str, Any]]:
    """Turn OpenRouter's key document into snapshot `Limit` dicts.

    Only the free-model daily request allowance is a quota in our sense. The
    credit balance is money, so it is left to OpenRouter's own dashboard.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    free = data.get("free_model_daily_requests")
    if not isinstance(free, dict):
        return []
    limit = _int(free.get("limit"))
    remaining = _int(free.get("remaining"))
    used = _int(free.get("used"))
    if remaining is None and limit is not None and used is not None:
        remaining = max(limit - used, 0)
    if limit is None and remaining is None:
        return []
    return [
        {
            "window": "day",
            "metric": "requests",
            "limit": limit,
            "remaining": remaining,
            "reset_at": _iso(_next_utc_midnight(now)),  # OpenRouter counts the UTC day
            "source": "header",  # told by the provider, not estimated
        }
    ]


def probe_openrouter(
    api_key: str,
    now: datetime | None = None,
    fetch: Fetch = _http_get,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Remaining free-model requests today. Empty list on any failure."""
    if not api_key:
        return []
    moment = now or datetime.now(timezone.utc)
    try:
        status, body = fetch(OPENROUTER_KEY_URL, {"Authorization": f"Bearer {api_key}"}, timeout)
        if status != 200:
            return []
        return parse_openrouter_key(json.loads(body.decode("utf-8")), moment)
    except (OSError, ValueError, UnicodeDecodeError):
        return []


PROBES: dict[str, Callable[..., list[dict[str, Any]]]] = {"openrouter": probe_openrouter}
