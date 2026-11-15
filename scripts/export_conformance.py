#!/usr/bin/env python3
"""Export conformance vectors for the Rust core (core/) from the Python pure functions.

Why: core/ re-implements muse.quota, the pacing part of muse.ration and the
key part of muse.cache for the desktop and phone apps. It must give the same
answers as this package. This script calls the Python functions on a fixed
list of inputs and writes what they returned to core/conformance/<area>.json;
`cargo test` in core/ replays every vector, and tests/test_conformance_export.py
fails when the committed files no longer match what Python returns.

The permissions area is the exception to "what Python computes": tokens cannot be
reproduced byte for byte, so it replays fixed tokens (see the section "permissions").

Usage:
    python scripts/export_conformance.py            # rewrite core/conformance/
    python scripts/export_conformance.py --check    # exit 1 if a file is out of date
    python scripts/export_conformance.py --out DIR  # write somewhere else

File format: a JSON list, one vector per line:
    {"name": unique within the file, "fn": the function, "input": its arguments by name,
     "expected": {"ok": the return value} or {"err": the exception}}
Floats are JSON numbers (shortest round-trip form). A float JSON cannot hold is
{"$float": "nan" | "inf" | "-inf"}. None is null, a tuple is a list, a
dataclass is an object of its fields. Exceptions are named by their Python
class, except that every "this timestamp does not fit a datetime" failure
(ValueError, OverflowError or OSError, depending on the value and platform) is
"TimeOutOfRange".

The output is deterministic: no clock, no randomness, no dependence on the
machine's time zone or locale. Running it twice gives the same bytes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from muse import cache, quota, ration  # noqa: E402
from muse.records import vectors as records_vectors  # noqa: E402  (records: both sides are new, so Python is the reference)

DEFAULT_OUT = ROOT / "core" / "conformance"
TIME_ERRORS = (ValueError, OverflowError, OSError)


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NOW = ts("2026-11-12T07:00:00")  # 23:00 PST on 11 Nov, the clock of tests/test_quota.py
NAN, INF = float("nan"), float("inf")


# ---------------------------------------------------------------- encoding


def enc(value: Any) -> Any:
    """A Python value as JSON data (see the module docstring)."""
    if isinstance(value, float):
        if math.isnan(value):
            return {"$float": "nan"}
        if math.isinf(value):
            return {"$float": "inf" if value > 0 else "-inf"}
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: enc(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (list, tuple)):
        return [enc(item) for item in value]
    if isinstance(value, dict):
        return {str(key): enc(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    raise TypeError(f"cannot export {type(value).__name__}")


class Area:
    """The vectors of one output file."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.vectors: list[dict[str, Any]] = []
        self._names: set[str] = set()

    def add(self, fn: str, label: str, shown: dict[str, Any], call: Callable[[], Any], *, time_errors: bool = False) -> None:
        """Record `call()`; `shown` is the input as the Rust test will read it."""
        name = f"{fn}: {label}"
        if name in self._names:
            raise SystemExit(f"duplicate vector name in {self.name}: {name}")
        self._names.add(name)
        try:
            expected = {"ok": enc(call())}
        except RecursionError:
            raise
        except Exception as exc:  # the exception is the expected result
            kind = "TimeOutOfRange" if time_errors and isinstance(exc, TIME_ERRORS) else type(exc).__name__
            expected = {"err": kind}
        self.vectors.append({"name": name, "fn": fn, "input": enc(shown), "expected": expected})

    def render(self) -> str:
        lines = [json.dumps(v, ensure_ascii=True, allow_nan=False, separators=(", ", ": ")) for v in self.vectors]
        return "[\n" + ",\n".join(lines) + "\n]\n"


def label_of(value: Any) -> str:
    text = json.dumps(enc(value), ensure_ascii=True)
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------- shared inputs

# Instants around every way a day or month can end. Fractions test the
# microsecond rounding datetime.fromtimestamp applies before reading the date.
CALENDAR_INSTANTS: list[tuple[str, float]] = [
    ("epoch", 0.0),
    ("just before epoch", -1.0),
    ("half a second before epoch", -0.5),
    ("1969 with a fraction", -86400.5),
    ("test clock", NOW),
    ("test clock + 0.25", NOW + 0.25),
    ("utc midnight exactly", ts("2026-11-12T00:00:00")),
    ("one second before utc midnight", ts("2026-11-12T23:59:59")),
    ("rounds up into the next day", ts("2026-11-12T23:59:59") + 0.9999996),
    ("rounds down, same day", ts("2026-11-12T23:59:59") + 0.9999994),
    ("half a microsecond after midnight", ts("2026-11-13T00:00:00") + 0.0000005),
    ("end of january", ts("2026-01-31T23:59:59")),
    ("february, common year", ts("2026-02-28T12:00:00")),
    ("february, leap year", ts("2028-02-29T12:00:00")),
    ("century leap year", ts("2000-02-29T00:00:00")),
    ("century common year", ts("2100-02-28T23:59:59")),
    ("end of april", ts("2026-04-30T23:00:00")),
    ("end of year", ts("2026-12-31T23:00:00")),
    ("first second of the year", ts("2027-01-01T00:00:00")),
    ("pacific: summer", ts("2026-07-01T06:59:00")),
    ("pacific: summer, at local midnight", ts("2026-07-01T07:00:00")),
    ("pacific: winter, at local midnight", ts("2026-11-12T08:00:00")),
    ("pacific: winter, just after local midnight", ts("2026-11-12T08:00:01")),
    ("1995 (before the 2007 US rule)", ts("1995-04-10T12:00:00")),
    ("1995 october (before the 2007 US rule)", ts("1995-10-30T12:00:00")),
    ("2006 march (before the 2007 US rule)", ts("2006-03-20T12:00:00")),
    ("2038 rollover", 2147483648.0),
    ("year 2100", ts("2100-06-15T12:00:00")),
    ("year 1000", ts("1000-01-01T00:00:00")),
    ("last second of year 9999", 253402300799.0),
    ("last day of year 9999", 253402300799.0 - 3600),
    ("december 9999", 253402300799.0 - 20 * 86400),
    ("first second of year 1", -62135596800.0),
    ("before year 1", -62135596801.0),
    ("after year 9999", 253402300800.0),
    ("far future", 1e18),
    ("far past", -1e18),
    ("nan", NAN),
    ("infinity", INF),
    ("minus infinity", -INF),
]


def dst_sweep() -> list[tuple[str, float]]:
    """Hourly through both 2026 switch days (and the day either side), three-hourly
    for 2024, 2025, 2027 and 2030, plus the seconds around each switch."""
    out: list[tuple[str, float]] = []
    switches = {
        2024: ("2024-03-10", "2024-11-03"),
        2025: ("2025-03-09", "2025-11-02"),
        2026: ("2026-03-08", "2026-11-01"),
        2027: ("2027-03-14", "2027-11-07"),
        2030: ("2030-03-10", "2030-11-03"),
    }
    for year, days in switches.items():
        step = 1 if year == 2026 else 3
        for which, day in zip(("spring", "autumn"), days):
            base = ts(f"{day}T00:00:00")
            for hour in range(-18, 54, step):
                at = base + hour * 3600 + (1800 if hour % 2 else 0)
                out.append((f"dst {year} {which} {hour:+d}h", at))
            switch = base + (10 if which == "spring" else 9) * 3600
            for delta in (-1.0, -0.000001, 0.0, 0.000001, 1.0):
                out.append((f"dst {year} {which} switch {delta:+.6f}s", switch + delta))
            for local_midnight in (base + 7 * 3600, base + 8 * 3600, base + 31 * 3600, base + 32 * 3600):
                for delta in (-1.0, 0.0, 1.0):
                    out.append((f"dst {year} {which} midnight candidate {int(local_midnight - base) // 3600}h {delta:+.0f}s",
                                local_midnight + delta))
    return out


# ---------------------------------------------------------------- quota


def quota_vectors() -> Area:
    area = Area("quota")

    # --- parse_retry_after (tests/test_quota.py table first)
    retry_after: list[tuple[Any, float]] = [(v, NOW) for v in [
        "120", " 5 ", "0", "1.5", 7, "Thu, 12 Nov 2026 07:01:00 GMT", "Thu, 12 Nov 2026 06:00:00 GMT",
        "-5", "nan", "NaN", "inf", "1e3", "", None, "soon", "١٢", "１２", "12abc",
        str(32 * 86400), str(31 * 86400), "Mon, 32 Foo 2026 99:99:99 GMT",
        # numbers
        "0.5", "00012", "1.", ".5", "+5", " \t7\n", "\x1c5\x1f", "5 ", "　7　", "1_0", "0x10", "Infinity",
        "2678400.0", "2678400.5", "0.000001", "9" * 400, "1,5", "5 5", 1.5, 0, 2678400, 2678401, 1000.0, 1e22, 0.00001,
        True, "   ", "　",
        # HTTP-dates and the other shapes email.utils accepts
        "Thu, 12 Nov 2026 07:00:00 GMT", "Thu, 12 Nov 2026 07:00:01 GMT", "thu, 12 nov 2026 07:01:00 gmt",
        "12 Nov 2026 07:01:00 GMT", "12 Nov 2026 07:01:00", "12 Nov 2026 07:01", "Thu,12 Nov 2026 07:01:00 GMT",
        "Thursday, 12-Nov-26 07:01:00 GMT", "Thu Nov 12 07:01:00 2026", "Nov 12 2026 07:01:00", "Nov 12, 2026 07:01:00 GMT",
        "Thu, 12 November 2026 07:01:00 GMT", "Thu, 12 Nov 2026 07.01.30 GMT", "Thu, 12 Nov 2026 07.01 GMT",
        "12 nov 2026 07.01.30", "Thu, 12 Nov 2026 0701 GMT", "Thu, 12 Nov 2026 07:01:00:00 GMT",
        "Thu, 12 Nov 2026 08:01:00 +0100", "Thu, 12 Nov 2026 08:01:00+0100", "Wed, 11 Nov 2026 23:01:00 -0800",
        "Wed, 11 Nov 2026 23:01:00 PST", "Thu, 12 Nov 2026 03:01:00 EDT", "Thu, 12 Nov 2026 07:01:00 Z",
        "Thu, 12 Nov 2026 07:01:00 UT", "Thu, 12 Nov 2026 07:01:00 -0000", "Thu, 12 Nov 2026 07:01:00 +0000",
        "Thu, 12 Nov 2026 07:01:00 XYZ", "Thu, 12 Nov 2026 07:01:00 pſt", "Thu, 12 Nov 2026 12:46:00 +0545",
        "Thu, 12 Nov 2026 07:31:00 +0099", "Fri, 13 Nov 2026 07:00:00 +2359", "Fri, 13 Nov 2026 07:00:00 +2400",
        "Thu, 12 Nov 2026 07:01:00 -2400", "Thu, 12 Nov 2026 07:01:00 +9999", "Thu, 12 Nov 2026 07:01:00 +99999999999999999999",
        "Thu, 12 Nov 26 07:01:00 GMT", "Thu, 12 Nov 26 07:01 -0000", "12 Nov 69 07:01:00 GMT", "12 Nov 68 07:01:00 GMT",
        "Thu, 12 Nov 2026 07:01:60 GMT", "Thu, 12 Nov 2026 24:00:00 GMT", "Thu, 12 Nov 2026 07:60:00 GMT",
        "Tue, 31 Nov 2026 07:01:00 GMT", "Mon, 30 Nov 2026 07:01:00 GMT", "Sun, 13 Dec 2026 07:00:00 GMT",
        "Sun, 13 Dec 2026 07:00:01 GMT", "Fri, 31 Dec 9999 23:59:59 GMT", "Sat, 01 Jan 10000 00:00:00 GMT",
        "Mon, 01 Jan 0001 00:00:00 GMT", "Thu, 0 Nov 2026 07:01:00 GMT", "Thu, 12 Nov 99999999999999999999 07:01:00 GMT",
        "Thu, ١٢ Nov 2026 07:01:00 GMT", "Thu, 1_2 Nov 2026 07:01:00 GMT", "Thu, +12 Nov 2026 07:01:00 GMT",
        "Thu, 12 Nov 2026 07:01:00 ＋0000", "²Thu, 12 Nov 2026 07:01:00 GMT", "٣ Nov 2026 07:01:00 GMT",
        ",12 Nov 2026 07:01:00 GMT", "Thu, , 12 Nov 2026 07:01:00 GMT", "Thu,", "Thu, 12 Nov", "Thu, 12 Nov 2026",
        "Thu, 12-Nov-2026 07:01:00 GMT", "12-Nov-2026 07:01:00 GMT", "12--2026 07:01:00 GMT", "Nov 2026 07:01:00 GMT",
        "Thu, 12 Nov 2026, 07:01:00 GMT", "Thu, 12 Nov 07:01:00 2026 GMT", "Thu, 12 Nov GMT 07:01:00",
        "Thu, 12 Nov 2026 07:01:00 GMT", "Thu,\x1c12 Nov 2026\x1f07:01:00 GMT", "Thu, 12 Nᴏv 2026 07:01:00 GMT",
        "Thu, 12 Nov 2026 7:1:5 GMT", "Thu, 12 Nov 2026 07:01: GMT", "Thu, 12 Nov 2026 :01:00 GMT", "Thu, 12 Nov 2026 07::00 GMT",
        "Thu, 12 Nov 2026 07:01:00 GMT extra words", "today", "Thu", "2026-11-12T07:01:00Z", "@1794466860",
    ]]
    retry_after += [
        ("Thu, 12 Nov 2026 07:01:00 GMT", NOW + 0.25),
        ("Thu, 12 Nov 2026 07:01:00 GMT", NOW + 59.999999),
        ("Thu, 12 Nov 2026 07:01:00 GMT", NOW - 40 * 86400),
        ("Thu, 12 Nov 2026 07:01:00 GMT", 0.0),
        ("Thu, 01 Jan 1970 00:00:10 GMT", 0.0),
        ("Thu, 01 Jan 1970 00:00:10 GMT", -5.5),
        ("30", NAN),
        ("Thu, 12 Nov 2026 07:01:00 GMT", NAN),
        ("Thu, 12 Nov 2026 07:01:00 GMT", -INF),
    ]
    for i, (value, now) in enumerate(retry_after):
        area.add("parse_retry_after", f"{i:03d} {label_of(value)} at {label_of(now)}", {"value": value, "now": now},
                 lambda value=value, now=now: quota.parse_retry_after(value, now))

    # --- parse_duration
    durations: list[Any] = [
        "2m59.56s", "7.66s", "1h2m", "6ms", "41s", "1d", "30", "", "abc", "m", "-3s",
        "1d2h3m4s5ms", "1.5h", "90m", "5ms", "1m5ms", "1s5ms", "1s1m", "1ms1s", "2M59.56S", " 7.66s ", "1h 2m", "0s", "0",
        "0.0", "1.5.2s", ".5s", "5.s", "1e3", "١٢s", "١٢", "5µs", "5K", "ms", "s", "d", "h",
        "5mss", "5sm", "5ss", "1d1d", "1h1d", "3d12h", "1.25d", "0.5ms", "1000ms", "59.999s", "1m0.001s", "007s", "1_0s",
        "9" * 400, "9" * 400 + "d", "9" * 300 + "d", "5 s", "5s ", "\x1c5s\x1d", "5S", "5MS", "5Ms", "1H2M3S", "+5s", "5s5s",
        "12:30", "PT5S", "5sec", "5m ", "0d0h0m0s0ms", "1d0.5s", "2h30", "30m15", "1.s", "5..5s",
        None, 30, 0, 0.5, 1e-5, 1e16, 1e15, 123456.789, True, False, -3, -0.0, NAN, INF,
    ]
    for i, value in enumerate(durations):
        area.add("parse_duration", f"{i:03d} {label_of(value)}", {"value": value},
                 lambda value=value: quota.parse_duration(value))

    # --- parse_reset
    resets: list[tuple[Any, float]] = [(v, NOW) for v in [
        "7.66s", "30", str(int(NOW + 600)), str(int((NOW + 600) * 1000)), str(int(NOW - 50)),
        str(int((NOW + 40 * 86400) * 1000)), "garbage",
        "", None, "   ", "0", "0.0", "2678400", "2678401", "2678400.0001", "999999999", "1000000000", "999999999.9",
        "999999999999", "1000000000000", "1794466860.5", "1794466860500", "1794466860500.75", str(int(NOW)),
        str(int(NOW * 1000)), str(int(NOW + 31 * 86400)), str(int(NOW + 31 * 86400) + 1), str(int((NOW + 31 * 86400) * 1000)),
        str(int((NOW + 31 * 86400) * 1000) + 1), "2m59.56s", "1h", "31d", "31d1s", "744h", "1d2h", "6ms", "-5", "+5", "5e2",
        " 45s ", "45S", "٤٥", "9" * 400, "1" + "0" * 30, "nan", 45, 45.5, 1794466860, 1794466860000, 1e22, True,
    ]]
    resets += [
        ("30", 0.0), ("1000000000", 0.0), ("1000000000", 999999000.0), ("1000000000000", 999999000.0),
        ("999999999", 999990000.0), ("45s", -100.5), ("30", NOW + 0.123456), ("30", NAN), ("30", INF),
        (str(int(NOW + 600)), NAN),
    ]
    for i, (value, now) in enumerate(resets):
        area.add("parse_reset", f"{i:03d} {label_of(value)} at {label_of(now)}", {"value": value, "now": now},
                 lambda value=value, now=now: quota.parse_reset(value, now))

    # --- calendars
    instants = CALENDAR_INSTANTS + dst_sweep()
    for label, now in instants:
        area.add("next_midnight_pacific", label, {"now": now}, lambda now=now: quota.next_midnight_pacific(now), time_errors=True)
    for label, now in CALENDAR_INSTANTS:
        area.add("next_midnight_utc", label, {"now": now}, lambda now=now: quota.next_midnight_utc(now), time_errors=True)
        area.add("next_month_utc", label, {"now": now}, lambda now=now: quota.next_month_utc(now), time_errors=True)
        area.add("month_start", label, {"now": now}, lambda now=now: quota.month_start(now), time_errors=True)
        # %Y is not zero-padded on every platform for years below 1000; keep those out of the files.
        if not (now == now and -30610224000.0 > now >= -62135596800.0):
            area.add("iso", label, {"ts": now}, lambda now=now: quota.iso(now), time_errors=True)
    area.add("iso", "none", {"ts": None}, lambda: quota.iso(None))
    for month in range(1, 13):
        for year in (2026, 2028):
            now = ts(f"{year}-{month:02d}-15T10:30:00")
            area.add("next_month_utc", f"{year}-{month:02d}", {"now": now}, lambda now=now: quota.next_month_utc(now), time_errors=True)
            area.add("month_start", f"{year}-{month:02d}", {"now": now}, lambda now=now: quota.month_start(now), time_errors=True)
    for provider in ("gemini", "groq", None, "", "Gemini", "GEMINI", "gemini "):
        picks = CALENDAR_INSTANTS if provider in ("gemini", "groq") else CALENDAR_INSTANTS[4:8]
        for label, now in picks + (dst_sweep()[::7] if provider == "gemini" else []):
            area.add("day_start", f"{label_of(provider)} {label}", {"now": now, "provider_id": provider},
                     lambda now=now, provider=provider: quota.day_start(now, provider), time_errors=True)

    # --- classify_failure
    for case in classify_cases():
        name, provider, status, headers, body, now = case
        shown_headers = None if headers is None else [[k, v] for k, v in headers]
        area.add(
            "classify_failure", name,
            {"provider_id": provider, "status": status, "headers": shown_headers, "body_text": body, "now": now},
            lambda provider=provider, status=status, headers=headers, body=body, now=now: quota.classify_failure(
                provider, status, None if headers is None else PairMapping(headers), body, now),
            time_errors=True,
        )
    return area


