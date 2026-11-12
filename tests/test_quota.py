"""Table-driven tests for muse.quota. Pure functions, fixed clock."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from muse import quota
from muse.quota import (
    classify_failure,
    next_midnight_pacific,
    next_midnight_utc,
    next_month_utc,
    parse_duration,
    parse_limit_headers,
    parse_reset,
    parse_retry_after,
)


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-11-12T07:00:00")  # 23:00 PST on 11 Nov


# ---------------------------------------------------------------- retry-after

@pytest.mark.parametrize(
    "value, expected",
    [
        ("120", 120.0),
        (" 5 ", 5.0),
        ("0", 0.0),
        ("1.5", 1.5),
        (7, 7.0),
        ("Thu, 12 Nov 2026 07:01:00 GMT", 60.0),
        ("Thu, 12 Nov 2026 06:00:00 GMT", 0.0),  # past date: retry now
        ("-5", None),
        ("nan", None),
        ("NaN", None),
        ("inf", None),
        ("1e3", None),
        ("", None),
        (None, None),
        ("soon", None),
        ("١٢", None),  # Arabic-Indic digits
        ("１２", None),  # full-width digits
        ("12abc", None),
        (str(32 * 86400), None),  # beyond the 31-day trust cap
        (str(31 * 86400), 31 * 86400.0),
        ("Mon, 32 Foo 2026 99:99:99 GMT", None),
    ],
)
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value, NOW) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2m59.56s", 179.56),
        ("7.66s", 7.66),
        ("1h2m", 3720.0),
        ("6ms", 0.006),
        ("41s", 41.0),
        ("1d", 86400.0),
        ("30", 30.0),
        ("", None),
        ("abc", None),
        ("m", None),
        ("-3s", None),
    ],
)
def test_parse_duration(value, expected):
    result = parse_duration(value)
    assert result == pytest.approx(expected) if expected is not None else result is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("7.66s", NOW + 7.66),
        ("30", NOW + 30),
        (str(int(NOW + 600)), NOW + 600),  # epoch seconds
        (str(int((NOW + 600) * 1000)), NOW + 600),  # epoch ms
        (str(int(NOW - 50)), NOW),  # already passed
        (str(int((NOW + 40 * 86400) * 1000)), None),  # too far out
        ("garbage", None),
    ],
)
def test_parse_reset(value, expected):
    result = parse_reset(value, NOW)
    assert result == pytest.approx(expected) if expected is not None else result is None


# ---------------------------------------------------------------- calendars

@pytest.mark.parametrize(
    "now, expected",
    [
        ("2026-11-12T07:00:00", "2026-11-12T08:00:00"),  # PST: midnight = 08:00Z
        ("2026-11-12T08:00:01", "2026-11-13T08:00:00"),
        ("2026-07-01T06:59:00", "2026-07-01T07:00:00"),  # PDT: midnight = 07:00Z
        ("2026-03-08T07:30:00", "2026-03-08T08:00:00"),  # 23:30 PST on 7 Mar
        ("2026-03-08T12:00:00", "2026-03-09T07:00:00"),  # DST started 10:00Z on 8 Mar
        ("2026-11-01T06:30:00", "2026-11-01T07:00:00"),  # DST ends 1 Nov 2026
        ("2026-11-01T08:30:00", "2026-11-02T08:00:00"),
    ],
)
def test_next_midnight_pacific(now, expected):
    assert next_midnight_pacific(ts(now)) == ts(expected)


def test_utc_boundaries():
    assert next_midnight_utc(NOW) == ts("2026-11-13T00:00:00")
    assert next_month_utc(NOW) == ts("2026-12-01T00:00:00")
    assert next_month_utc(ts("2026-12-31T23:00:00")) == ts("2027-01-01T00:00:00")
    assert quota.day_start(NOW, "gemini") == ts("2026-11-11T08:00:00")
    assert quota.month_start(NOW) == ts("2026-11-01T00:00:00")


# ---------------------------------------------------------------- classify

GEMINI_DAY = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})
GEMINI_MINUTE = GEMINI_DAY.replace("PerDay", "PerMinute")
GEMINI_MINUTE_NO_DELAY = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED", "details": [
    {"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}})
# LiteLLM's gemini/ provider can put the body in the exception message as text.
GEMINI_DAY_TEXT = "litellm.RateLimitError: VertexAIException - " + GEMINI_DAY
GROQ_TPD = ("Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` "
            "on tokens per day (TPD): Limit 200000, Used 193487, Requested 7464. Please try again in 6m50.832s.")
GROQ_RPM = "Rate limit reached for model `x` on requests per minute (RPM): Limit 30, Used 30. Please try again in 2s."
OR_DAY = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
                                "code": 429, "metadata": {"headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                                                      "X-RateLimit-Reset": str(int((NOW + 3600) * 1000))}}}})
OR_DAY_NO_RESET = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429}})
OR_MIN = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-min.", "code": 429}})
COHERE_TRIAL = ("You are using a Trial key, which is limited to 1000 API calls / month. You can continue to use the "
                "Trial key for free or upgrade to a Production key with higher rate limits")


@pytest.mark.parametrize(
    "case, provider, status, headers, body, scope, kind, until",
    [
        ("caller 400", "gemini", 400, {}, "bad", None, None, None),
        ("caller 404", "groq", 404, {}, "model not found", None, None, None),
        ("caller 413", "groq", 413, {}, "too large", None, None, None),
        ("caller 422", "cohere", 422, {}, "", None, None, None),
        ("5xx left to litellm", "gemini", 500, {}, "", None, None, None),
        ("503 left to litellm", "groq", 503, {"retry-after": "5"}, "", None, None, None),
        ("no status", "gemini", None, {}, "", None, None, None),
        ("bool status", "gemini", True, {}, "", None, None, None),
        ("401 bad key", "groq", 401, {}, "Invalid API Key", "provider", "exhausted", NOW + 900),
        ("403 no access", "gemini", 403, {}, "PERMISSION_DENIED", "provider", "exhausted", NOW + 900),
        ("402 credit", "openrouter", 402, {}, "insufficient credits", "provider", "exhausted", NOW + 3600),
        ("gemini per day", "gemini", 429, {}, GEMINI_DAY, "upstream", "exhausted", ts("2026-11-12T08:00:00")),
        ("gemini per day as text", "gemini", 429, {}, GEMINI_DAY_TEXT, "upstream", "exhausted", ts("2026-11-12T08:00:00")),
        ("gemini per minute retryDelay", "gemini", 429, {}, GEMINI_MINUTE, "upstream", "cooldown", NOW + 41),
        ("gemini per minute retry-after", "gemini", 429, {"Retry-After": "9"}, GEMINI_MINUTE_NO_DELAY, "upstream", "cooldown", NOW + 9),
        ("gemini per minute nothing", "gemini", 429, {}, GEMINI_MINUTE_NO_DELAY, "upstream", "cooldown", NOW + 60),
        ("groq TPD hint", "groq", 429, {"retry-after": "410"}, GROQ_TPD, "upstream", "exhausted", NOW + 410.832),
        ("groq RPM retry-after", "groq", 429, {"retry-after": "2", "x-ratelimit-reset-requests": "2m59.56s"}, GROQ_RPM, "upstream", "cooldown", NOW + 2),
        ("groq RPM via litellm prefix", "groq", 429, {"llm_provider-retry-after": "3"}, GROQ_RPM, "upstream", "cooldown", NOW + 3),
        ("openrouter free per day reset", "openrouter", 429, {}, OR_DAY, "provider", "exhausted", NOW + 3600),
        ("openrouter free per day header", "openrouter", 429, {"X-RateLimit-Reset": str(int((NOW + 7200) * 1000))}, OR_DAY_NO_RESET, "provider", "exhausted", NOW + 7200),
        ("openrouter free per day estimated", "openrouter", 429, {}, OR_DAY_NO_RESET, "provider", "exhausted", ts("2026-11-13T00:00:00")),
        ("openrouter free per day far reset", "openrouter", 429, {"X-RateLimit-Reset": str(int((NOW + 60 * 86400) * 1000))}, OR_DAY_NO_RESET, "provider", "exhausted", ts("2026-11-13T00:00:00")),
        ("openrouter free per min", "openrouter", 429, {"retry-after": "20"}, OR_MIN, "provider", "cooldown", NOW + 20),
        ("cohere trial month", "cohere", 429, {}, COHERE_TRIAL, "provider", "exhausted", ts("2026-12-01T00:00:00")),
        ("generic daily text", "siliconflow", 429, {}, "daily quota exceeded", "upstream", "exhausted", ts("2026-11-13T00:00:00")),
        ("generic short retry-after", "jina", 429, {"Retry-After": "30"}, "slow down", "upstream", "cooldown", NOW + 30),
        ("generic long retry-after", "jina", 429, {"Retry-After": "7200"}, "slow down", "upstream", "exhausted", NOW + 7200),
        ("generic garbage retry-after", "jina", 429, {"Retry-After": "-1"}, "slow down", "upstream", "cooldown", NOW + 60),
        ("generic x-ratelimit-reset", "novita", 429, {"x-ratelimit-reset-requests": "45s"}, "", "upstream", "cooldown", NOW + 45),
        ("generic epoch-ms reset", "novita", 429, {"x-ratelimit-reset": str(int((NOW + 90) * 1000))}, "", "upstream", "cooldown", NOW + 90),
        ("bare 429", "deepseek", 429, {}, "", "upstream", "cooldown", NOW + 60),
        ("unicode body", "gemini", 429, {}, "配额已用尽 ١٢", "upstream", "cooldown", NOW + 60),
    ],
)
def test_classify_failure(case, provider, status, headers, body, scope, kind, until):
    block = classify_failure(provider, status, headers, body, NOW)
    if scope is None:
        assert block is None, case
        return
    assert block is not None, case
    assert (block.scope, block.kind) == (scope, kind), case
    assert block.until == pytest.approx(until), case
    assert block.until - NOW <= quota.MAX_TRUSTED_SECONDS
    assert block.reason


def test_estimated_resets_say_so():
    gem = classify_failure("gemini", 429, {}, GEMINI_DAY, NOW)
    assert "estimated" in gem.reason and "Pacific" in gem.reason
    other = classify_failure("groq", 429, {}, "you hit requests per day (RPD)", NOW)
    assert "estimated" in other.reason and other.until == ts("2026-11-13T00:00:00")
    assert "key" in classify_failure("groq", 401, {}, "", NOW).reason


def test_block_key():
    block = classify_failure("groq", 401, {}, "", NOW)
    assert block.key("groq", "groq:groq/x") == "provider:groq"
    block = classify_failure("groq", 429, {"retry-after": "1"}, "", NOW)
    assert block.key("groq", "groq:groq/x") == "groq:groq/x"
    assert block.key("groq", None) == "provider:groq"


def test_headers_object_without_items_is_ignored():
    assert classify_failure("x", 429, object(), "", NOW).kind == "cooldown"  # type: ignore[arg-type]


# ---------------------------------------------------------------- observed limits

GROQ_HEADERS = {
    "x-ratelimit-limit-requests": "14400",
    "x-ratelimit-remaining-requests": "14370",
    "x-ratelimit-reset-requests": "2m59.56s",
    "x-ratelimit-limit-tokens": "18000",
    "x-ratelimit-remaining-tokens": "17997",
    "x-ratelimit-reset-tokens": "7.66s",
}


def as_tuples(limits):
    return {(l.window, l.metric): (l.limit, l.remaining, None if l.reset_at is None else round(l.reset_at - NOW, 2)) for l in limits}


@pytest.mark.parametrize(
    "provider, headers, expected",
    [
        ("groq", GROQ_HEADERS, {("day", "requests"): (14400, 14370, 179.56), ("minute", "tokens"): (18000, 17997, 7.66)}),
        ("openai_like", GROQ_HEADERS, {("minute", "requests"): (14400, 14370, 179.56), ("minute", "tokens"): (18000, 17997, 7.66)}),
        ("groq", {f"llm_provider-{k}": v for k, v in GROQ_HEADERS.items()},
         {("day", "requests"): (14400, 14370, 179.56), ("minute", "tokens"): (18000, 17997, 7.66)}),
        ("openrouter", {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int((NOW + 7200) * 1000))},
         {("day", "requests"): (50, 0, 7200.0)}),
        ("openrouter", {"X-RateLimit-Limit": "20", "X-RateLimit-Remaining": "3", "X-RateLimit-Reset": str(int(NOW + 30))},
         {("minute", "requests"): (20, 3, 30.0)}),
        ("cerebras", {"x-ratelimit-limit-requests-day": "1000", "x-ratelimit-remaining-tokens-minute": "50"},
         {("day", "requests"): (1000, None, None), ("minute", "tokens"): (None, 50, None)}),
        ("ietf", {"RateLimit-Limit": "100", "RateLimit-Remaining": "99", "RateLimit-Reset": "30"}, {("minute", "requests"): (100, 99, 30.0)}),
        ("gemini", {"content-type": "application/json"}, {}),
        ("groq", {"x-ratelimit-limit-requests": "lots", "x-ratelimit-remaining-requests": "nan"}, {}),
        ("groq", None, {}),
    ],
)
def test_parse_limit_headers(provider, headers, expected):
    assert as_tuples(parse_limit_headers(provider, headers, NOW)) == expected
