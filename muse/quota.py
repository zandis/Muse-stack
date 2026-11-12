"""Turn provider rate-limit signals into blocks and observed limits.

Why this exists: LiteLLM cools a single deployment for a fixed time and knows
nothing about daily or monthly quotas. These pure functions read a failed
response (status, headers, body) and say how long an upstream model, or a whole
provider, should be left alone. Nothing here does I/O, so every rule can be
table-tested. Times are epoch seconds (float); `now` is always passed in.

Shapes that were checked against provider docs on 2026-11-12:
- Groq: `retry-after` (seconds, 429 only); `x-ratelimit-*-requests` are
  requests per DAY, `x-ratelimit-*-tokens` are tokens per MINUTE; reset values
  look like `2m59.56s` / `7.66s` (console.groq.com/docs/rate-limits).
- OpenRouter: `X-RateLimit-Limit/Remaining/Reset` on rate-limit errors only,
  reset in epoch milliseconds; free models 20/min and 50 or 1000/day per
  account (openrouter.ai/docs/api-reference/limits).
- Gemini: 429 RESOURCE_EXHAUSTED; RPD resets at midnight Pacific; limits are
  per project and per model (aistudio.google.com/docs/rate-limits).
- Cohere: trial keys 1,000 calls/month, chat 20/min (docs.cohere.com/docs/rate-limits).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

MAX_TRUSTED_SECONDS = 31 * 86400  # never trust a reset further out than this
BAD_KEY_SECONDS = 15 * 60
PAYMENT_SECONDS = 60 * 60
DEFAULT_COOLDOWN_SECONDS = 60.0
LONG_RETRY_AFTER = 3600.0  # a Retry-After longer than this is a quota, not a blip

# 4xx statuses that are the caller's fault. They say nothing about the provider.
CALLER_ERRORS = frozenset({400, 404, 405, 409, 413, 415, 422})

_ASCII_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_DURATION = re.compile(
    r"(?:(?P<d>[0-9]+(?:\.[0-9]+)?)d)?"
    r"(?:(?P<h>[0-9]+(?:\.[0-9]+)?)h)?"
    r"(?:(?P<m>[0-9]+(?:\.[0-9]+)?)m(?!s))?"
    r"(?:(?P<s>[0-9]+(?:\.[0-9]+)?)s)?"
    r"(?:(?P<ms>[0-9]+(?:\.[0-9]+)?)ms)?",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class Block:
    """Leave an upstream (or a whole provider) alone until `until`."""

    scope: str  # "upstream" | "provider"
    kind: str  # "cooldown" | "exhausted"
    until: float
    reason: str

    def key(self, provider_id: str, upstream: str | None) -> str:
        """The ledger scope key: the upstream string, or `provider:<id>`."""
        if self.scope == "provider" or not upstream:
            return f"provider:{provider_id}"
        return upstream


@dataclass(frozen=True, slots=True)
class ObservedLimit:
    """What a provider said about a limit on its last response."""

    window: str  # minute | day | month
    metric: str  # requests | tokens
    limit: int | None
    remaining: int | None
    reset_at: float | None


# ---------------------------------------------------------------- time helpers


def _finite(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def parse_duration(value: Any) -> float | None:
    """`2m59.56s`, `7.66s`, `1h2m`, `6ms`, `41s` or bare seconds -> seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if _ASCII_NUMBER.fullmatch(text):
        seconds = float(text)
        return seconds if _finite(seconds) else None
    match = _DURATION.fullmatch(text)
    if not match or not any(match.groupdict().values()):
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v}
    seconds = (
        parts.get("d", 0) * 86400
        + parts.get("h", 0) * 3600
        + parts.get("m", 0) * 60
        + parts.get("s", 0)
        + parts.get("ms", 0) / 1000
    )
    return seconds if _finite(seconds) else None


def parse_retry_after(value: Any, now: float) -> float | None:
    """Retry-After as seconds from `now`.

    Accepts delta-seconds (ASCII digits, optional fraction) and HTTP-dates.
    Negative, NaN, non-ASCII digits and garbage give None. Values beyond
    MAX_TRUSTED_SECONDS are rejected rather than clamped: a month-long block
    from one odd header would do more harm than LiteLLM's own retry.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _ASCII_NUMBER.fullmatch(text):
        seconds = float(text)
    else:
        if text[0] in "+-" or text[0].isdigit():
            return None  # "-5", "1e3", "١٢"
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = max(0.0, when.timestamp() - now)
    if not _finite(seconds) or seconds < 0 or seconds > MAX_TRUSTED_SECONDS:
        return None
    return seconds


def parse_reset(value: Any, now: float) -> float | None:
    """A reset header as an absolute epoch time.

    Handles durations (`2m59.56s`), delta seconds, epoch seconds and epoch
    milliseconds. A value in the past means "now". Anything over 31 days out
    is not trusted (None).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _ASCII_NUMBER.fullmatch(text):
        number = float(text)
        if number >= 1e12:  # epoch milliseconds
            at = number / 1000.0
        elif number >= 1e9:  # epoch seconds
            at = number
        else:
            at = now + number
    else:
        seconds = parse_duration(text)
        if seconds is None:
            return None
        at = now + seconds
    if not _finite(at):
        return None
    at = max(at, now)
    return at if at - now <= MAX_TRUSTED_SECONDS else None