class PairMapping:
    """Headers as an ordered list of (name, value) pairs with a Mapping's `.items()`.

    A dict cannot hold two names that differ only in case in a way JSON keeps
    in order for every reader, so vectors store pairs; normalise_headers only
    calls `.items()`.
    """

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        self._pairs = list(pairs)

    def items(self) -> list[tuple[str, Any]]:
        return list(self._pairs)

    def __bool__(self) -> bool:
        return bool(self._pairs)


def classify_cases() -> list[tuple[str, Any, Any, list[tuple[str, Any]] | None, Any, float]]:
    def h(mapping: dict[str, Any]) -> list[tuple[str, Any]]:
        return list(mapping.items())

    gemini_day = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})
    gemini_minute = gemini_day.replace("PerDay", "PerMinute")
    gemini_minute_no_delay = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}})
    gemini_day_text = "litellm.RateLimitError: VertexAIException - " + gemini_day
    groq_tpd = ("Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` "
                "on tokens per day (TPD): Limit 200000, Used 193487, Requested 7464. Please try again in 6m50.832s.")
    groq_rpm = "Rate limit reached for model `x` on requests per minute (RPM): Limit 30, Used 30. Please try again in 2s."
    or_day = json.dumps({"error": {
        "message": "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
        "code": 429, "metadata": {"headers": {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "0",
                                              "X-RateLimit-Reset": str(int((NOW + 3600) * 1000))}}}})
    or_day_no_reset = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-day", "code": 429}})
    or_min = json.dumps({"error": {"message": "Rate limit exceeded: free-models-per-min.", "code": 429}})
    cohere_trial = ("You are using a Trial key, which is limited to 1000 API calls / month. You can continue to use the "
                    "Trial key for free or upgrade to a Production key with higher rate limits")

    def minute_body(**extra: Any) -> str:
        return json.dumps({"error": {"details": [{"violations": [{"quotaId": "RequestsPerMinute"}]}, extra]}})

    def or_embedded(message: str, headers: Any) -> str:
        return json.dumps({"error": {"message": message, "metadata": {"headers": headers}}})

    def nested(depth: int, leaf: Any) -> Any:
        for _ in range(depth):
            leaf = {"x": leaf}
        return leaf

    ms = lambda seconds: str(int((NOW + seconds) * 1000))  # noqa: E731
    spring, autumn = ts("2026-03-08T09:30:00"), ts("2026-11-01T08:30:00")

    table: list[tuple[str, Any, Any, dict[str, Any] | list[tuple[str, Any]] | None, Any]] = [
        # tests/test_quota.py::test_classify_failure, in order
        ("caller 400", "gemini", 400, {}, "bad"),
        ("caller 404", "groq", 404, {}, "model not found"),
        ("caller 413", "groq", 413, {}, "too large"),
        ("caller 422", "cohere", 422, {}, ""),
        ("5xx left to litellm", "gemini", 500, {}, ""),
        ("503 left to litellm", "groq", 503, {"retry-after": "5"}, ""),
        ("no status", "gemini", None, {}, ""),
        ("bool status", "gemini", True, {}, ""),
        ("401 bad key", "groq", 401, {}, "Invalid API Key"),
        ("403 no access", "gemini", 403, {}, "PERMISSION_DENIED"),
        ("402 credit", "openrouter", 402, {}, "insufficient credits"),
        ("gemini per day", "gemini", 429, {}, gemini_day),
        ("gemini per day as text", "gemini", 429, {}, gemini_day_text),
        ("gemini per minute retryDelay", "gemini", 429, {}, gemini_minute),
        ("gemini per minute retry-after", "gemini", 429, {"Retry-After": "9"}, gemini_minute_no_delay),
        ("gemini per minute nothing", "gemini", 429, {}, gemini_minute_no_delay),
        ("groq TPD hint", "groq", 429, {"retry-after": "410"}, groq_tpd),
        ("groq RPM retry-after", "groq", 429, {"retry-after": "2", "x-ratelimit-reset-requests": "2m59.56s"}, groq_rpm),
        ("groq RPM via litellm prefix", "groq", 429, {"llm_provider-retry-after": "3"}, groq_rpm),
        ("openrouter free per day reset", "openrouter", 429, {}, or_day),
        ("openrouter free per day header", "openrouter", 429, {"X-RateLimit-Reset": ms(7200)}, or_day_no_reset),
        ("openrouter free per day estimated", "openrouter", 429, {}, or_day_no_reset),
        ("openrouter free per day far reset", "openrouter", 429, {"X-RateLimit-Reset": ms(60 * 86400)}, or_day_no_reset),
        ("openrouter free per min", "openrouter", 429, {"retry-after": "20"}, or_min),
        ("cohere trial month", "cohere", 429, {}, cohere_trial),
        ("generic daily text", "siliconflow", 429, {}, "daily quota exceeded"),
        ("generic short retry-after", "jina", 429, {"Retry-After": "30"}, "slow down"),
        ("generic long retry-after", "jina", 429, {"Retry-After": "7200"}, "slow down"),
        ("generic garbage retry-after", "jina", 429, {"Retry-After": "-1"}, "slow down"),
        ("generic x-ratelimit-reset", "novita", 429, {"x-ratelimit-reset-requests": "45s"}, ""),
        ("generic epoch-ms reset", "novita", 429, {"x-ratelimit-reset": ms(90)}, ""),
        ("bare 429", "deepseek", 429, {}, ""),
        ("unicode body", "gemini", 429, {}, "配额已用尽 ١٢"),
        # unclassified 429: "quota" or "limit", and no clock, waits out the day.
        # A bare 429 and a word that only looks like those stay at 60s.
        ("unclassified quota, no clock", "groq", 429, {}, "You exceeded your quota"),
        ("unclassified limit, no clock", "deepseek", 429, {}, "account limit reached"),
        ("unclassified quota is pacific for gemini", "gemini", 429, {}, "quota exceeded"),
        ("quota word with a clock stays short", "groq", 429, {}, "quota exceeded, please try again in 2s"),
        ("rate limit with retry-after stays short", "groq", 429, {"retry-after": "4"}, "rate limit reached"),
        ("unlimited is not the word limit", "x", 429, {}, "unlimited plan"),
        ("quotaId is not the word quota", "gemini", 429, {}, "quotaId: nope"),
        # the other tests in that file
        ("groq RPD text, estimated", "groq", 429, {}, "you hit requests per day (RPD)"),
        ("401 with no body", "groq", 401, {}, ""),
        ("429 retry-after 1", "groq", 429, {"retry-after": "1"}, ""),
        # status
        ("status 429.0 is not an int", "groq", 429.0, {"retry-after": "5"}, ""),
        ("status as text", "groq", "429", {"retry-after": "5"}, ""),
        ("status 0", "groq", 0, {}, ""),
        ("status negative", "groq", -429, {}, ""),
        ("status 408", "groq", 408, {"retry-after": "5"}, ""),
        ("status 200", "groq", 200, {}, ""),
        ("status 401 ignores retry-after", "groq", 401, {"retry-after": "5"}, groq_tpd),
        ("status 403 upper-case provider", "OpenRouter", 403, {}, ""),
        ("status 402 empty provider", "", 402, {}, None),
        ("status 401 unicode provider", "İstanbul-AI", 401, {}, ""),
        ("body none, headers none", "groq", 429, None, None),
        ("headers none with body", "groq", 429, None, groq_rpm),
        # header handling
        ("retry-after: later duplicate wins", "x", 429, [("Retry-After", "5"), ("retry-after", "9")], ""),
        ("retry-after: later duplicate wins, other order", "x", 429, [("retry-after", "9"), ("Retry-After", "5")], ""),
        ("retry-after: prefixed copy wins", "x", 429, [("llm_provider-retry-after", "3"), ("retry-after", "50")], ""),
        ("retry-after: prefixed copy wins, upper case", "x", 429, [("retry-after", "50"), ("LLM_Provider-Retry-After", "3")], ""),
        ("retry-after: name with spaces", "x", 429, [("  Retry-After\t", "12")], ""),
        ("retry-after: null value skipped", "x", 429, [("retry-after", "7"), ("Retry-After", None)], ""),
        ("retry-after: only null", "x", 429, [("retry-after", None)], ""),
        ("retry-after: empty", "x", 429, {"retry-after": ""}, ""),
        ("retry-after: zero", "x", 429, {"retry-after": "0"}, ""),
        ("retry-after: fraction", "x", 429, {"retry-after": "0.25"}, ""),
        ("retry-after: exactly an hour", "x", 429, {"retry-after": "3600"}, ""),
        ("retry-after: an hour and a bit", "x", 429, {"retry-after": "3600.5"}, ""),
        ("retry-after: 31 days", "x", 429, {"retry-after": "2678400"}, ""),
        ("retry-after: beyond 31 days", "x", 429, {"retry-after": "2678401"}, ""),
        ("retry-after: http date", "x", 429, {"retry-after": "Thu, 12 Nov 2026 07:05:00 GMT"}, ""),
        ("retry-after: http date in the past", "x", 429, {"retry-after": "Thu, 12 Nov 2026 06:00:00 GMT"}, ""),
        ("retry-after: http date two hours out", "x", 429, {"retry-after": "Thu, 12 Nov 2026 09:00:00 GMT"}, ""),
        ("retry-after: arabic digits fall through to reset", "x", 429, {"retry-after": "١٢", "x-ratelimit-reset": "20"}, ""),
        ("retry-after: garbage, hint in body", "x", 429, {"retry-after": "soon"}, "Please try again in 8s."),
        ("reset: several, latest wins", "x", 429,
         {"x-ratelimit-reset-requests": "45s", "x-ratelimit-reset-tokens": "7.5s", "ratelimit-reset": "50"}, ""),
        ("reset: ietf name", "x", 429, {"RateLimit-Reset": "30"}, ""),
        ("reset: in the past is ignored", "x", 429, {"x-ratelimit-reset": str(int(NOW - 5))}, ""),
        ("reset: now exactly is ignored", "x", 429, {"x-ratelimit-reset": str(int(NOW))}, ""),
        ("reset: over an hour is exhausted", "x", 429, {"x-ratelimit-reset-requests": "1h0m1s"}, ""),
        ("reset: exactly an hour is a cooldown", "x", 429, {"x-ratelimit-reset-requests": "1h"}, ""),
        ("reset: beyond 31 days is ignored", "x", 429, {"x-ratelimit-reset": ms(40 * 86400), "x-ratelimit-reset-tokens": "9s"}, ""),
        ("reset: garbage", "x", 429, {"x-ratelimit-reset": "tomorrow"}, ""),
        ("reset: name must start with the prefix", "x", 429, {"my-x-ratelimit-reset": "30", "x-ratelimit-remaining": "0"}, ""),
        ("reset: prefixed by litellm", "x", 429, {"llm_provider-x-ratelimit-reset-tokens": "12s"}, ""),
        # hints in the text
        ("hint: seconds", "x", 429, {}, "Please try again in 2s."),
        ("hint: without please", "x", 429, {}, "Try Again In 90S"),
        ("hint: hours and minutes is exhausted", "x", 429, {}, "rate limited, try again in 1h30m."),
        ("hint: exactly an hour", "x", 429, {}, "try again in 1h"),
        ("hint: milliseconds", "x", 429, {}, "try again in 250ms"),
        ("hint: bare number", "x", 429, {}, "try again in 15"),
        ("hint: trailing dots", "x", 429, {}, "try again in 3.5s..."),
        ("hint: beyond 31 days falls to default", "x", 429, {}, "try again in 9999h"),
        ("hint: 31 days", "x", 429, {}, "try again in 744h"),
        ("hint: not a duration", "x", 429, {}, "try again in soon"),
        ("hint: dots only", "x", 429, {}, "try again in ..."),
        ("hint: unparsable letters", "x", 429, {}, "try again in mmss"),
        ("hint: first occurrence wins", "x", 429, {}, "try again in 5s or try again in 50s"),
        ("hint: first parsable occurrence", "x", 429, {}, "try again in a while; try again in 50s"),
        ("hint: retry-after wins over hint", "x", 429, {"retry-after": "4"}, "try again in 50s"),
        ("hint: wins over reset header", "x", 429, {"x-ratelimit-reset": "4"}, "try again in 50s"),
        ("hint: kelvin sign in text", "x", 429, {}, "toKens per day exceeded"),
        # daily and monthly text
        ("daily: hint and retry-after, latest wins", "groq", 429, {"retry-after": "500"}, groq_tpd),
        ("daily: untrusted hint, estimated", "groq", 429, {}, "tokens per day (TPD) reached. Please try again in 9999h."),
        ("daily: zero hint, estimated", "groq", 429, {}, "tokens per day (TPD) reached. Please try again in 0s."),
        ("daily: retry-after only", "groq", 429, {"retry-after": "120"}, "requests per day (RPD)"),
        ("daily: gemini text is pacific", "gemini", 429, {}, "Daily Limit reached"),
        ("daily: per-day with hyphen", "x", 429, {}, "free-models-per-day"),
        ("daily: (tpd) marker", "x", 429, {}, "limit (TPD)"),
        ("daily: 'per  day' with two spaces is not daily", "x", 429, {}, "per  day"),
        ("monthly: generic", "x", 429, {}, "Monthly limit reached"),
        ("monthly: slash month needs limit or quota", "x", 429, {}, "1000 calls / month"),
        ("monthly: slash month with quota", "x", 429, {}, "quota: 1000 calls /month"),
        ("monthly: calls a month, december", "x", 429, {}, "limit of 1000 calls a month"),
        ("monthly: daily text wins over monthly", "x", 429, {}, "monthly quota and daily quota"),
        ("cohere: trial monthly", "Cohere", 429, {"retry-after": "5"}, cohere_trial),
        ("cohere: no trial word", "cohere", 429, {}, "1000 API calls / month"),
        ("cohere: trial without month", "cohere", 429, {}, "Trial key rate limit: 20 per minute"),
        # gemini details
        ("gemini: per day for another provider", "vertex_ai", 429, {}, gemini_day),
        ("gemini: per day on the spring switch day", "gemini", 429, {}, gemini_day),
        ("gemini: per day on the autumn switch day", "gemini", 429, {}, gemini_day),
        ("gemini: per day wins over per minute", "gemini", 429, {},
         json.dumps({"a": {"quotaId": "RequestsPerMinute"}, "b": {"quotaId": "RequestsPerDay"}})),
        ("gemini: quotaMetric names the day", "gemini", 429, {}, json.dumps({"quotaMetric": "requests_perday"})),
        ("gemini: quotaId is a list", "gemini", 429, {}, json.dumps({"quotaId": ["a", "RequestsPerDay"]})),
        ("gemini: quotaId is an object whose key names the day", "gemini", 429, {}, json.dumps({"quotaId": {"PerDay": 1}})),
        ("gemini: quotaId is a number", "gemini", 429, {}, json.dumps({"quotaId": 5, "quotaMetric": None})),
        ("gemini: quotaId escaped", "gemini", 429, {}, '{"quotaId": "Requests\\u0050erDay"}'),
        ("gemini: nine levels down is not walked", "gemini", 429, {}, json.dumps(nested(9, {"quotaMetric": "PerDay"}))),
        ("gemini: eight levels down is walked", "gemini", 429, {}, json.dumps(nested(8, {"quotaMetric": "PerDay"}))),
        ("gemini: item 51 of a list is not walked", "gemini", 429, {},
         json.dumps({"v": [{"n": i} for i in range(50)] + [{"quotaMetric": "PerDay"}]})),
        ("gemini: item 50 of a list is walked", "gemini", 429, {},
         json.dumps({"v": [{"n": i} for i in range(49)] + [{"quotaMetric": "PerDay"}]})),
        ("gemini: second object in the text", "gemini", 429, {}, 'error {"a": 1} then {"quotaId": "x-PerDay"} end'),
        ("gemini: object after broken braces", "gemini", 429, {}, 'bad { { {"quotaMetric": "PerMinute", "retryDelay": "3s"}'),
        ("gemini: object 21 is not scanned", "gemini", 429, {}, "{ " * 20 + '{"quotaMetric": "PerDay"}'),
        ("gemini: object 20 is scanned", "gemini", 429, {}, "{ " * 19 + '{"quotaMetric": "PerDay"}'),
        ("gemini: python repr text, minute", "gemini", 429, {},
         "{'error': {'details': [{'quotaId': 'GenerateRequestsPerMinutePerProject', 'retryDelay': '12s'}]}}"),
        ("gemini: python repr text, day", "gemini", 429, {}, "{'quotaId' : 'GenerateRequestsPerDayPerProject'}"),
        ("gemini: text quotaid closed before the word", "gemini", 429, {}, "quotaId: 'x' and then PerDay"),
        ("gemini: text quotaid without quotes", "gemini", 429, {}, "quotaId: RequestsPerDay"),
        ("gemini: text quotaid with odd spacing", "gemini", 429, {}, 'QUOTAID" :\x1c "Requests PerDay'),
        ("gemini: text retrydelay needs the s", "gemini", 429, {}, "{'quotaId': 'PerMinute', 'retryDelay': '12'}"),
        ("gemini: text retrydelay malformed number", "gemini", 429, {}, "{'quotaId': 'PerMinute', 'retryDelay': '1.2.3s'}"),
        ("gemini: text retrydelay second match", "gemini", 429, {},
         "{'quotaId': 'PerMinute', 'retryDelay': 'soon', 'x': {'retryDelay': '7s'}}"),
        ("gemini: json with a raw newline in a string", "gemini", 429, {}, '{"quotaMetric": "Per\nDay", "quotaId": "PerMinute"}'),
        ("gemini: json with NaN and Infinity", "gemini", 429, {},
         '{"a": NaN, "b": -Infinity, "c": Infinity, "quotaMetric": "PerMinute", "retryDelay": "2s"}'),
        ("gemini: trailing comma is not json", "gemini", 429, {}, '{"quotaMetric": "PerMinute", "retryDelay": "2s",}'),
        ("gemini: single quotes are not json", "gemini", 429, {}, "{'quotaMetric': 'PerMinute'}"),
        ("gemini: leading zero is not json", "gemini", 429, {}, '{"n": 01, "quotaMetric": "PerMinute"}'),
        ("gemini: bad escape is not json", "gemini", 429, {}, '{"n": "\\x41", "quotaMetric": "PerMinute"}'),
        ("gemini: surrogate pair escape", "gemini", 429, {}, '{"n": "\\ud83d\\ude00", "quotaMetric": "PerMinute", "retryDelay": "2s"}'),
        ("gemini: lone surrogate escape", "gemini", 429, {}, '{"n": "\\ud83d", "quotaMetric": "PerMinute \\udc00", "retryDelay": "2s"}'),
        ("gemini: high surrogate then bad escape", "gemini", 429, {}, '{"n": "\\ud83d\\uZZZZ", "quotaMetric": "PerMinute"}'),
        ("gemini: high surrogate then non-surrogate", "gemini", 429, {}, '{"n": "\\ud83d\\u0041", "quotaMetric": "PerMinute"}'),
        ("gemini: duplicate key, last value wins", "gemini", 429, {},
         '{"quotaMetric": "PerMinute", "retryDelay": "5s", "retryDelay": "9s"}'),
        ("gemini: unterminated", "gemini", 429, {}, '{"quotaMetric": "PerMinute"'),
        ("gemini: 200 levels deep", "gemini", 429, {}, json.dumps(nested(200, 1))[:-1] + ', "quotaMetric": "PerMinute"}'),
        ("gemini: huge exponent", "gemini", 429, {}, '{"n": 1e999, "m": -1E-999, "quotaMetric": "PerMinute", "retryDelay": 1e2}'),
        ("gemini: 5000-digit integer is not json", "gemini", 429, {}, '{"n": ' + "7" * 5000 + ', "quotaMetric": "PerMinute"}'),
        ("gemini: 4300-digit integer is json", "gemini", 429, {}, '{"n": -' + "7" * 4300 + ', "quotaMetric": "PerMinute"}'),
        ("gemini minute: retryDelay text", "gemini", 429, {}, minute_body(retryDelay="1.5s")),
        ("gemini minute: retryDelay integer", "gemini", 429, {}, minute_body(retryDelay=41)),
        ("gemini minute: retryDelay float", "gemini", 429, {}, minute_body(retryDelay=2.5)),
        ("gemini minute: retryDelay small float", "gemini", 429, {"retry-after": "9"}, minute_body(retryDelay=0.00001)),
        ("gemini minute: retryDelay negative", "gemini", 429, {"retry-after": "9"}, minute_body(retryDelay=-3)),
        ("gemini minute: retryDelay null falls to retry-after", "gemini", 429, {"retry-after": "9"}, minute_body(retryDelay=None)),
        ("gemini minute: retryDelay true", "gemini", 429, {}, minute_body(retryDelay=True)),
        ("gemini minute: retryDelay list", "gemini", 429, {"retry-after": "9"}, minute_body(retryDelay=["5s"])),
        ("gemini minute: retryDelay garbage, text has one", "gemini", 429, {}, minute_body(retryDelay="soon", note="'retryDelay': '6s'")),
        ("gemini minute: retryDelay over an hour is capped", "gemini", 429, {}, minute_body(retryDelay="7200s")),
        ("gemini minute: retryDelay zero", "gemini", 429, {}, minute_body(retryDelay="0s")),
        ("gemini minute: later retryDelay wins", "gemini", 429, {}, minute_body(retryDelay="5s", more={"retryDelay": "8s"})),
        ("gemini minute: long retry-after is capped", "gemini", 429, {"retry-after": "7200"}, gemini_minute_no_delay),
        ("gemini minute: other provider", "x", 429, {}, gemini_minute),
        # openrouter
        ("openrouter day: reset in the past, estimated", "openrouter", 429, {"x-ratelimit-reset": ms(-5)}, or_day_no_reset),
        ("openrouter day: reset now, estimated", "openrouter", 429, {"x-ratelimit-reset": ms(0)}, or_day_no_reset),
        ("openrouter day: header wins over embedded", "openrouter", 429, {"x-ratelimit-reset": ms(600)}, or_day),
        ("openrouter day: empty header falls to embedded", "openrouter", 429, {"x-ratelimit-reset": ""}, or_day),
        ("openrouter day: embedded reset is a number", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"X-RateLimit-Reset": int((NOW + 1800) * 1000)})),
        ("openrouter day: embedded reset is a float", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"X-RateLimit-Reset": (NOW + 1800) * 1000 + 0.5})),
        ("openrouter day: embedded reset is 1e12-form float", "openrouter", 429, {},
         '{"error": {"message": "free-models-per-day", "metadata": {"headers": {"X-RateLimit-Reset": 1.7944704e12}}}}'),
        ("openrouter day: embedded reset in exponent form", "openrouter", 429, {},
         '{"error": {"message": "free-models-per-day", "metadata": {"headers": {"X-RateLimit-Reset": 1.7944704e21}}}}'),
        ("openrouter day: embedded reset is null", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"X-RateLimit-Reset": None, "X-RateLimit-Limit": "50"})),
        ("openrouter day: embedded reset is a list", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"X-RateLimit-Reset": [ms(600)]})),
        ("openrouter day: embedded headers without ratelimit names", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"X-Reset": ms(600)})),
        ("openrouter day: embedded llm_provider prefix", "openrouter", 429, {},
         or_embedded("free-models-per-day", {"x-ratelimit-reset": ms(600), "llm_provider-X-RateLimit-Reset": ms(900)})),
        ("openrouter day: later embedded headers win", "openrouter", 429, {},
         json.dumps({"message": "free-models-per-day", "a": {"headers": {"x-ratelimit-reset": ms(600)}},
                     "b": {"headers": {"x-ratelimit-limit": "50"}}})),
        ("openrouter day: upper-case provider", "OPENROUTER", 429, {}, or_day),
        ("openrouter day: other provider takes the generic path", "x", 429, {}, or_day),
        ("openrouter min: retry-after zero becomes the default", "openrouter", 429, {"retry-after": "0"}, or_min),
        ("openrouter min: reset header only", "openrouter", 429, {"x-ratelimit-reset": ms(25)}, or_min),
        ("openrouter min: reset in the past becomes the default", "openrouter", 429, {"x-ratelimit-reset": ms(-25)}, or_min),
        ("openrouter min: nothing", "openrouter", 429, {}, or_min),
        ("openrouter min: retry-after wins over reset", "openrouter", 429, {"retry-after": "4", "x-ratelimit-reset": ms(25)}, or_min),
        ("openrouter min: long retry-after stays a cooldown", "openrouter", 429, {"retry-after": "7200"}, or_min),
        ("openrouter: both texts, day wins", "openrouter", 429, {}, "free-models-per-min and free-models-per-day"),
        ("openrouter: ordinary 429", "openrouter", 429, {"retry-after": "6"}, "Rate limit exceeded"),
    ]
    out = []
    for name, provider, status, headers, body in table:
        now = spring if "spring switch" in name else autumn if "autumn switch" in name else NOW
        pairs = None if headers is None else (list(headers) if isinstance(headers, list) else h(headers))
        out.append((name, provider, status, pairs, body, now))
    # other clocks
    out += [
        ("clock: bare 429 at the epoch", "x", 429, [], "", 0.0),
        ("clock: bare 429 with a fractional clock", "x", 429, [("retry-after", "0.1")], "", NOW + 0.7),
        ("clock: daily at the end of the year", "x", 429, [], "daily limit", ts("2026-12-31T23:59:59")),
        ("clock: monthly in december", "x", 429, [], "monthly limit", ts("2026-12-15T00:00:00")),
        ("clock: monthly in february of a leap year", "x", 429, [], "monthly quota", ts("2028-02-29T23:59:59")),
        ("clock: cohere trial in december", "cohere", 429, [], cohere_trial, ts("2026-12-31T12:00:00")),
        ("clock: gemini daily in summer", "gemini", 429, [], "requests per day", ts("2026-07-01T06:59:00")),
        ("clock: 401 with a nan clock", "x", 401, [], "", NAN),
        ("clock: bare 429 with a nan clock", "x", 429, [], "", NAN),
        ("clock: gemini daily beyond year 9999", "gemini", 429, [], gemini_day, 1e18),
        ("clock: daily text with a nan clock", "x", 429, [], "daily limit", NAN),
        ("clock: monthly in december 9999", "x", 429, [], "monthly limit", 253402300799.0),
        ("clock: bare 429 beyond year 9999 needs no calendar", "x", 429, [], "", 1e18),
    ]
    return out


# ---------------------------------------------------------------- ration (pacing)


def limit_input(lim: ration.Limit) -> dict[str, Any]:
    return {f.name: getattr(lim, f.name) for f in dataclasses.fields(lim)}


def settings_input(s: ration.Settings) -> dict[str, Any]:
    """The Settings fields the Rust struct mirrors (the Decimal paid cap, tz and warnings are not ported yet)."""
    skip = {"paid_cap_usd", "warnings"}
    return {f.name: getattr(s, f.name) for f in dataclasses.fields(s) if f.name not in skip}


def day_limit(remaining: int, hours_to_reset: float, limit: int = 100, now: float = NOW, metric: str = "requests") -> ration.Limit:
    reset = now + hours_to_reset * 3600
    return ration.Limit("day", metric, limit, remaining, reset, "header", reset - 86400, "")


def ration_vectors() -> Area:
    area = Area("ration")
    sweep = dst_sweep()

    # --- previous_midnight_pacific
    for label, now in CALENDAR_INSTANTS + sweep:
        area.add("previous_midnight_pacific", label, {"now": now}, lambda now=now: ration.previous_midnight_pacific(now),
                 time_errors=True)

    # --- window_bounds, declared
    def bounds(label: str, window: Any, provider: Any, now: float, reset_at: Any = None, observed: bool = False) -> None:
        area.add("window_bounds", label,
                 {"window": window, "provider_id": provider, "now": now, "reset_at": reset_at, "observed": observed},
                 lambda: ration.window_bounds(window, provider, now, reset_at=reset_at, observed=observed), time_errors=True)

    declared = [
        ("minute", "groq", ts("2026-11-12T07:00:30")), ("day", "groq", ts("2026-11-12T07:00:00")),
        ("day", "groq", ts("2026-11-12T00:00:00")), ("day", "groq", ts("2026-11-12T23:59:59")),
        ("month", "cohere", ts("2026-02-14T00:00:00")), ("month", "cohere", ts("2028-02-14T00:00:00")),
        ("month", "cohere", ts("2026-04-30T23:00:00")), ("month", "cohere", ts("2026-12-31T12:00:00")),
        ("month", "cohere", ts("2026-01-01T00:00:00")),
        ("minute", "groq", NOW + 59.999), ("minute", "groq", NOW + 60), ("minute", "groq", 0.0), ("minute", "groq", -0.5),
        ("minute", "groq", -60.0), ("minute", "groq", -61.25), ("minute", "gemini", 1e18), ("minute", "groq", NAN),
        ("minute", "groq", INF), ("minute", "groq", 1794466812.3456789),
        ("week", "groq", NOW), ("", "groq", NOW), ("Day", "groq", NOW), ("hour", "gemini", NOW), ("days", "gemini", NOW),
        ("day", "Gemini", NOW), ("day", "", NOW), ("day", "gemini", NOW), ("month", "gemini", NOW),
        ("day", "groq", 1e18), ("month", "groq", 253402300799.0), ("day", "groq", 253402300799.0), ("day", "gemini", NAN),
        ("month", "groq", -62135596800.0), ("day", "gemini", -62135596800.0), ("day", "gemini", -62135596800.0 + 12 * 3600),
    ]
    for i, (window, provider, now) in enumerate(declared):
        bounds(f"declared {i:02d} {window!r} {provider!r} at {label_of(now)}", window, provider, now)
    # reset_at is ignored for a declared window
    bounds("declared ignores reset_at", "day", "groq", NOW, NOW + 5, False)
    for label, now in sweep:
        bounds(f"declared gemini day {label}", "day", "gemini", now)
    for label, now in CALENDAR_INSTANTS:
        bounds(f"declared gemini day {label}", "day", "gemini", now)
        bounds(f"declared utc day {label}", "day", "groq", now)
        bounds(f"declared month {label}", "month", "cohere", now)
        bounds(f"declared minute {label}", "minute", "cohere", now)

    # --- window_bounds, observed
    observed = [
        ("day", NOW + 5 * 3600), ("day", NOW + 86400), ("day", NOW + 86401), ("day", NOW - 1), ("minute", NOW + 30),
        ("minute", NOW + 180), ("month", NOW + 10 * 86400), ("week", NOW + 3600),
        ("day", None), ("minute", None), ("month", None), ("week", None),
        ("day", NOW), ("minute", NOW), ("month", NOW), ("minute", NOW + 60), ("minute", NOW + 60.000001),
        ("month", NOW + 30 * 86400), ("month", NOW + 30 * 86400 + 1), ("month", ts("2026-12-01T00:00:00")),
        ("month", ts("2027-01-01T00:00:00")), ("month", ts("2026-12-01T00:00:00") + 0.5), ("month", ts("2026-12-01T00:00:01")),
        ("day", ts("2026-12-01T00:00:00")), ("day", ts("2026-11-13T00:00:00")), ("day", NAN), ("day", INF), ("day", -INF),
        ("month", INF), ("month", 1e18), ("month", 253402300800.0), ("month", 253402300799.0), ("day", 1e18),
        ("Month", NOW + 5), ("", NOW + 5),
    ]
    for i, (window, reset_at) in enumerate(observed):
        bounds(f"observed {i:02d} {window!r} reset {label_of(reset_at)}", window, "x", NOW, reset_at, True)
    bounds("observed month on a calendar boundary", "month", "x", ts("2026-02-10T00:00:00"), ts("2026-03-01T00:00:00"), True)
    bounds("observed month boundary two months out", "month", "x", ts("2026-02-10T00:00:00"), ts("2026-04-01T00:00:00"), True)
    bounds("observed month boundary now", "month", "x", ts("2026-03-01T00:00:00"), ts("2026-03-01T00:00:00"), True)
    bounds("observed month boundary of january", "month", "x", ts("2026-12-31T23:59:59"), ts("2027-01-01T00:00:00"), True)
    bounds("observed month boundary of march, leap year", "month", "x", ts("2028-02-29T12:00:00"), ts("2028-03-01T00:00:00"), True)
    bounds("observed month boundary of year 1", "month", "x", -62135596800.0, -62135596800.0, True)
    bounds("observed gemini day uses the header, not pacific", "day", "gemini", NOW, NOW + 3600, True)
    bounds("observed with a nan clock", "day", "x", NAN, NOW, True)
    bounds("observed month boundary with a nan clock", "month", "x", NAN, ts("2026-12-01T00:00:00"), True)
    bounds("observed integer-valued reset", "minute", "x", 1794466800.0, 1794466830.0, True)

    # --- window_reset
    for window in ("minute", "day", "month", "week", "", "Minute"):
        for provider in ("gemini", "groq", "Gemini", ""):
            picks = CALENDAR_INSTANTS if (window, provider) in (("day", "gemini"), ("minute", "groq"), ("month", "groq")) \
                else CALENDAR_INSTANTS[:11]
            extra = sweep[::5] if (window, provider) == ("day", "gemini") else []
            for label, now in picks + extra:
                area.add("window_reset", f"{window!r} {provider!r} {label}", {"window": window, "provider_id": provider, "now": now},
                         lambda window=window, provider=provider, now=now: ration.window_reset(window, provider, now),
                         time_errors=True)

    # --- burn_rate
    burns = [
        (30, 5, 3600.0, 3), (30, 3, 3600.0, 3), (30, 2, 3600.0, 3), (0, 10, 3600.0, 3), (-5, 10, 3600.0, 3),
        (None, 5, 3600.0, 3), (30, None, 3600.0, 3), (None, None, 3600.0, 3), (30, 5, 0.0, 3), (30, 5, -1.0, 3),
        (30, 5, 60.0, 0), (30, 0, 60.0, 0), (30, 1, 60.0, -5), (30, 0, 60.0, -5), (30, 1, 60.0, 1), (12345, 7, 86400.0, 3),
        (1, 3, 3.0, 3), (7.5, 3, 3600.0, 3), (-0.0, 3, 3600.0, 3), (1e300, 3, 1e-300, 3), (10, 3, 1e-320, 3),
        (2 ** 53, 100000, 3600.0, 100000), (5, 5, INF, 3), (5, 5, NAN, 3), (NAN, 5, 60.0, 3), (INF, 5, 60.0, 3),
        (-INF, 5, 60.0, 3), (100, -1, 60.0, 3), (100, 3, 0.1, 3), (100, 10 ** 12, 3600.0, 10 ** 12),
    ]
    for i, (units, calls, window_s, min_calls) in enumerate(burns):
        area.add("burn_rate", f"{i:02d} {label_of([units, calls, window_s, min_calls])}",
                 {"units": units, "calls": calls, "window_s": window_s, "min_calls": min_calls},
                 lambda units=units, calls=calls, window_s=window_s, min_calls=min_calls:
                 ration.burn_rate(units, calls, window_s, min_calls))

    # --- limits for the pacing line
    month_reset = ts("2026-12-01T00:00:00")
    month_start = ts("2026-11-01T00:00:00")
    limits: list[tuple[str, ration.Limit]] = [
        ("full at window start", day_limit(100, 24)),
        ("half way, on the line", day_limit(50, 12)),
        ("half way, surplus", day_limit(60, 12)),
        ("half way, deficit", day_limit(40, 12)),
        ("at the reset, empty", day_limit(0, 0)),
        ("an hour left, a tenth left", day_limit(10, 1)),
        ("3.6 s to reset, full", day_limit(100, 0.001)),
        ("an hour left, empty", day_limit(0, 1)),
        ("just started, empty", day_limit(0, 23.5)),
        ("just started, one used", day_limit(99, 23.999)),
        ("thirds", day_limit(1, 16, limit=3)),
        ("large numbers", day_limit(123456789, 7.25, limit=1000000007)),
        ("tokens", day_limit(170000, 5, limit=200000, metric="tokens")),
        ("remaining above the limit", day_limit(150, 12)),
        ("remaining negative", day_limit(-5, 12)),
        ("limit one", day_limit(1, 12, limit=1)),
        ("limit zero", day_limit(0, 12, limit=0)),
        ("limit zero, some remaining", day_limit(5, 12, limit=0)),
        ("limit negative", day_limit(5, 12, limit=-10)),
        ("window not started yet", day_limit(80, 30)),
        ("window already over", day_limit(80, -2)),
        ("minute window", ration.Limit("minute", "requests", 30, 12, NOW + 20, "header", NOW - 40, "")),
        ("unknown window name", ration.Limit("week", "requests", 30, 12, NOW + 20, "header", NOW - 40, "")),
        ("empty window name", ration.Limit("", "requests", 30, 12, NOW + 20, "header", NOW - 40, "")),
        ("day, start unknown", ration.Limit("day", "requests", 100, 40, NOW + 3600, "header", None, "")),
        ("day, reset unknown", ration.Limit("day", "requests", 100, 40, None, "declared", NOW - 3600, "")),
        ("day, both unknown", ration.Limit("day", "requests", 100, 40, None, "declared")),
        ("day, reset equals start", ration.Limit("day", "requests", 100, 40, NOW, "header", NOW, "")),
        ("day, reset before start", ration.Limit("day", "requests", 100, 40, NOW - 10, "header", NOW + 10, "")),
        ("day, 23 hours long", ration.Limit("day", "requests", 250, 100, ts("2026-03-09T07:00:00"), "declared",
                                             ts("2026-03-08T08:00:00"), "provider:gemini")),
        ("day, 25 hours long", ration.Limit("day", "requests", 250, 100, ts("2026-11-02T08:00:00"), "declared",
                                             ts("2026-11-01T07:00:00"), "provider:gemini")),
        ("month, declared", ration.Limit("month", "requests", 1000, 700, month_reset, "declared", month_start, "provider:cohere")),
        ("month, nearly spent", ration.Limit("month", "requests", 1000, 3, month_reset, "declared", month_start, "provider:cohere")),
        ("month, untouched", ration.Limit("month", "tokens", 5000000, 5000000, month_reset, "declared", month_start, "")),
        ("month, rolling from a header", ration.Limit("month", "requests", 1000, 400, NOW + 3 * 86400, "header",
                                                        NOW - 27 * 86400, "cohere:command")),
        ("day, tiny span", ration.Limit("day", "requests", 10, 5, NOW + 0.000001, "header", NOW - 0.000001, "")),
        ("day, nan start", ration.Limit("day", "requests", 10, 5, NOW + 100, "header", NAN, "")),
        ("day, infinite reset", ration.Limit("day", "requests", 10, 5, INF, "header", NOW - 100, "")),
    ]
    clocks = [("", NOW), (" one hour later", NOW + 3600), (" at +0.5 s", NOW + 0.5)]
    rates: list[Any] = [None, 0.0, 0.001, 1.0, -0.5, 1e9, NAN]
    custom = dataclasses.replace(ration.Settings(), pace_margin=0.05, expiring_day_s=7200.0, expiring_month_s=3 * 86400.0)
    wide = dataclasses.replace(ration.Settings(), pace_margin=-0.25, expiring_day_s=0.0, expiring_month_s=0.0)
    setting_sets = [("defaults", ration.Settings()), ("custom", custom), ("wide", wide)]

    for label, lim in limits:
        for clock_label, now in clocks:
            for rate in (rates if not clock_label else rates[:3]):
                area.add("pace_position", f"{label}{clock_label}, rate {label_of(rate)}",
                         {"limit": limit_input(lim), "now": now, "rate": rate},
                         lambda lim=lim, now=now, rate=rate: ration.pace_position(lim, now, rate))
            for set_label, settings in setting_sets:
                area.add("pace_holds", f"{label}{clock_label}, {set_label}",
                         {"limit": limit_input(lim), "settings": settings_input(settings), "now": now},
                         lambda lim=lim, settings=settings, now=now: ration.pace_holds(lim, settings, now))
                for margin in ((settings.pace_margin,) if clock_label else (settings.pace_margin, 0.1, -0.1, 1.0)):
                    area.add("pace_release_at", f"{label}{clock_label}, {set_label}, margin {margin}",
                             {"limit": limit_input(lim), "margin": margin, "settings": settings_input(settings), "now": now},
                             lambda lim=lim, margin=margin, settings=settings, now=now:
                             ration.pace_release_at(lim, margin, settings, now))

    # The margin tolerance: surplus within 1e-12 of the margin does not hold.
    for i, margin in enumerate((0.1, 0.1 + 1e-13, 0.1 + 1e-11, 0.1 - 1e-13, 0.10000000000000002, 0.09999999999999999)):
        settings = dataclasses.replace(ration.Settings(), pace_margin=margin)
        lim = day_limit(60, 12)  # surplus 0.1 (as floats: 0.6 - 0.5)
        area.add("pace_holds", f"margin edge {i} ({margin!r})",
                 {"limit": limit_input(lim), "settings": settings_input(settings), "now": NOW},
                 lambda lim=lim, settings=settings: ration.pace_holds(lim, settings, NOW))

    # --- options_trailer / parse_options
    trailers: list[tuple[Any, bool, Any]] = [
        (NOW + 3600, True, 0.0123456), (NOW + 3600, False, None), (None, True, None), (None, False, 0.5), (None, False, None),
        (NOW, True, 0.0), (NOW + 0.999999, False, None), (NOW + 0.9999996, False, None), (0.0, False, None), (-1.0, False, None),
        (253402300799.0, False, None), (253402300800.0, True, 1.0), (NAN, True, 1.0), (INF, False, None), (1e18, False, None),
        (None, False, 0.0000005), (None, False, 0.0000015), (None, False, 0.0000025), (None, False, 1e-7), (None, False, 123456.789),
        (None, False, 1e21), (None, False, 1.7976931348623157e308), (None, False, -0.0), (None, False, -1.5), (None, False, NAN),
        (None, False, INF), (None, False, -INF), (None, True, 0.1), (None, False, 2.675), (None, False, 1), (None, False, 0.9999995),
        (ts("2026-03-08T10:00:00"), True, 12.5), (ts("2000-02-29T23:59:59"), False, None),
    ]
    for i, (wait_at, handoff, pay_usd) in enumerate(trailers):
        area.add("options_trailer", f"{i:02d} {label_of([wait_at, handoff, pay_usd])}",
                 {"wait_at": wait_at, "handoff": handoff, "pay_usd": pay_usd},
                 lambda wait_at=wait_at, handoff=handoff, pay_usd=pay_usd: ration.options_trailer(wait_at, handoff, pay_usd),
                 time_errors=True)
    messages = [
        "[muse] no trailer", "", "options=wait:2026-11-12T08:00:00Z", "[muse] x | options=none", "[muse] x | options=",
        "[muse] x | options=handoff", "[muse] x | options=wait:2026-11-12T08:00:00Z,handoff,pay:0.012346",
        "[muse] x | options=pay:0.500000", "[muse] x | options=wait:2026-11-12T08:00:00Z",
        "[muse] a | options=handoff | options=pay:1.5", "[muse] x | options=pay:1.5,pay:2.5,wait:a,wait:b",
        "[muse] x | options=handoff:yes,wait,pay:3", "[muse] x | options=HANDOFF,Wait:1,PAY:2", "[muse] x | options= handoff",
        "[muse] x | options=handoff \n", "[muse] x | options=wait:a, handoff", "[muse] x | options=handoff,,", "[muse] x |options=handoff",
        "[muse] x | options=pay:abc", "[muse] x | options=pay:", "[muse] x | options=pay", "[muse] x | options=handoff,pay:x,wait:y",
        "[muse] x | options=pay:1e3", "[muse] x | options=pay: 5 ", "[muse] x | options=pay:١٢.٥",
        "[muse] x | options=pay:1_0", "[muse] x | options=pay:1__0", "[muse] x | options=pay:_10", "[muse] x | options=pay:10_",
        "[muse] x | options=pay:1_0.2_5e1_0", "[muse] x | options=pay:nan", "[muse] x | options=pay:-NaN", "[muse] x | options=pay:inf",
        "[muse] x | options=pay:-Infinity", "[muse] x | options=pay:+iNf", "[muse] x | options=pay:infinit", "[muse] x | options=pay:.5",
        "[muse] x | options=pay:5.", "[muse] x | options=pay:5.e2", "[muse] x | options=pay:.e2", "[muse] x | options=pay:.",
        "[muse] x | options=pay:+.5E-3", "[muse] x | options=pay:--5", "[muse] x | options=pay:0x10", "[muse] x | options=pay:1e",
        "[muse] x | options=pay:1e+", "[muse] x | options=pay:1 0", "[muse] x | options=pay:1e400", "[muse] x | options=pay:-0",
        "[muse] x | options=pay:\x1c7\x1f", "[muse] x | options=pay:0.1", "[muse] x | options=pay:12345678901234567890",
        "[muse] x | options=pay:²", "[muse] x | options=pay:1,5", "[muse] x | options=pay:1d5", "[muse] x | options=pay:1.5f",
        "[muse] x | options=wait:été,handoff", "[muse] 配额 | options=wait:2026-11-12T08:00:00Z　",
        "[muse] x | options=wait:a:b:c",
    ]
    for i, message in enumerate(messages):
        area.add("parse_options", f"{i:02d} {label_of(message)}", {"message": message},
                 lambda message=message: ration.parse_options(message))
    return area