def _pacific_offset_hours(utc: datetime) -> int:
    """US Pacific offset without tzdata: PDT from 2nd Sunday of March 10:00 UTC
    to 1st Sunday of November 09:00 UTC (02:00 local both times)."""
    year = utc.year
    march = datetime(year, 3, 8, tzinfo=timezone.utc)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7, hours=10)
    november = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = november + timedelta(days=(6 - november.weekday()) % 7, hours=9)
    return -7 if dst_start <= utc < dst_end else -8


def next_midnight_pacific(now: float) -> float:
    """Gemini's RPD reset."""
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    offset = _pacific_offset_hours(utc)
    local = utc + timedelta(hours=offset)
    midnight_local = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) + timedelta(days=1)
    candidate = midnight_local - timedelta(hours=offset)
    # The offset may differ at the target midnight (DST switch day); recompute once.
    offset2 = _pacific_offset_hours(candidate)
    if offset2 != offset:
        candidate = midnight_local - timedelta(hours=offset2)
    return candidate.timestamp()


def next_midnight_utc(now: float) -> float:
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    return (datetime(utc.year, utc.month, utc.day, tzinfo=timezone.utc) + timedelta(days=1)).timestamp()


def next_month_utc(now: float) -> float:
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    year, month = (utc.year + 1, 1) if utc.month == 12 else (utc.year, utc.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def day_start(now: float, provider_id: str | None = None) -> float:
    """Start of the provider's quota day: Pacific for Gemini, UTC otherwise."""
    if provider_id == "gemini":
        return next_midnight_pacific(now) - 86400  # close enough on DST days
    return next_midnight_utc(now) - 86400


def month_start(now: float) -> float:
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    return datetime(utc.year, utc.month, 1, tzinfo=timezone.utc).timestamp()


def _daily_reset(provider_id: str, now: float) -> float:
    return next_midnight_pacific(now) if provider_id == "gemini" else next_midnight_utc(now)


def iso(ts: float | None) -> str | None:
    """ISO-8601 UTC with a trailing Z, the snapshot's time format."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- header/body helpers


def normalise_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Lower-case names; strip LiteLLM's `llm_provider-` prefix (the raw
    upstream copy wins over LiteLLM's re-exported one)."""
    out: dict[str, str] = {}
    if not headers:
        return out
    try:
        items = list(headers.items())
    except (AttributeError, TypeError):
        return out
    prefixed: dict[str, str] = {}
    for key, value in items:
        if value is None:
            continue
        name = str(key).strip().lower()
        if name.startswith("llm_provider-"):
            prefixed[name[len("llm_provider-"):]] = str(value)
        else:
            out[name] = str(value)
    out.update(prefixed)
    return out


def _json_candidates(body_text: str) -> list[Any]:
    """Every JSON object embedded in the text (LiteLLM often prefixes a message)."""
    found: list[Any] = []
    if not body_text:
        return found
    decoder = json.JSONDecoder()
    index = body_text.find("{")
    scanned = 0
    while index != -1 and scanned < 20:
        scanned += 1
        try:
            value, end = decoder.raw_decode(body_text, index)
        except ValueError:
            index = body_text.find("{", index + 1)
            continue
        found.append(value)
        index = body_text.find("{", end)
    return found


def _walk(value: Any, depth: int = 0):
    """Yield every dict inside a JSON value."""
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _walk(item, depth + 1)


@dataclass
class _Signals:
    text: str  # lower-cased message/body for regex rules
    gemini_daily: bool = False
    gemini_minute: bool = False
    retry_delay: float | None = None  # Gemini RetryInfo
    embedded_headers: dict[str, str] | None = None  # OpenRouter error.metadata.headers


def _signals(body_text: str) -> _Signals:
    sig = _Signals(text=(body_text or "").lower())
    for doc in _json_candidates(body_text or ""):
        for node in _walk(doc):
            quota_bits = " ".join(str(node.get(k, "")) for k in ("quotaId", "quotaMetric"))
            if "perday" in quota_bits.lower():
                sig.gemini_daily = True
            if "perminute" in quota_bits.lower():
                sig.gemini_minute = True
            if "retryDelay" in node:
                sig.retry_delay = parse_duration(node.get("retryDelay"))
            headers = node.get("headers")
            if isinstance(headers, dict) and any("ratelimit" in str(k).lower() for k in headers):
                sig.embedded_headers = normalise_headers(headers)
    # Gemini via LiteLLM's gemini/ provider can arrive as Python-repr text, not JSON.
    if not sig.gemini_daily and re.search(r"quotaid['\"]?\s*:\s*['\"][^'\"]*perday", sig.text):
        sig.gemini_daily = True
    if not sig.gemini_minute and re.search(r"quotaid['\"]?\s*:\s*['\"][^'\"]*perminute", sig.text):
        sig.gemini_minute = True
    if sig.retry_delay is None:
        match = re.search(r"retrydelay['\"]?\s*:\s*['\"]([0-9.]+s)['\"]", sig.text)
        if match:
            sig.retry_delay = parse_duration(match.group(1))
    return sig


_TRY_AGAIN = re.compile(r"(?:please )?try again in ([0-9hms.]+)", re.ASCII)
_DAILY_TEXT = re.compile(
    r"per[ -]day|\(tpd\)|\(rpd\)|daily (?:limit|quota)|free-models-per-day|requests per day|tokens per day"
)
_MONTHLY_TEXT = re.compile(r"/ ?month|per month|monthly (?:limit|quota)|calls a month")


def _block(scope: str, kind: str, until: float, reason: str, now: float) -> Block:
    return Block(scope=scope, kind=kind, until=max(until, now), reason=reason)


def _trusted(at: float | None, now: float) -> float | None:
    if at is None or not _finite(at) or at - now > MAX_TRUSTED_SECONDS:
        return None
    return max(at, now)


# ---------------------------------------------------------------- classify


def classify_failure(
    provider_id: str,
    status: int | None,
    headers: Mapping[str, Any] | None,
    body_text: str | None,
    now: float,
) -> Block | None:
    """Decide whether a failed call should block its upstream or provider.

    None means "leave it to LiteLLM": caller errors (400/404/413/422), 5xx,
    and anything not understood. 5xx is LiteLLM's job because its
    allowed_fails/cooldown already handle repeated server errors.
    """
    provider_id = str(provider_id or "").lower()
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    hdrs = normalise_headers(headers)
    sig = _signals(body_text or "")
    text = sig.text

    if status in (401, 403):
        what = "bad or revoked key" if status == 401 else "key has no access (403)"
        return _block(
            "provider", "exhausted", now + BAD_KEY_SECONDS,
            f"{what} for {provider_id}: check the key; retrying in 15 min", now,
        )
    if status == 402:
        return _block(
            "provider", "exhausted", now + PAYMENT_SECONDS,
            f"{provider_id} says payment required (out of credit); retrying in 1 h", now,
        )
    if status != 429:
        return None  # caller errors, 5xx, 408, anything else

    retry_after = parse_retry_after(hdrs.get("retry-after"), now)

    # --- Gemini: RESOURCE_EXHAUSTED with QuotaFailure details.
    if sig.gemini_daily:
        # retryDelay on a per-day violation is known to be a short, wrong hint.
        return _block(
            "upstream", "exhausted", next_midnight_pacific(now) if provider_id == "gemini" else next_midnight_utc(now),
            "daily quota exhausted (quotaId PerDay); resets at midnight Pacific (estimated)"
            if provider_id == "gemini" else "daily quota exhausted (PerDay); resets at UTC midnight (estimated)",
            now,
        )
    if sig.gemini_minute:
        wait = sig.retry_delay if sig.retry_delay is not None else retry_after
        source = "retryDelay" if sig.retry_delay is not None else "Retry-After"
        if wait is None:
            wait, source = DEFAULT_COOLDOWN_SECONDS, "estimated"
        return _block("upstream", "cooldown", now + min(wait, LONG_RETRY_AFTER),
                      f"per-minute quota (quotaId PerMinute); {source}", now)

    # --- OpenRouter: account-wide limits on :free models.
    if provider_id == "openrouter" and ("free-models-per-day" in text or "free-models-per-min" in text):
        embedded = sig.embedded_headers or {}
        reset = parse_reset(hdrs.get("x-ratelimit-reset") or embedded.get("x-ratelimit-reset"), now)
        if "free-models-per-day" in text:
            if _trusted(reset, now) is not None and reset > now:
                return _block("provider", "exhausted", reset, "OpenRouter free-models-per-day limit; X-RateLimit-Reset", now)
            return _block("provider", "exhausted", next_midnight_utc(now),
                          "OpenRouter free-models-per-day limit; resets at UTC midnight (estimated)", now)
        wait = retry_after if retry_after is not None else ((reset - now) if reset else None)
        return _block("provider", "cooldown", now + (wait if wait else DEFAULT_COOLDOWN_SECONDS),
                      "OpenRouter free-models-per-minute limit", now)

    # --- Cohere trial monthly cap.
    # unconfirmed: exact Cohere wording; docs say "1,000 API calls a month" but show no error body.
    if provider_id == "cohere" and "trial" in text and _MONTHLY_TEXT.search(text):
        return _block("provider", "exhausted", next_month_utc(now),
                      "Cohere trial monthly call limit; resets on the 1st (UTC, estimated)", now)

    # --- Text that names a daily or monthly quota (Groq TPD/RPD, generic).
    hint = None
    match = _TRY_AGAIN.search(text)
    if match:
        hint = parse_duration(match.group(1).rstrip("."))
    if _DAILY_TEXT.search(text):
        # Groq's TPD/RPD are rolling windows, so its "try again in" hint is the real wait.
        candidates = [now + hint] if hint is not None else []
        if retry_after is not None:
            candidates.append(now + retry_after)
        until = _trusted(max(candidates), now) if candidates else None
        if until is not None and until > now:
            return _block("upstream", "exhausted", until, "daily quota exhausted; provider retry hint", now)
        return _block("upstream", "exhausted", _daily_reset(provider_id, now),
                      "daily quota exhausted; reset time estimated "
                      + ("(midnight Pacific)" if provider_id == "gemini" else "(UTC midnight)"), now)
    if _MONTHLY_TEXT.search(text) and ("limit" in text or "quota" in text):
        return _block("provider", "exhausted", next_month_utc(now),
                      "monthly quota exhausted; resets on the 1st (UTC, estimated)", now)

    # --- Short rate limit.
    if retry_after is not None:
        if retry_after > LONG_RETRY_AFTER:
            return _block("upstream", "exhausted", now + retry_after, "429 with a long Retry-After", now)
        return _block("upstream", "cooldown", now + retry_after, "429 rate limit; Retry-After", now)
    if hint is not None and hint <= MAX_TRUSTED_SECONDS:
        kind = "exhausted" if hint > LONG_RETRY_AFTER else "cooldown"
        return _block("upstream", kind, now + hint, "429 rate limit; provider retry hint", now)
    resets = [
        parse_reset(hdrs.get(name), now)
        for name in hdrs
        if name.startswith(("x-ratelimit-reset", "ratelimit-reset"))
    ]
    resets = [at for at in resets if at is not None and at > now]
    if resets:
        until = max(resets)
        kind = "exhausted" if until - now > LONG_RETRY_AFTER else "cooldown"
        return _block("upstream", kind, until, "429 rate limit; x-ratelimit-reset", now)
    return _block("upstream", "cooldown", now + DEFAULT_COOLDOWN_SECONDS,
                  "429 rate limit; no reset given (estimated 60 s)", now)


# ---------------------------------------------------------------- observed limits


def _int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not _ASCII_NUMBER.fullmatch(text):
        return None
    number = float(text)
    return int(number) if _finite(number) else None


_SUFFIXED = re.compile(r"x-ratelimit-(limit|remaining|reset)-(requests|tokens)(?:-(minute|day|month))?$")


def parse_limit_headers(provider_id: str, headers: Mapping[str, Any] | None, now: float) -> list[ObservedLimit]:
    """Limits the provider reported on a response.

    OpenAI-style `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` are
    per minute, except Groq where `-requests` is per day. A trailing
    `-minute|-day|-month` (Cerebras-style) wins. OpenRouter's unsuffixed
    `x-ratelimit-*` count requests; its window is not stated, so a reset more
    than an hour away is read as a day. # unconfirmed: OpenRouter window inference.
    """
    provider_id = str(provider_id or "").lower()
    hdrs = normalise_headers(headers)
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for name, value in hdrs.items():
        match = _SUFFIXED.match(name)
        if not match:
            continue
        field_name, metric, window = match.groups()
        if window is None:
            window = "day" if (provider_id == "groq" and metric == "requests") else "minute"
        groups.setdefault((window, metric), {})[field_name] = value

    plain = {k: hdrs.get(f"x-ratelimit-{k}") for k in ("limit", "remaining", "reset")}
    if not any(plain.values()):
        plain = {k: hdrs.get(f"ratelimit-{k}") for k in ("limit", "remaining", "reset")}  # IETF draft
    if any(plain.values()):
        reset_at = parse_reset(plain.get("reset"), now)
        window = "day" if reset_at is not None and reset_at - now > 3600 else "minute"
        groups.setdefault((window, "requests"), {}).update({k: v for k, v in plain.items() if v is not None})

    out: list[ObservedLimit] = []
    for (window, metric), values in sorted(groups.items()):
        limit = _int(values.get("limit"))
        remaining = _int(values.get("remaining"))
        reset_at = parse_reset(values.get("reset"), now)
        if limit is None and remaining is None and reset_at is None:
            continue
        out.append(ObservedLimit(window, metric, limit, remaining, reset_at))
    return out