# ---------------------------------------------------------------- cache


def cache_vectors() -> Area:
    area = Area("cache")
    msg = [{"role": "user", "content": "hello"}]
    base = {"messages": msg, "temperature": 0}

    def cacheable(label: str, data: Any, deterministic: bool = False) -> None:
        """`data` is an object, or JSON text when the case needs what an object in this file cannot hold."""
        shown = {"data_json": data} if isinstance(data, str) else {"data": data}
        parsed = json.loads(data) if isinstance(data, str) else json.loads(json.dumps(data))
        area.add("cacheable", label, {**shown, "deterministic": deterministic},
                 lambda: cache.cacheable(parsed, deterministic=deterministic))

    def key(label: str, data: Any, alias: str = "fast", privacy: str = "standard", app: str = "standard") -> None:
        shown = {"data_json": data} if isinstance(data, str) else {"data": data}
        parsed = json.loads(data) if isinstance(data, str) else json.loads(json.dumps(data))
        area.add("cache_key", label, {"alias": alias, **shown, "privacy": privacy, "app_default_privacy": app},
                 lambda: cache.cache_key(alias, parsed, privacy, app))

    # tests/test_cache.py::test_cacheable, in order
    table = [
        ({"messages": msg, "temperature": 0}, False), ({"messages": msg, "temperature": 0.0}, False), ({"messages": msg}, True),
        ({"messages": msg}, False), ({"messages": msg, "temperature": 0.2}, True), ({"messages": msg, "temperature": "x"}, False),
        ({"messages": msg, "temperature": 0, "stream": True}, False),
        ({"messages": msg, "temperature": 0, "tools": [{"type": "function"}]}, False),
        ({"messages": msg, "temperature": 0, "n": 2}, False), ({"messages": msg, "temperature": 0, "n": 1}, False),
        ({"messages": msg, "temperature": 0, "logprobs": True}, False),
        ({"messages": msg, "temperature": 0, "stream": False, "tools": []}, False),
        ({"input": "x", "temperature": 0}, False), ({"messages": [], "temperature": 0}, False),
    ]
    for i, (data, deterministic) in enumerate(table):
        cacheable(f"table {i:02d}", data, deterministic)
    for i, messages in enumerate(("hi", {}, {"role": "user"}, None, 0, True, [None], [[]], [0])):
        cacheable(f"messages {i} {label_of(messages)}", {"messages": messages, "temperature": 0})
    cacheable("empty request", {})
    cacheable("empty request, deterministic", {}, True)
    for name in cache.NEVER:
        for i, value in enumerate((None, False, 0, 0.0, -0.0, [], {}, True, 1, "", "none", "auto", [0], {"a": None}, 0.5, -1)):
            cacheable(f"{name} = {label_of(value)} ({i})", {**base, name: value})
    cacheable("first never-key is reported", {**base, "top_logprobs": 5, "tools": [1], "stream": True})
    cacheable("never-key before n", {**base, "n": 3, "logprobs": True})
    cacheable("n before temperature", {"messages": msg, "temperature": 0.7, "n": 3})
    for i, n in enumerate((None, 1, 1.0, True, False, 0, 2, -1, "1", [1], 1.0000000000000002, {})):
        cacheable(f"n = {label_of(n)} ({i})", {**base, "n": n})
    temperatures: list[Any] = [
        None, 0, 0.0, -0.0, False, True, 1, 0.2, 1e-320, 5e-324, -1e-9, "0", "0.0", " 0 ", "-0", "+0.0", "0e5", ".0", "0.", "00",
        "0_0", "٠", "０.０", "1", "abc", "", " ", "0x0", "nan", "inf", "-inf", "1e400", "0,0", [], [0], {}, {"a": 0},
    ]
    for i, temperature in enumerate(temperatures):
        for deterministic in (False, True):
            cacheable(f"temperature = {label_of(temperature)} ({i}), deterministic {deterministic}",
                      {"messages": msg, "temperature": temperature}, deterministic)
    cacheable("temperature NaN literal", '{"messages": [1], "temperature": NaN}')
    cacheable("temperature -Infinity literal", '{"messages": [1], "temperature": -Infinity}')
    cacheable("temperature -0 integer", '{"messages": [1], "temperature": -0}')
    cacheable("temperature huge integer that fits a float", '{"messages": [1], "temperature": 1' + "0" * 300 + "}")
    cacheable("temperature integer too large for a float", '{"messages": [1], "temperature": 1' + "0" * 400 + "}")
    cacheable("temperature zero with 400 digits", '{"messages": [1], "temperature": 0.' + "0" * 400 + "}")
    cacheable("stream huge integer", '{"messages": [1], "temperature": 0, "stream": 1' + "0" * 400 + "}")
    cacheable("n is 1 with an exponent", '{"messages": [1], "temperature": 0, "n": 1e0}')
    cacheable("duplicate temperature, last wins", '{"messages": [1], "temperature": 1, "temperature": 0}')
    cacheable("duplicate messages, last wins", '{"messages": [1], "messages": [], "temperature": 0}')

    # tests/test_cache.py: what changes the key and what does not
    key("base", base)
    key("alias smart", base, alias="smart")
    key("other message", {"messages": [{"role": "user", "content": "hello!"}], "temperature": 0})
    key("max_tokens", {**base, "max_tokens": 10})
    key("seed", {**base, "seed": 1})
    key("stop", {**base, "stop": ["\n"]})
    key("response_format", {**base, "response_format": {"type": "json_object"}})
    key("privacy public", base, privacy="public")
    key("app privacy private", base, app="private")
    key("noise is ignored", {**base, "metadata": {"x": 1}, "user": "u", "litellm_call_id": "abc", "request_timeout": 5})
    # shape
    key("no messages", {"temperature": 0})
    key("empty request", {})
    key("messages null", {"messages": None})
    key("messages text", {"messages": "hello"})
    key("messages number", {"messages": 5})
    key("messages nested", {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"},
                                                                         {"type": "image_url", "image_url": {"url": "x", "detail": None}}]}]})
    key("key order inside messages does not matter (a)", {"messages": [{"role": "user", "content": "a", "name": "n"}]})
    key("key order inside messages does not matter (b)", {"messages": [{"name": "n", "content": "a", "role": "user"}]})
    key("message order matters", {"messages": [{"role": "user", "content": "b"}, {"role": "user", "content": "a"}]})
    key("every output parameter", {
        "messages": msg, "temperature": 0, "top_p": 0.9, "max_tokens": 256, "max_completion_tokens": 512, "stop": ["a", "b"],
        "seed": 42, "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}},
        "presence_penalty": -0.5, "frequency_penalty": 0.25, "logit_bias": {"50256": -100, "9": 1, "10": 2},
        "reasoning_effort": "low", "top_k": 40})
    key("null parameters are left out", {**base, "top_p": None, "seed": None, "stop": None})
    key("false and zero parameters are kept", {"messages": msg, "temperature": 0.0, "seed": 0, "stop": [], "top_p": False,
                                                 "logit_bias": {}, "reasoning_effort": ""})
    key("temperature 0 and 0.0 differ (int)", {"messages": msg, "temperature": 0})
    key("temperature 0 and 0.0 differ (float)", {"messages": msg, "temperature": 0.0})
    key("empty alias and classes", base, alias="", privacy="", app="")
    key("unicode alias and classes", base, alias="schön/快", privacy="privé", app="標準")
    # strings
    strings = [
        ("ascii punctuation", "a \"quoted\" \\ back/slash 'single' <tag> & %"), ("newline tab", "line1\nline2\ttab\r\n"),
        ("control characters", "\x00\x01\x08\x0b\x0c\x1f\x7f\x80\x9f"), ("latin", "café naïve ß ångström"),
        ("cjk", "你好，世界 日本語 한국어"), ("emoji", "\U0001f600 \U0001f468‍\U0001f469‍\U0001f467 ❤️"),
        ("combining and nfd", "é vs é, ṩ vs ṩ"), ("rtl", "مرحبا שלום"),
        ("line separators", "a b c\u0085d"), ("bom and specials", "﻿start � ￿ "),
        ("astral", "\U00010000 \U0010ffff \U0001d7ce"), ("empty", ""), ("spaces", "   "), ("escapes as text", "\\u00e9 \\n \\\""),
        ("long", "abc é中\U0001f600 " * 200),
    ]
    for label, text in strings:
        key(f"content: {label}", {"messages": [{"role": "user", "content": text}], "temperature": 0})
    key("unicode object keys sort by code point", {"messages": [{
        "～": 1, "\U0001f600": 2, "z": 3, "Z": 4, "é": 5, "a": 6, "": 7, "": 8, "퟿": 9, "aa": 10, "a\u0000": 11,
        "A": 12, "1": 13, "10": 14, "9": 15, "_": 16, " ": 17, "\x7f": 18}]})
    # numbers
    floats = [0.1, 0.5, 1.0, -1.0, 100.0, 1e15, 1e16, 1.5e16, 123456789012345680.0, 1e21, 1e22, 1.5e300, 1.7976931348623157e308,
              5e-324, 2.2250738585072014e-308, 1e-4, 0.0001234, 1e-5, 1.5e-5, 1e-7, 0.30000000000000004, 1 / 3, 2 / 3, 1e100,
              -0.0, 0.0, 3.141592653589793, 9007199254740993.0, 4.35, 0.000001, 123456.789e3, 1.0e-10, 999999999999999.9,
              9999999999999998.0, 99999999999999990000.0, 0.7, 2.5e-5]
    for i, x in enumerate(floats):
        key(f"float {i:02d} {x!r}", {"messages": msg, "temperature": x})
    key("floats in nested places", {"messages": [{"role": "user", "content": [0.1, 1e22, -0.0, 1e-7, 2.0]}],
                                    "logit_bias": {"1": 0.5, "2": -100.0}, "top_p": 0.95})
    ints = [0, 1, -1, 2 ** 31, 2 ** 53, 2 ** 53 + 1, 2 ** 63 - 1, -(2 ** 63), 2 ** 64 - 1]
    for i, n in enumerate(ints):
        key(f"int {i} {n}", {"messages": msg, "seed": n})
    key("booleans and null inside messages", {"messages": [True, False, None, {"a": True, "b": None}]})
    # JSON text: what only Python's reader keeps apart
    key("text: integer above u64", '{"messages": [1], "seed": 18446744073709551616}')
    key("text: integer below i64", '{"messages": [1], "seed": -9223372036854775809}')
    key("text: 40-digit integer", '{"messages": [1], "seed": 1234567890123456789012345678901234567890}')
    key("text: minus zero integer", '{"messages": [1], "seed": -0}')
    key("text: minus zero float", '{"messages": [1], "seed": -0.0}')
    key("text: exponent forms", '{"messages": [1E2, 1e-2, 1.50, 10e-1, 0e0, 1E+2, 123456789.123456789e-3]}')
    key("text: NaN and infinities", '{"messages": [NaN, Infinity, -Infinity], "top_p": NaN}')
    key("text: overflow to infinity", '{"messages": [1e999, -1e999, 1e-999]}')
    key("text: duplicate keys, last wins", '{"messages": [{"a": 1, "b": 2, "a": 3}], "seed": 1, "seed": 2}')
    key("text: whitespace and escapes", ' {\t"messages" : [ "\\u00e9\\n\\/\\b\\f\\r\\t\\"\\\\" , "\\ud83d\\ude00" ] ,\n"temperature":0 }\r\n')
    key("text: escaped controls round-trip", '{"messages": ["\\u0000\\u001f\\u007f\\u2028"]}')
    key("text: nested empties", '{"messages": [[], {}, [[]], [{}], {"a": {}}], "stop": {}, "logit_bias": []}')
    key("text: deep nesting", '{"messages": ' + "[" * 100 + "1" + "]" * 100 + "}")
    key("text: same as base", '{"temperature": 0, "messages": [{"content": "hello", "role": "user"}]}')
    return area


# ---------------------------------------------------------------- permissions
#
# This area is different in kind. The other areas export what Python computes from plain
# inputs. A permission token cannot be reproduced: every Biscuit block is signed with a
# fresh random key, so two runs never give the same bytes. Instead, the tokens were made
# once with test-only keys (core/conformance/fixtures/permission_tokens.json by the Rust
# core, permission_tokens_py.json by this script with --make-python-fixtures) and both
# implementations must read the same tokens the same way. Where a vector narrows or
# signs, each side does it itself and the description of the result is compared.
#
# biscuit-python is an optional extra. Without it this area cannot be recomputed; the
# committed file is passed through unchanged (tests/test_permissions.py recomputes it
# when the library is installed, and `cargo test` always replays it).

FIXTURES = ROOT / "core" / "conformance" / "fixtures"
PERMISSIONS_FILE = "permissions.json"
PT0 = 1_794_528_000  # 2026-11-13T00:00:00Z, the fixtures' clock
PDAY = 86_400


def permissions_available() -> bool:
    from muse.permissions import available

    return available()


def load_permission_fixtures() -> dict[str, Any]:
    rust = json.loads((FIXTURES / "permission_tokens.json").read_text(encoding="utf-8"))
    path = FIXTURES / "permission_tokens_py.json"
    py = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"tokens": {}, "revocations": {}}
    rust["tokens"] = {**rust["tokens"], **py["tokens"]}
    rust["revocations"] = {**rust["revocations"], **py["revocations"]}
    return rust


def permission_base_draft(issuer: str) -> dict[str, Any]:
    return {
        "issuer": issuer, "holder": "helper:vm", "actions": ["files.read", "files.write", "pay.spend"], "routine": [],
        "resources": [["folder", "/docs/work"], ["host", "api.example.com"]],
        "limits": [["requests", "", 0, "day", 200], ["spend", "TWD", 2, "day", 2000]],
        "purpose_text": "Weekly digest 每週摘要", "purpose_code": "", "issued_at": PT0, "not_before": PT0,
        "not_after": PT0 + 17 * PDAY, "confirm": ["each_write"], "privacy": "private", "period_zone": "Asia/Taipei",
    }


def make_python_fixtures() -> dict[str, Any]:
    """Tokens and entries made by the Python twin, for the Rust core to read. Run once."""
    from muse import permissions as P
    from muse.permissions import codec

    fx = json.loads((FIXTURES / "permission_tokens.json").read_text(encoding="utf-8"))
    root = P.parse_private_key(fx["root_private"])
    pub = fx["root_public"]
    tokens: dict[str, str] = {}
    base = P.issue(codec.draft_from_data(permission_base_draft(pub)), root)
    tokens["py_base"] = base.to_base64()
    narrowed = P.narrow(base, P.Narrowing(actions=("files.read",), resources=(P.Resource("folder", "/docs/work/sub"),)))
    tokens["py_base_narrowed"] = narrowed.to_base64()
    tokens["py_base_job"] = P.narrow(narrowed, P.Narrowing(job="job-7", privacy="local")).to_base64()
    rust_base = P.Mandate.parse(fx["tokens"]["base"], pub)
    approval = P.Narrowing(actions=("pay.spend",), resources=(P.Resource("host", "api.example.com"),),
                           caps=(P.Cap("spend", "TWD", 150),), not_after=PT0 + 300, card="card-py")
    tokens["py_card_on_rust_base"] = P.narrow(rust_base, approval).to_base64()
    chain = P.Mandate.parse(fx["tokens"]["rich_chain2"], pub)
    tokens["py_on_rust_chain2"] = P.narrow(chain, P.Narrowing(
        actions=("files.read",), limits=(P.NarrowLimit("spend", "TWD", "day", 900),), not_before=PT0 + 60,
        not_after=PT0 + 600, confirm=P.EACH_WRITE, tool="call-py")).to_base64()
    rich = P.Draft(
        issuer=pub, holder="device:phone-1", actions=("calendar.read", "llm.call", "llm.paid", "mail.send"), routine=("mail.send",),
        resources=(P.Resource("calendar", "家庭"), P.Resource("home", "home-1"), P.Resource("provider", "*")),
        limits=(P.Limit("spend", "USD", 2, "month", 500), P.Limit("messages", "", 0, "month", 20)),
        purpose_text="寄出每月提醒 (monthly reminders)", purpose_code="reminder.v1", issued_at=PT0 + 1, not_before=PT0,
        not_after=PT0 + 40 * PDAY, confirm=P.Confirm("over_amount", "USD", 2, 100), privacy="standard", period_zone="America/Los_Angeles")
    tokens["py_rich"] = P.issue(rich, root).to_base64()
    entries = {
        "py_base_root": P.RevocationEntry.sign(P.revocation_ids(base)[0], PT0 + 10, root).token,
        "py_rust_base_root_earliest": P.RevocationEntry.sign(P.revocation_ids(rust_base)[0], PT0 + 10, root).token,
        "py_narrowed_block": P.RevocationEntry.sign(P.revocation_ids(narrowed)[1], PT0 + 20, root).token,
    }
    return {
        "note": "Made once by scripts/export_conformance.py --make-python-fixtures with the test-only keys of "
                "permission_tokens.json. Token bytes differ on every run, so do not regenerate unless the schema changes.",
        "made_by": f"python biscuit-python {P.BISCUIT_PYTHON_VERSION}",
        "tokens": tokens,
        "revocations": entries,
    }


def permissions_vectors() -> Area:
    from muse import permissions as P
    from muse.permissions import codec

    fx = load_permission_fixtures()
    tokens, entries = fx["tokens"], fx["revocations"]
    pub, other_pub = fx["root_public"], fx["other_public"]
    area = Area("permissions")

    def add(fn: str, label: str, shown: dict[str, Any], call: Callable[[], Any]) -> None:
        name = f"{fn}: {label}"
        if name in area._names:
            raise SystemExit(f"duplicate vector name in permissions: {name}")
        area._names.add(name)
        try:
            expected = {"ok": enc(call())}
        except P.PermissionError as exc:
            if exc.code == "library_missing":
                raise
            expected = {"err": exc.code}
        area.vectors.append({"name": name, "fn": fn, "input": enc(shown), "expected": expected})

    def described(m: Any, lang: str) -> dict[str, Any]:
        d = P.describe(m, lang)
        return {"sentence": d.sentence, "lines": list(d.lines)}

    def mandate_data(m: Any, with_ids: bool = True) -> dict[str, Any]:
        out = {
            "id": m.id, "blocks": len(m.levels) + 1, "revocation_ids": P.revocation_ids(m),
            "draft": codec.draft_to_data(m.draft), "levels": [codec.narrowing_to_data(n) for n in m.levels],
            "effective": {
                "actions": list(m.actions()), "resources": [[r.kind, r.value] for r in m.resources()],
                "limits": [[l.kind, l.unit, l.decimals, l.period, l.amount] for l in m.limits()],
                "caps": [[c.kind, c.unit, c.amount] for c in m.caps()],
                "not_before": m.not_before(), "not_after": m.not_after(), "privacy": m.privacy(),
                "jobs": list(m.jobs()), "tools": list(m.tools()), "cards": list(m.cards()),
            },
            "en": described(m, "en"), "zh-TW": described(m, "zh-TW"),
        }
        if not with_ids:
            del out["revocation_ids"]
        return out

    # ---- propose: the rules for a draft
    def propose(label: str, **changes: Any) -> None:
        data = {**permission_base_draft(pub), **changes}

        def call() -> Any:
            d = P.propose_from_fields(codec.draft_from_data(data))
            return {"id": P.mandate_id(d), "draft": codec.draft_to_data(d)}

        add("permissions.propose", label, {"draft": data}, call)

    propose("base")
    propose("lists are sorted", actions=["pay.spend", "files.write", "files.read"],
            resources=[["host", "api.example.com"], ["folder", "/docs/work"]],
            limits=[["spend", "TWD", 2, "day", 2000], ["requests", "", 0, "day", 200]])
    propose("unicode purpose and paths", purpose_text="整理「每週」摘要 — résumé 😀 ‘ok’", resources=[["folder", "/文件/工作"], ["folder", "/Users/me/My Documents"], ["calendar", "家庭"]])
    propose("every resource kind", resources=[["folder", "/a"], ["host", "a.example:8443"], ["calendar", "work@group.calendar.example"], ["record_kind", "medication"], ["provider", "openrouter"], ["home", "home-1"]])
    propose("any is written out per kind", resources=[["folder", "*"], ["host", "*"], ["calendar", "*"], ["record_kind", "*"], ["provider", "*"], ["home", "*"]])
    propose("over_amount", confirm=["over_amount", "TWD", 2, 500])
    propose("never for reads", actions=["files.read", "llm.call"], limits=[], confirm=["never"])
    propose("never with routine writes", actions=["files.read", "mail.draft"], routine=["mail.draft"], limits=[], confirm=["never"])
    propose("usdc six decimals", actions=["pay.spend"], limits=[["spend", "USDC", 6, "day", 2000000], ["spend", "USDC", 6, "month", 30000000]], confirm=["over_amount", "USDC", 6, 500000])
    propose("unknown action name is allowed", actions=["x.custom_thing.v2"])
    propose("purpose code", purpose_code="TREAT")
    propose("empty purpose", purpose_text="")
    propose("largest time", not_after=253402300799)
    propose("largest amount", limits=[["spend", "TWD", 2, "day", 9223372036854775807]])
    propose("every holder kind: device", holder="device:phone-1")
    propose("every holder kind: app", holder="app:reader")
    propose("every holder kind: clinician", holder="clinician:dr.lin@hospital.example")
    for label, changes in [
        ("issuer too short", {"issuer": "ed25519/00"}), ("issuer upper-case hex", {"issuer": pub.upper().replace("ED25519", "ed25519")}),
        ("issuer other algorithm", {"issuer": "secp256r1/" + "0" * 64}),
        ("holder unknown kind", {"holder": "robot:x"}), ("holder no id", {"holder": "helper:"}), ("holder with space", {"holder": "helper:my vm"}),
        ("holder with quote", {"holder": 'helper:a"b'}),
        ("no actions", {"actions": []}), ("action not dotted", {"actions": ["read"]}), ("action upper case", {"actions": ["Files.Read"]}),
        ("action duplicate", {"actions": ["files.read", "files.read"]}), ("action empty part", {"actions": ["files..read"]}),
        ("action leading digit", {"actions": ["files.1read"]}), ("33 actions", {"actions": [f"a.b{i}" for i in range(33)]}),
        ("routine not an action", {"routine": ["mail.send"]}), ("routine duplicate", {"routine": ["files.read", "files.read"]}),
        ("no resources", {"resources": []}), ("resource unknown kind", {"resources": [["any", "*"]]}),
        ("folder root", {"resources": [["folder", "/"]]}), ("folder trailing slash", {"resources": [["folder", "/a/"]]}),
        ("folder relative", {"resources": [["folder", "docs"]]}), ("folder dot-dot", {"resources": [["folder", "/a/../b"]]}),
        ("folder dot", {"resources": [["folder", "/a/./b"]]}), ("folder double slash", {"resources": [["folder", "/a//b"]]}),
        ("folder with quote", {"resources": [["folder", '/a"b']]}), ("folder with backslash", {"resources": [["folder", "/a\\b"]]}),
        ("folder with newline", {"resources": [["folder", "/a\nb"]]}), ("folder with c1 control", {"resources": [["folder", "/a\u0085b"]]}),
        ("folder too long", {"resources": [["folder", "/" + "é" * 512]]}),
        ("host upper case", {"resources": [["host", "Example.com"]]}), ("host with path", {"resources": [["host", "example.com/x"]]}),
        ("host bad port", {"resources": [["host", "example.com:http"]]}), ("host leading dot", {"resources": [["host", ".example.com"]]}),
        ("host empty", {"resources": [["host", ""]]}), ("calendar empty", {"resources": [["calendar", ""]]}),
        ("resource duplicate", {"resources": [["folder", "/a"], ["folder", "/a"]]}),
        ("limit negative", {"limits": [["spend", "TWD", 2, "day", -1]]}), ("limit unknown period", {"limits": [["spend", "TWD", 2, "week", 1]]}),
        ("limit unknown kind", {"limits": [["tokens", "", 0, "day", 1]]}), ("spend without currency", {"limits": [["spend", "", 2, "day", 1]]}),
        ("currency lower case", {"limits": [["spend", "twd", 2, "day", 1]]}), ("requests with unit", {"limits": [["requests", "TWD", 0, "day", 1]]}),
        ("requests with decimals", {"limits": [["requests", "", 2, "day", 1]]}), ("decimals 19", {"limits": [["spend", "TWD", 19, "day", 1]]}),
        ("two decimals for one unit", {"limits": [["spend", "TWD", 2, "day", 1], ["spend", "TWD", 0, "month", 1]]}),
        ("same limit twice with different amounts", {"limits": [["spend", "TWD", 2, "day", 1], ["spend", "TWD", 2, "day", 2]]}),
        ("purpose with double quote", {"purpose_text": 'say "hi"'}), ("purpose with backslash", {"purpose_text": "a\\b"}),
        ("purpose with newline", {"purpose_text": "line\nbreak"}), ("purpose with tab", {"purpose_text": "a\tb"}),
        ("purpose 501 characters", {"purpose_text": "字" * 501}), ("purpose code with space", {"purpose_code": "a b"}),
        ("not_before equals not_after", {"not_after": PT0}), ("not_after before not_before", {"not_after": PT0 - 1}),
        ("time past year 9999", {"not_after": 253402300800}), ("negative time", {"not_before": -1}), ("negative issued_at", {"issued_at": -5}),
        ("over_amount without that limit", {"confirm": ["over_amount", "USD", 2, 1]}),
        ("over_amount with other decimals", {"confirm": ["over_amount", "TWD", 0, 1]}),
        ("over_amount negative", {"confirm": ["over_amount", "TWD", 2, -1]}),
        ("never with a write", {"confirm": ["never"]}), ("never with spend marked routine is allowed", {"routine": ["files.write", "pay.spend"], "confirm": ["never"]}),
        ("unknown confirm rule", {"confirm": ["sometimes"]}), ("unknown privacy", {"privacy": "secret"}),
        ("zone with space", {"period_zone": "Asia/Taipei x"}), ("zone empty", {"period_zone": ""}),
    ]:
        propose(label, **changes)
    propose("500 characters of purpose", purpose_text="字" * 500)

    # ---- issue: each side signs, then reads its own token back
    def issue(label: str, key: str, **changes: Any) -> None:
        data = {**permission_base_draft(pub), **changes}

        def call() -> Any:
            root = P.parse_private_key(key)
            m = P.issue(codec.draft_from_data(data), root)
            return mandate_data(P.Mandate.parse(m.to_base64(), P.public_key_text(root)), with_ids=False)

        add("permissions.issue", label, {"root_private": key, "draft": data}, call)

    issue("base", fx["root_private"])
    issue("unsorted input is signed in canonical order", fx["root_private"], actions=["pay.spend", "files.read", "files.write"])
    issue("unicode", fx["root_private"], purpose_text="整理「每週」摘要 😀", resources=[["folder", "/文件/工作"], ["calendar", "*"]], actions=["calendar.write", "files.read"])
    issue("other key signs its own draft", fx["other_private"], issuer=other_pub)
    issue("a draft for another root is refused", fx["other_private"])
    issue("an invalid draft is refused", fx["root_private"], holder="nobody")
    issue("a malformed key is refused", "ed25519-private/zz")
    issue("upper-case key hex", fx["root_private"].upper().replace("ED25519-PRIVATE", "ed25519-private"))

    # ---- read: every fixture token, as both sides must read it
    for name in sorted(tokens):
        add("permissions.read", name, {"token": tokens[name], "root": pub}, lambda name=name: mandate_data(P.Mandate.parse(tokens[name], pub)))
    add("permissions.read", "base under another root", {"token": tokens["base"], "root": other_pub}, lambda: mandate_data(P.Mandate.parse(tokens["base"], other_pub)))
    add("permissions.read", "other_root under its own key", {"token": tokens["other_root"], "root": other_pub}, lambda: mandate_data(P.Mandate.parse(tokens["other_root"], other_pub)))
    add("permissions.read", "malformed root key", {"token": tokens["base"], "root": "ed25519/xyz"}, lambda: mandate_data(P.Mandate.parse(tokens["base"], "ed25519/xyz")))

    # ---- check
    usage0 = [["requests", "", "day", 0], ["spend", "TWD", "day", 0]]

    def req(action: str = "files.read", resource: list[str] | None = None, *, holder: str = "helper:vm", privacy: str = "private",
            amounts: list[list[Any]] | None = None, job: str | None = None, tool: str | None = None, card: str | None = None) -> dict[str, Any]:
        return {"holder": holder, "action": action, "resource": resource or ["folder", "/docs/work/a.md"], "amounts": amounts or [],
                "privacy": privacy, "job": job, "tool": tool, "card": card}

    def twd(n: int) -> list[list[Any]]:
        return [["spend", "TWD", n]]

    def pay(n: int | None, **kw: Any) -> dict[str, Any]:
        return req("pay.spend", ["host", "api.example.com"], amounts=[] if n is None else twd(n), **kw)

    def spend_usage(day: int, month: int | None = None, requests: int = 0) -> list[list[Any]]:
        out = [["requests", "", "day", requests], ["spend", "TWD", "day", day]]
        return out + ([["spend", "TWD", "month", month]] if month is not None else [])

    def check(label: str, token: str, request: dict[str, Any], *, usage: list[list[Any]] | None = None, revoked: tuple[str, ...] = (),
              spent: list[list[Any]] | None = None, now: int = PT0 + 5, skew: int = 60, root: str = pub) -> None:
        shown = {"token": tokens[token], "root": root, "request": request, "usage": usage0 if usage is None else usage,
                 "revoked": [entries[e] for e in revoked], "spent": spent or [], "now": now, "skew": skew}

        def call() -> Any:
            revoked_list = P.RevocationList()
            for entry in shown["revoked"]:
                revoked_list.add_entry(entry, root)
            d = P.check_encoded(shown["token"], root, codec.request_from_data(request), {(k, u, p): n for k, u, p, n in shown["usage"]},
                                revoked_list, P.SpentSet.from_entries(shown["spent"]), now, skew)
            return [d.kind, d.detail]

        add("permissions.check", f"{token}: {label}", shown, call)

    end = PT0 + 17 * PDAY
    check("read inside the folder", "base", req())
    check("read the folder itself", "base", req(resource=["folder", "/docs/work"]))
    check("read deep inside", "base", req(resource=["folder", "/docs/work/a/b/c.md"]))
    check("a write asks", "base", req("files.write"))
    check("a payment asks", "base", pay(120))
    check("wrong holder", "base", req(holder="helper:other"))
    check("holder of another kind", "base", req(holder="app:vm"))
    for label, request in [
        ("malformed holder", req(holder="vm")), ("resource is any", req(resource=["folder", "*"])),
        ("path with dot-dot", req(resource=["folder", "/docs/work/../../etc"])), ("path with trailing slash", req(resource=["folder", "/docs/work/"])),
        ("relative path", req(resource=["folder", "docs/work"])), ("path with quote", req(resource=["folder", '/docs/work/a"b'])),
        ("unknown resource kind", req(resource=["bucket", "x"])), ("unknown privacy", req(privacy="secret")),
        ("negative amount", pay(-1)), ("amount twice", req("pay.spend", ["host", "api.example.com"], amounts=twd(1) + twd(2))),
        ("amount of unknown kind", req(amounts=[["tokens", "", 1]])), ("malformed action", req("Read")),
        ("job with a space", req(job="job 7")), ("card with a quote", req(card='c"')), ("host in upper case", req(resource=["host", "API.example.com"])),
    ]:
        check(f"bad request: {label}", "base", request)
    check("bad request: negative now", "base", req(), now=-1)
    for label, now, skew in [
        ("61 s before the start", PT0 - 61, 60), ("60 s before the start", PT0 - 60, 60), ("at the start", PT0, 60),
        ("60 s after the end", end + 60, 60), ("61 s after the end", end + 61, 60),
        ("no tolerance: 1 s early", PT0 - 1, 0), ("no tolerance: at the start", PT0, 0), ("no tolerance: at the end", end, 0),
        ("no tolerance: 1 s late", end + 1, 0), ("tolerance is capped at 300: 300 s early", PT0 - 300, 9999),
        ("tolerance is capped at 300: 301 s early", PT0 - 301, 9999), ("negative tolerance is none", PT0 - 1, -5),
        ("tolerance is capped at 300: 301 s late", end + 301, 100000),
    ]:
        check(f"time: {label}", "base", req(), now=now, skew=skew)
    check("action not granted", "base", req("mail.send"))
    for path in ["/docs/workshop", "/docs/work2", "/docs/wor", "/docs", "/Docs/Work/a", "/docs/work.bak/a", "/other", "/docs/work x"]:
        check(f"segment-aware prefix: {path}", "base", req(resource=["folder", path]))
    check("host exact", "base", req(resource=["host", "api.example.com"]))
    check("host with a port is another host", "base", req(resource=["host", "api.example.com:443"]))
    check("host suffix does not match", "base", req(resource=["host", "evil-api.example.com"]))
    check("host subdomain does not match", "base", req(resource=["host", "x.api.example.com"]))
    check("resource kind not granted", "base", req(resource=["calendar", "work"]))
    for privacy in ["local", "private", "standard", "public"]:
        check(f"privacy {privacy}", "base", req(privacy=privacy))
    check("payment without an amount", "base", pay(None))
    check("amount in a unit with no limit", "base", req("pay.spend", ["host", "api.example.com"], amounts=[["spend", "USD", 1]]))
    check("amount of a kind with no limit", "base", req(amounts=[["messages", "", 1]]))
    check("no usage figure", "base", pay(1), usage=[])
    check("usage figure for another period only", "base", pay(1), usage=[["spend", "TWD", "month", 0]])
    check("negative usage figure", "base", pay(1), usage=[["spend", "TWD", "day", -1]])
    check("exactly at the limit", "base", pay(120), usage=spend_usage(1880))
    check("one over the limit", "base", pay(120), usage=spend_usage(1881))
    check("limit already used up", "base", pay(1), usage=spend_usage(2000))
    check("zero amount when used up", "base", pay(0), usage=spend_usage(2000))
    check("usage that overflows", "base", pay(1), usage=spend_usage(9223372036854775807))
    check("amount above the whole limit", "base", pay(2001))
    check("read that counts one request, at the limit", "base", req(amounts=[["requests", "", 1]]), usage=spend_usage(0, requests=199))
    check("read that counts one request, over the limit", "base", req(amounts=[["requests", "", 1]]), usage=spend_usage(0, requests=200))
    check("read with a spend amount", "base", req(amounts=twd(5)))
    check("revoked at the root", "base", req(), revoked=("base_root",))
    check("revoked at the root, entry from the other implementation", "base", req(), revoked=("py_rust_base_root_earliest",))
    check("an unrelated revocation", "base", req(), revoked=("unrelated", "py_base_root"))
    check("a child's revocation does not reach the parent", "base", req(), revoked=("base_actions_block", "base_job_block"))
    check("revoked: a write would have asked", "base", req("files.write"), revoked=("base_root", "unrelated"))
    check("wrong root key", "base", req(), root=other_pub)
    check("signed by another key", "other_root", req())
    for name in ["not_base64", "empty", "truncated", "flipped_byte", "wrong_issuer", "bad_id", "extra_fact", "authority_check", "unsorted_authority",
                 "schema_2", "foreign_schema", "never_for_writes", "evil_facts", "evil_rules", "evil_check_true", "evil_unsorted", "evil_duplicate",
                 "evil_unknown_check", "evil_never", "evil_card_no_end", "evil_unlimited_unit", "evil_quote_job", "third_party"]:
        check("refused whatever is asked", name, req())
    check("a sealed token still verifies", "sealed", req())
    # widening attempts: the request each block was trying to unlock
    for name in ["evil_facts", "evil_rules", "evil_check_true"]:
        check("the write the block tried to add", name, req("files.write"))
        check("the folder the block tried to add", name, req(resource=["folder", "/etc/passwd"]))
    check("reads as two actions, really one odd one: read", "evil_quote_actions", req())
    check("reads as two actions, really one odd one: write", "evil_quote_actions", req("files.write"))
    check("reads as two actions, really one odd one: pay", "evil_quote_actions", pay(1))
    check("an action the parent has", "foreign_wider_action", req())
    check("an action only the block lists", "foreign_wider_action", req("mail.send"))
    check("an action the block dropped", "foreign_wider_action", req("files.write"))
    check("levels that share no action: read", "foreign_disjoint", req())
    check("levels that share no action: pay", "foreign_disjoint", pay(1))
    check("bound to two jobs: the first", "foreign_two_jobs", req(job="job-7"))
    check("bound to two jobs: the second", "foreign_two_jobs", req(job="job-8"))

    check("the action kept", "base_actions", req())
    check("the action dropped", "base_actions", req("files.write"))
    check("payment dropped", "base_actions", pay(1))
    check("revoked at its own block", "base_actions", req(), revoked=("base_actions_block",))
    check("revoked at the root", "base_actions", req(), revoked=("base_root_earlier",))
    check("a grandchild's revocation does not reach it", "base_actions", req(), revoked=("base_job_block",))
    for path, label in [("/docs/work/sub", "the folder itself"), ("/docs/work/sub/x.md", "inside"), ("/docs/work/x.md", "the parent's folder"),
                        ("/docs/work/subx", "a sibling with the same prefix"), ("/docs/work/sub2/x", "another sibling")]:
        check(label, "base_folder", req(resource=["folder", path]))
    check("the host the narrowing left out", "base_folder", req(resource=["host", "api.example.com"]))
    check("at the lower limit", "base_limit", pay(120), usage=spend_usage(380))
    check("over the lower limit, inside the parent's", "base_limit", pay(120), usage=spend_usage(381))
    check("a read is not measured against it", "base_limit", req(), usage=spend_usage(1999))
    check("a read needs no usage figure", "base_limit", req(), usage=[])
    check("new period: no figure for it", "base_limit_month", pay(1))
    check("new period: inside", "base_limit_month", pay(100), usage=spend_usage(0, 4900))
    check("new period: over", "base_limit_month", pay(101), usage=spend_usage(0, 4900))
    check("at the cap", "base_cap", pay(100))
    check("over the cap", "base_cap", pay(101))
    check("a read has no amount to cap", "base_cap", req())
    for label, now in [("61 s before its start", PT0 + PDAY - 61), ("60 s before its start", PT0 + PDAY - 60), ("60 s after its end", PT0 + 2 * PDAY + 60),
                       ("61 s after its end", PT0 + 2 * PDAY + 61), ("inside the parent's window only", PT0 + 5)]:
        check(label, "base_window", req(), now=now)
    check("private is no longer allowed", "base_local", req())
    check("local", "base_local", req(privacy="local"))
    check("no job", "base_job", req())
    check("the job", "base_job", req(job="job-7"))
    check("another job", "base_job", req(job="job-8"))
    check("the job, but a write", "base_job", req("files.write", job="job-7"))
    check("revoked at the grandparent's block", "base_job", req(job="job-7"), revoked=("base_actions_block",))
    check("revoked at its own block", "base_job", req(job="job-7"), revoked=("base_job_block",))
    check("job and tool", "base_job_tool", req(job="job-7", tool="call-19"))
    check("job without the tool", "base_job_tool", req(job="job-7"))
    check("another tool call", "base_job_tool", req(job="job-7", tool="call-20"))
    check("tool without the job", "base_job_tool", req(tool="call-19"))
    base_id = P.Mandate.parse(tokens["base"], pub).id
    check("replayed", "base_job_tool", req(job="job-7", tool="call-19"), spent=[[f"{base_id}/tool/call-19", PT0 + 660]])
    check("a spent entry that has lapsed", "base_job_tool", req(job="job-7", tool="call-19"), spent=[[f"{base_id}/tool/call-19", PT0 + 4]])
    check("another call's spent entry", "base_job_tool", req(job="job-7", tool="call-19"), spent=[[f"{base_id}/tool/call-18", PT0 + 660], [f"{base_id}/card/call-19", PT0 + 660]])
    check("after its ten minutes", "base_job_tool", req(job="job-7", tool="call-19"), now=PT0 + 661)
    check("single use must end within a day", "base_tool_long", req(tool="call-20"))
    check("single use: one second too long", "base_tool_long", req(tool="call-20"), now=PT0 + PDAY - 61)
    check("single use: just short enough", "base_tool_long", req(tool="call-20"), now=PT0 + PDAY - 60)
    check("shortening afterwards does not help", "base_tool_long_then_short", req(tool="call-20"))
    check("approved payment", "base_card", pay(120, card="card-3"))
    check("without the card", "base_card", pay(120))
    check("another card", "base_card", pay(120, card="card-4"))
    check("the other approval's card", "base_card_other", pay(120, card="card-3"))
    check("approved, but over the daily limit", "base_card", pay(2001, card="card-3"))
    check("approved, at the daily limit", "base_card", pay(2000, card="card-3"))
    check("approved, but not this action", "base_card", req(card="card-3"))
    check("approved, replayed", "base_card", pay(120, card="card-3"), spent=[[f"{base_id}/card/card-3", PT0 + 360]])
    check("approved, last second with tolerance", "base_card", pay(120, card="card-3"), now=PT0 + 360)
    check("approved, expired", "base_card", pay(120, card="card-3"), now=PT0 + 361)
    check("approved, then the family is revoked", "base_card", pay(120, card="card-3"), revoked=("base_root",))
    check("a card id on a token that is not bound to one is not an approval", "base", pay(120, card="card-3"))

    rich_usage = [["messages", "", "month", 0], ["requests", "", "day", 0], ["spend", "TWD", "day", 0], ["spend", "TWD", "month", 0]]

    def rpay(n: int, host: str = "pay.example.com:8443", **kw: Any) -> dict[str, Any]:
        return req("pay.spend", ["host", host], amounts=twd(n), **kw)

    check("payment at the threshold", "rich", rpay(550), usage=rich_usage)
    check("payment over the threshold", "rich", rpay(551), usage=rich_usage)
    check("payment needs both periods' figures", "rich", rpay(1), usage=usage0)
    check("payment over the month", "rich", rpay(100), usage=[["spend", "TWD", "day", 0], ["spend", "TWD", "month", 29901]])
    check("payment in another unit", "rich", req("pay.spend", ["host", "api.example.com"], amounts=[["spend", "USD", 1]]), usage=rich_usage)
    check("a write asks", "rich", req("files.write", ["folder", "/文件/工作/報告.md"]))
    check("a routine write does not ask", "rich", req("mail.draft", ["folder", "/文件/工作/草稿"]))
    check("a routine write that counts a message", "rich", req("mail.draft", ["folder", "/文件/工作"], amounts=[["messages", "", 1]]), usage=[["messages", "", "month", 19]])
    check("the 21st message", "rich", req("mail.draft", ["folder", "/文件/工作"], amounts=[["messages", "", 1]]), usage=[["messages", "", "month", 20]])
    check("any calendar", "rich", req("calendar.write", ["calendar", "家庭 2026"]))
    check("an unknown action is a write", "rich", req("x.custom", ["host", "api.example.com"]))
    check("unicode folder", "rich", req(resource=["folder", "/文件/工作/報告/十一月.md"]))
    check("unicode sibling with the same prefix", "rich", req(resource=["folder", "/文件/工作坊/a"]))
    check("folder with spaces", "rich", req(resource=["folder", "/Users/me/My Documents/a b.txt"]))
    check("folder with spaces: same prefix", "rich", req(resource=["folder", "/Users/me/My Documents2/a"]))
    check("a record kind", "rich", req(resource=["record_kind", "medication"]))
    check("another record kind", "rich", req(resource=["record_kind", "lab"]))
    check("privacy standard", "rich", req(resource=["folder", "/文件/工作/a"], privacy="standard"))
    check("privacy public", "rich", req(resource=["folder", "/文件/工作/a"], privacy="public"))
    check("stricter rule: a small payment asks", "rich_each", rpay(1), usage=rich_usage)
    check("stricter rule: routine stays routine", "rich_each", req("mail.draft", ["folder", "/文件/工作/a"]))
    check("lower threshold: at it", "rich_over_lower", rpay(100), usage=rich_usage)
    check("lower threshold: over it", "rich_over_lower", rpay(101), usage=rich_usage)
    check("inside the narrowed unicode folder", "rich_unicode", req(resource=["folder", "/文件/工作/報告/a"]))
    check("the parent's unicode folder", "rich_unicode", req(resource=["folder", "/文件/工作/a"]))
    check("any calendar is kept", "rich_unicode", req("calendar.write", ["calendar", "x"]))
    check("a host it kept", "rich_unicode", req(resource=["host", "pay.example.com:8443"]))
    check("a host it dropped", "rich_unicode", req(resource=["host", "api.example.com"]))
    check("level 1: standard is gone", "rich_chain1", req(resource=["folder", "/文件/工作/a"], privacy="standard"))
    check("level 1: mail.draft is gone", "rich_chain1", req("mail.draft", ["folder", "/文件/工作/a"]))
    check("level 2: lower day limit", "rich_chain2", rpay(500, "api.example.com"), usage=[["spend", "TWD", "day", 501], ["spend", "TWD", "month", 0]])
    check("level 2: lower month limit", "rich_chain2", rpay(500, "api.example.com"), usage=[["spend", "TWD", "day", 0], ["spend", "TWD", "month", 8501]])
    check("level 2: inside both", "rich_chain2", rpay(500, "api.example.com"), usage=[["spend", "TWD", "day", 500], ["spend", "TWD", "month", 8500]])
    chain_usage = [["spend", "TWD", "day", 0], ["spend", "TWD", "month", 0]]
    check("level 3: the approved payment", "rich_chain3", rpay(300, "api.example.com", job="job-7", card="card-9"), usage=chain_usage)
    check("level 3: over its cap", "rich_chain3", rpay(301, "api.example.com", job="job-7", card="card-9"), usage=chain_usage)
    check("level 3: without the job", "rich_chain3", rpay(300, "api.example.com", card="card-9"), usage=chain_usage)
    check("level 3: without the card", "rich_chain3", rpay(300, "api.example.com", job="job-7"), usage=chain_usage)
    check("level 3: a host level 2 dropped", "rich_chain3", rpay(300, job="job-7", card="card-9"), usage=chain_usage)
    check("level 3: an action it dropped", "rich_chain3", req("files.read", ["folder", "/文件/工作/報告/a"], job="job-7", card="card-9"))
    check("level 3: after 15 minutes", "rich_chain3", rpay(300, "api.example.com", job="job-7", card="card-9"), usage=chain_usage, now=PT0 + 961)

    quiet = dict(holder="app:reader", privacy="public")
    check("never: a routine write", "quiet", req("mail.draft", ["folder", "/anything/at all"], **quiet))
    check("never: a read anywhere", "quiet", req(resource=["folder", "/etc/hosts"], **quiet))
    check("never: any provider", "quiet", req("llm.call", ["provider", "openrouter"], **quiet))
    check("never: a kind it lacks", "quiet", req("llm.call", ["host", "api.example.com"], **quiet))
    check("never: the helper is not the holder", "quiet", req(resource=["folder", "/a"]))
    check("narrowed to one folder and one provider: inside", "quiet_sub", req(resource=["folder", "/a b/c"], **quiet))
    check("narrowed to one folder and one provider: outside", "quiet_sub", req(resource=["folder", "/a bc"], **quiet))
    check("narrowed to one folder and one provider: provider", "quiet_sub", req("llm.call", ["provider", "local-llm"], **quiet))
    check("narrowed to one folder and one provider: other provider", "quiet_sub", req("llm.call", ["provider", "openrouter"], **quiet))
    clin = dict(holder="clinician:dr-lin", privacy="local")
    check("reads a granted kind", "clinician", req("health.read", ["record_kind", "medication"], **clin))
    check("a kind not granted", "clinician", req("health.read", ["record_kind", "genome"], **clin))
    check("may not leave the home", "clinician", req("health.read", ["record_kind", "allergy"], holder="clinician:dr-lin", privacy="private"))
    check("after three days", "clinician", req("health.read", ["record_kind", "allergy"], **clin), now=PT0 + 3 * PDAY + 61)
    usdc_usage = [["spend", "USDC", "day", 0], ["spend", "USDC", "month", 0]]

    def upay(n: int, action: str = "pay.spend", host: str = "b.example") -> dict[str, Any]:
        return req(action, ["host", host], amounts=[["spend", "USDC", n]])

    check("half a USDC", "usdc", upay(500000), usage=usdc_usage)
    check("just over half a USDC", "usdc", upay(500001), usage=usdc_usage)
    check("over the day", "usdc", upay(1), usage=[["spend", "USDC", "day", 2000000], ["spend", "USDC", "month", 0]])
    check("over the month", "usdc", upay(1), usage=[["spend", "USDC", "day", 0], ["spend", "USDC", "month", 30000000]])
    check("paid model call without an amount", "usdc", req("llm.paid", ["provider", "openrouter"]), usage=usdc_usage)
    check("paid model call", "usdc", req("llm.paid", ["provider", "openrouter"], amounts=[["spend", "USDC", 1200]]), usage=usdc_usage)
    check("TWD where only USDC is limited", "usdc", req("pay.spend", ["host", "a.example"], amounts=twd(1)), usage=usdc_usage)
    check("a fourth host", "usdc", upay(1, host="d.example"), usage=usdc_usage)
    check("at the cap, over the threshold", "usdc_cap", upay(1250000), usage=usdc_usage)
    check("over the cap", "usdc_cap", upay(1250001), usage=usdc_usage)
    check("a host the cap's block dropped", "usdc_cap", upay(1, host="a.example"), usage=usdc_usage)

    check("issued by Python: read", "py_base", req())
    check("issued by Python: revoked by a Python entry", "py_base", req(), revoked=("py_base_root",))
    check("issued by Python: Rust's entry is for another token", "py_base", req(), revoked=("base_root",))
    check("narrowed by Python: inside", "py_base_narrowed", req(resource=["folder", "/docs/work/sub/a"]))
    check("narrowed by Python: outside", "py_base_narrowed", req())
    check("narrowed by Python: revoked at its block", "py_base_narrowed", req(resource=["folder", "/docs/work/sub/a"]), revoked=("py_narrowed_block",))
    check("narrowed twice by Python: job and local", "py_base_job", req(resource=["folder", "/docs/work/sub/a"], privacy="local", job="job-7"))
    check("narrowed twice by Python: private refused", "py_base_job", req(resource=["folder", "/docs/work/sub/a"], job="job-7"))
    check("narrowed twice by Python: parent block revoked", "py_base_job", req(resource=["folder", "/docs/work/sub/a"], privacy="local", job="job-7"), revoked=("py_narrowed_block",))
    check("Rust token, Python approval", "py_card_on_rust_base", pay(150, card="card-py"))
    check("Rust token, Python approval: over its cap", "py_card_on_rust_base", pay(151, card="card-py"))
    check("Rust token, Python approval: family revoked by Rust entry", "py_card_on_rust_base", pay(150, card="card-py"), revoked=("base_root",))
    check("Rust chain, Python block: before its start", "py_on_rust_chain2", req(resource=["folder", "/文件/工作/報告/a"], tool="call-py"), now=PT0 - 1)
    check("Rust chain, Python block: inside", "py_on_rust_chain2", req(resource=["folder", "/文件/工作/報告/a"], tool="call-py"), now=PT0 + 100)
    check("Rust chain, Python block: without the tool", "py_on_rust_chain2", req(resource=["folder", "/文件/工作/報告/a"]), now=PT0 + 100)
    pyrich = dict(holder="device:phone-1", privacy="standard")
    check("Python rich: routine send with a message", "py_rich", req("mail.send", ["home", "home-1"], amounts=[["messages", "", 1]], **pyrich), usage=[["messages", "", "month", 3]])
    check("Python rich: paid call at the threshold", "py_rich", req("llm.paid", ["provider", "x"], amounts=[["spend", "USD", 100]], **pyrich), usage=[["spend", "USD", "month", 0]])
    check("Python rich: paid call over the threshold", "py_rich", req("llm.paid", ["provider", "x"], amounts=[["spend", "USD", 101]], **pyrich), usage=[["spend", "USD", "month", 0]])
    check("Python rich: unicode calendar", "py_rich", req("calendar.read", ["calendar", "家庭"], **pyrich))
    check("Python rich: another calendar", "py_rich", req("calendar.read", ["calendar", "家庭2"], **pyrich))

    # ---- sequence: one spent set across requests
    def sequence(label: str, capacity: int, steps: list[tuple[str, dict[str, Any], int]], skew: int = 60, usage: list[list[Any]] | None = None) -> None:
        shown = {"root": pub, "capacity": capacity, "skew": skew,
                 "steps": [{"token": tokens[t], "request": r, "usage": usage or usage0, "now": now} for t, r, now in steps]}

        def call() -> Any:
            spent = P.SpentSet(capacity)
            out = []
            for step in shown["steps"]:
                m = P.Mandate.parse(step["token"], pub)
                d = P.check_and_record(m, codec.request_from_data(step["request"]), {(k, u, p): n for k, u, p, n in step["usage"]},
                                       P.RevocationList(), spent, step["now"], skew)
                out.append([d.kind, d.detail])
            return {"decisions": out, "spent": [[k, v] for k, v in spent.entries()]}

        add("permissions.sequence", label, shown, call)

    approved = pay(120, card="card-3")
    sequence("an approval works once", 100, [("base_card", approved, PT0 + 1), ("base_card", approved, PT0 + 2), ("base_card", approved, PT0 + 360)])
    sequence("a refused request spends nothing", 100, [("base_card", pay(120), PT0 + 1), ("base_card", pay(12001, card="card-3"), PT0 + 2), ("base_card", approved, PT0 + 3), ("base_card", approved, PT0 + 4)])
    sequence("a request that asks spends nothing", 100, [("base", pay(120), PT0), ("base", req(), PT0)])
    tool = req(job="job-7", tool="call-19")
    sequence("a tool call works once", 100, [("base_job_tool", tool, PT0), ("base_job_tool", tool, PT0), ("base_job", req(job="job-7"), PT0), ("base_job", req(job="job-7"), PT0)])
    sequence("two approvals, room for one", 1, [("base_card", approved, PT0 + 1), ("base_card_other", pay(120, card="card-4"), PT0 + 2), ("base_card_other", pay(120, card="card-4"), PT0 + 3)])
    sequence("room returns when the first has lapsed", 1, [("base_card", approved, PT0 + 1), ("base_job_tool", tool, PT0 + 361)])
    sequence("approval from Python on a Rust token, then replay", 10, [("py_card_on_rust_base", pay(150, card="card-py"), PT0), ("py_card_on_rust_base", pay(150, card="card-py"), PT0 + 1)])
    sequence("job and card bound together spend one id", 10, [("rich_chain3", rpay(300, "api.example.com", job="job-7", card="card-9"), PT0 + 5)] * 2, usage=chain_usage)

    # ---- narrow: each side appends, then reads its own bytes back
    def narrow(label: str, token: str, *steps: dict[str, Any]) -> None:
        def call() -> Any:
            m = P.Mandate.parse(tokens[token], pub)
            for step in steps:
                m = P.narrow(m, codec.narrowing_from_data(step))
            return mandate_data(P.Mandate.parse(m.to_base64(), pub), with_ids=False)

        add("permissions.narrow", f"{token}: {label}", {"token": tokens[token], "root": pub, "narrowings": list(steps)}, call)

    narrow("fewer actions", "base", {"actions": ["files.read"]})
    narrow("actions given unsorted", "base", {"actions": ["pay.spend", "files.read"]})
    narrow("a folder inside", "base", {"resources": [["folder", "/docs/work/sub"]]})
    narrow("the same folder and the host", "base", {"resources": [["host", "api.example.com"], ["folder", "/docs/work"]]})
    narrow("lower limit", "base", {"limits": [["spend", "TWD", "day", 500]]})
    narrow("equal limit", "base", {"limits": [["spend", "TWD", "day", 2000]]})
    narrow("limit for a new period", "base", {"limits": [["spend", "TWD", "month", 5000]]})
    narrow("requests limit", "base", {"limits": [["requests", "", "day", 10]]})
    narrow("cap", "base", {"caps": [["spend", "TWD", 12000]]})
    narrow("window", "base", {"not_before": PT0 + 60, "not_after": PT0 + 600})
    narrow("a window of one instant", "base", {"not_before": PT0 + 60, "not_after": PT0 + 60})
    narrow("privacy", "base", {"privacy": "local"})
    narrow("same privacy", "base", {"privacy": "private"})
    narrow("each_write again", "base", {"confirm": ["each_write"]})
    narrow("over_amount under each_write", "base", {"confirm": ["over_amount", "TWD", 2, 100]})
    narrow("job", "base", {"job": "job-7"})
    narrow("tool with an end", "base", {"tool": "call-1", "not_after": PT0 + 60})
    narrow("approval", "base", {"actions": ["pay.spend"], "resources": [["host", "api.example.com"]], "caps": [["spend", "TWD", 12000]], "not_after": PT0 + 300, "card": "card-3"})
    narrow("everything at once", "rich", {"actions": ["files.read", "pay.spend"], "resources": [["folder", "/文件/工作/報告"], ["calendar", "*"], ["host", "api.example.com"]],
                                          "limits": [["spend", "TWD", "day", 1000], ["messages", "", "month", 5]], "caps": [["spend", "TWD", 300], ["requests", "", 1]],
                                          "not_before": PT0 + 1, "not_after": PT0 + 500, "privacy": "local", "confirm": ["over_amount", "TWD", 2, 50],
                                          "job": "job-1", "tool": "call-1", "card": "card-1"})
    narrow("three steps", "rich", {"actions": ["files.read", "files.write", "pay.spend"]}, {"resources": [["folder", "/文件/工作/報告"]], "privacy": "private"},
           {"actions": ["files.read"], "job": "job-2"})
    narrow("lower, then lower again", "base", {"limits": [["spend", "TWD", "day", 500]], "caps": [["spend", "TWD", 300]]}, {"limits": [["spend", "TWD", "day", 400]], "caps": [["spend", "TWD", 200]]})
    narrow("any calendar to one calendar", "rich", {"resources": [["calendar", "家庭"]]})
    narrow("on a narrowed token", "base_actions", {"resources": [["folder", "/docs/work/sub/deeper"]]})
    narrow("same job again", "base_job", {"job": "job-7"})
    narrow("on a token narrowed by the other implementation", "py_base_narrowed", {"job": "job-9"})
    narrow("on a chain from both implementations", "py_on_rust_chain2", {"privacy": "local"})
    for label, token, step in [
        ("nothing", "base", {}), ("an action it lacks", "base", {"actions": ["mail.send"]}), ("one good and one missing action", "base", {"actions": ["files.read", "mail.send"]}),
        ("an action a level dropped", "base_actions", {"actions": ["files.write"]}), ("the parent folder", "base", {"resources": [["folder", "/docs"]]}),
        ("a sibling with the same prefix", "base", {"resources": [["folder", "/docs/workshop"]]}), ("any folder", "base", {"resources": [["folder", "*"]]}),
        ("a kind it lacks", "base", {"resources": [["calendar", "x"]]}), ("a folder a level dropped", "base_folder", {"resources": [["folder", "/docs/work/other"]]}),
        ("a higher limit", "base", {"limits": [["spend", "TWD", "day", 2001]]}), ("a limit above a level's", "base_limit", {"limits": [["spend", "TWD", "day", 501]]}),
        ("a limit in a unit it lacks", "base", {"limits": [["spend", "USD", "day", 1]]}), ("a limit of a kind it lacks", "base", {"limits": [["messages", "", "month", 1]]}),
        ("a cap above a level's", "base_cap", {"caps": [["spend", "TWD", 101]]}), ("a cap in a unit it lacks", "base", {"caps": [["spend", "USD", 1]]}),
        ("an earlier start", "base", {"not_before": PT0 - 1}), ("a later end", "base", {"not_after": end + 1}),
        ("an end before the start", "base", {"not_before": PT0 + 10, "not_after": PT0 + 9}), ("an end before a level's start", "base_window", {"not_after": PT0 + 10}),
        ("a less protective class", "base", {"privacy": "standard"}), ("a class above a level's", "base_local", {"privacy": "private"}),
        ("never", "base", {"confirm": ["never"]}), ("over_amount in a unit it lacks", "base", {"confirm": ["over_amount", "USD", 2, 1]}),
        ("another job", "base_job", {"job": "job-8"}), ("another tool", "base_job_tool", {"tool": "call-20", "not_after": PT0 + 60}),
        ("another card", "base_card", {"card": "card-4", "not_after": PT0 + 60}),
        ("a card without an end", "base", {"card": "card-1"}), ("a tool without an end", "base", {"tool": "call-1"}),
        ("a job with a quote", "base", {"job": 'j"x'}), ("a job with a space", "base", {"job": "job 7"}),
        ("a folder with a quote", "base", {"resources": [["folder", '/docs/work/a"b']]}), ("a folder with a newline", "base", {"resources": [["folder", "/docs/work/a\nb"]]}),
        ("duplicate actions", "base", {"actions": ["files.read", "files.read"]}), ("a malformed action", "base", {"actions": ["Read"]}),
        ("an unknown privacy class", "base", {"privacy": "secret"}), ("an unknown period", "base", {"limits": [["spend", "TWD", "week", 1]]}),
        ("a negative limit", "base", {"limits": [["spend", "TWD", "day", -1]]}), ("a time past year 9999", "base", {"not_after": 253402300800}),
        ("a sealed token", "sealed", {"job": "job-1"}), ("a token that does not verify", "other_root", {"job": "job-1"}),
        ("a token with an unknown block", "evil_check_true", {"job": "job-1"}),
    ]:
        narrow(f"refuses {label}", token, step)

    # ---- revocation lists
    def revocations(label: str, lists: list[list[str]], ask: list[str] | None = None, root: str = pub) -> None:
        shown = {"root": root, "lists": [[entries[e] for e in names] for names in lists], "ask": ask or []}

        def call() -> Any:
            merged = P.RevocationList()
            for group in shown["lists"]:
                one = P.RevocationList()
                for entry in group:
                    one.add_entry(entry, root)
                merged.merge(one)
            merged.verify(root)
            assert P.RevocationList.from_json(merged.to_json(), root) == merged
            return {"entries": [[e.id, e.at, e.signer] for e in merged.entries()], "json": merged.to_json(),
                    "contains": [merged.contains(i) for i in shown["ask"]]}

        add("permissions.revocations", label, shown, call)

    some_id, absent_id = "ab" * 64, "cd" * 64
    revocations("empty", [], [some_id])
    revocations("one entry", [["unrelated"]], [some_id, absent_id])
    revocations("union", [["base_root", "unrelated"], ["base_actions_block"], ["base_job_block", "unrelated"]], [some_id, absent_id])
    revocations("same id twice: the earlier time is kept", [["base_root"], ["base_root_earlier"]])
    revocations("same id twice, other order", [["base_root_earlier"], ["base_root"]])
    revocations("same id three times, from both implementations", [["base_root"], ["py_rust_base_root_earliest", "base_root_earlier"]])
    revocations("merging a list with itself", [["base_root", "unrelated"], ["base_root", "unrelated"]])
    revocations("entries made by Python", [["py_base_root", "py_narrowed_block"], ["unrelated"]])
    for bad in ["forged_by_other", "a_permission_is_not_an_entry", "two_blocks", "extra_line", "names_other_signer"]:
        revocations(f"refuses {bad}", [["unrelated"], [bad]])
    revocations("entries under another root are refused", [["unrelated"]], root=other_pub)
    revocations("the other key's entry under the other root", [["forged_by_other"]], root=other_pub)

    def revoke(label: str, key: str, ident: str, at: int) -> None:
        def call() -> Any:
            root = P.parse_private_key(key)
            revoked_list = P.RevocationList()
            revoked_list.add(ident, at, root)
            e = P.RevocationEntry.verify(revoked_list.entries()[0].token, P.public_key_text(root))
            return [e.id, e.at, e.signer]

        add("permissions.revoke", label, {"root_private": key, "id": ident, "at": at}, call)

    revoke("sign and verify", fx["root_private"], some_id, PT0)
    revoke("other key", fx["other_private"], absent_id, 0)
    revoke("last second", fx["root_private"], some_id, 253402300799)
    revoke("short id", fx["root_private"], "abcd", PT0)
    revoke("upper-case id", fx["root_private"], "AB" * 64, PT0)
    revoke("negative time", fx["root_private"], some_id, -1)

    # ---- spent set
    def spent(label: str, capacity: int, ops: list[list[Any]]) -> None:
        def call() -> Any:
            s = P.SpentSet(capacity)
            out: list[Any] = []
            for op in ops:
                if op[0] == "insert":
                    try:
                        s.insert(op[1], op[2], op[3])
                        out.append("ok")
                    except P.PermissionError as exc:
                        out.append(exc.code)
                elif op[0] == "contains":
                    out.append(s.contains(op[1], op[2]))
                elif op[0] == "room":
                    out.append(s.has_room(op[1], op[2]))
                else:
                    s.purge(op[1])
                    out.append(len(s))
            return {"results": out, "entries": [[k, v] for k, v in s.entries()]}

        add("permissions.spent", label, {"capacity": capacity, "ops": ops}, call)

    spent("insert and ask", 10, [["insert", "a", 100, 0], ["contains", "a", 0], ["contains", "a", 100], ["contains", "a", 101], ["contains", "b", 0]])
    spent("purge drops what has lapsed", 10, [["insert", "a", 100, 0], ["insert", "b", 200, 0], ["purge", 100], ["purge", 101], ["purge", 201]])
    spent("full refuses", 2, [["insert", "a", 100, 0], ["insert", "b", 100, 0], ["insert", "c", 100, 0], ["room", 0, 0], ["room", 1, 0], ["contains", "c", 0]])
    spent("inserting again keeps the later time", 2, [["insert", "a", 100, 0], ["insert", "a", 50, 0], ["insert", "a", 300, 0], ["insert", "a", 200, 0]])
    spent("a full set takes a key it already has", 1, [["insert", "a", 100, 0], ["insert", "a", 150, 0], ["insert", "b", 150, 0]])
    spent("lapsed entries make room", 1, [["insert", "a", 100, 0], ["room", 1, 100], ["room", 1, 101], ["insert", "b", 500, 101], ["contains", "a", 101]])
    spent("unicode-free keys sort by code point", 10, [["insert", "m1_b/tool/x", 9, 0], ["insert", "m1_a/tool/x", 9, 0], ["insert", "m1_a/card/x", 9, 0]])
    spent("capacity zero", 0, [["room", 0, 0], ["room", 1, 0], ["insert", "a", 1, 0]])

    # ---- formatting
    for kind, unit, decimals, amount in [
        ("spend", "TWD", 2, 2000), ("spend", "TWD", 2, 2050), ("spend", "TWD", 2, 5), ("spend", "TWD", 2, 0), ("spend", "TWD", 0, 20),
        ("spend", "USD", 2, 123456), ("spend", "EUR", 2, 99), ("spend", "JPY", 0, 300), ("spend", "USDC", 6, 2000000), ("spend", "USDC", 6, 1500000),
        ("spend", "USDC", 6, 1234567), ("spend", "USDC", 6, 1), ("spend", "ETH", 18, 1000000000000000000), ("spend", "ETH", 18, 1),
        ("spend", "TWD", 2, 9223372036854775807), ("spend", "BTC", 8, 10), ("requests", "", 0, 200), ("messages", "", 0, 20), ("requests", "", 0, 1),
    ]:
        for lang in ("en", "zh-TW"):
            add("permissions.format", f"amount {kind} {unit} {decimals} {amount} {lang}", {"what": "amount", "kind": kind, "unit": unit, "decimals": decimals, "amount": amount, "lang": lang},
                lambda k=kind, u=unit, d=decimals, a=amount, g=lang: P.format_amount(k, u, d, a, g))
    for t in [0, 1, 59, 86399, 86400, 951782400, 951868800, 1709164800, PT0, PT0 + 300, 4102444800, 253402300799]:
        add("permissions.format", f"time {t}", {"what": "time", "t": t}, lambda t=t: [P.format_time(t), P.rfc3339(t)])
    for action in ["files.read", "files.write", "mail.read", "mail.draft", "mail.send", "calendar.read", "calendar.write", "health.read", "records.read",
                   "llm.call", "llm.paid", "pay.spend", "home.admin", "x.custom", "files.readx"]:
        add("permissions.format", f"class {action}", {"what": "class", "action": action}, lambda a=action: P.action_class(a))
    return area


def permissions_file(out_dir: Path) -> str | None:
    """The permissions area: recomputed when biscuit-python is installed, otherwise the
    file already in `out_dir` or the committed one, unchanged (None if there is neither)."""
    if permissions_available():
        return permissions_vectors().render()
    for directory in (out_dir, DEFAULT_OUT):
        path = directory / PERMISSIONS_FILE
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


# ---------------------------------------------------------------- main


def build(out_dir: Path = DEFAULT_OUT) -> dict[str, str]:
    """File name -> content for every area."""
    files = {f"{area.name}.json": area.render() for area in (quota_vectors(), ration_vectors(), cache_vectors())}
    files["records.json"] = records_vectors.render()  # see muse/records/vectors.py; the keys in it are test-only
    permissions = permissions_file(out_dir)
    if permissions is not None:
        files[PERMISSIONS_FILE] = permissions
    return files


def write(out_dir: Path) -> dict[str, str]:
    files = build(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (out_dir / name).write_text(content, encoding="utf-8", newline="\n")
    return files


def stale(out_dir: Path) -> list[str]:
    """Names of files in `out_dir` that differ from what Python returns now (or are missing or extra)."""
    files = build(out_dir)
    present = {p.name for p in out_dir.glob("*.json")} if out_dir.is_dir() else set()
    bad = [name for name, content in files.items()
           if name not in present or (out_dir / name).read_text(encoding="utf-8") != content]
    return sorted(bad + [name for name in present if name not in files])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory to write (default: core/conformance)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file is out of date")
    parser.add_argument("--make-python-fixtures", action="store_true",
                        help="write core/conformance/fixtures/permission_tokens_py.json (new random tokens; only when the schema changes)")
    args = parser.parse_args(argv)
    if args.make_python_fixtures:
        path = FIXTURES / "permission_tokens_py.json"
        path.write_text(json.dumps(make_python_fixtures(), indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {path}; now run this script again without the flag")
        return 0
    if not permissions_available():
        print("note: biscuit-python is not installed; permissions.json is passed through, not recomputed", file=sys.stderr)
    if args.check:
        bad = stale(args.out)
        if bad:
            print("out of date: " + ", ".join(bad) + " (run python scripts/export_conformance.py)", file=sys.stderr)
            return 1
        print("conformance vectors are up to date")
        return 0
    files = write(args.out)
    for name, content in files.items():
        print(f"{name}: {content.count(chr(10)) - 2} vectors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
