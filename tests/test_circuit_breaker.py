"""Circuit breaker, Retry-After parsing, probe tickets, cross-alias, quota."""

from datetime import datetime, timezone

import pytest

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState
from muse.gateway.clock import FakeClock
from muse.gateway.retry_after import InvalidRetryAfter, parse_retry_after
from muse.gateway.quota import is_quota_exhausted


def test_parse_retry_after_delta_seconds():
    # Returns a *delay*, not an absolute timestamp.
    assert parse_retry_after("30", now_wall=1000.0) == 30.0


def test_parse_retry_after_http_date():
    when = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
    header = "Thu, 12 Nov 2026 06:30:00 GMT"
    got = parse_retry_after(header, now_wall=when.timestamp() - 15, default_seconds=99)
    assert got == 15.0


def test_parse_retry_after_past_http_date_uses_default_not_zero():
    when = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
    header = "Thu, 12 Nov 2026 06:30:00 GMT"
    got = parse_retry_after(header, now_wall=when.timestamp() + 60, default_seconds=99)
    assert got == 99.0


def test_parse_retry_after_missing_uses_default():
    assert parse_retry_after(None, now_wall=10.0, default_seconds=7) == 7.0
    assert parse_retry_after("", now_wall=10.0, default_seconds=7) == 7.0


def test_parse_retry_after_invalid_raises():
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("not-a-date", now_wall=10.0, default_seconds=7)
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("²", now_wall=10.0)
    # Fullwidth digits are not ASCII and must not be accepted as delta-seconds.
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("３０", now_wall=10.0)


def test_parse_retry_after_is_capped():
    assert parse_retry_after("99999", now_wall=0.0, max_seconds=60) == 60.0


def test_closed_by_default():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, jitter_ratio=0)
    assert breaker.state("groq") == CircuitState.CLOSED
    breaker.allow("groq")  # does not raise


def test_429_with_retry_after_opens_for_that_duration():
    clock = FakeClock(1000)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=30, jitter_ratio=0)
    state = breaker.on_response("groq", 429, "60")
    assert state == CircuitState.OPEN
    try:
        breaker.allow("groq")
        raise AssertionError("expected CircuitOpen")
    except CircuitOpen as exc:
        assert exc.provider == "groq"
        assert abs(exc.retry_after - 60) < 0.01
    clock.advance(59)
    try:
        breaker.allow("groq")
        raise AssertionError("should still be open")
    except CircuitOpen:
        pass
    clock.advance(2)
    assert breaker.state("groq") == CircuitState.HALF_OPEN
    ticket = breaker.allow("groq")  # probe is allowed
    breaker.on_response("groq", 200, ticket=ticket)
    assert breaker.state("groq") == CircuitState.CLOSED


def test_429_without_header_uses_default_cooldown():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=12, jitter_ratio=0)
    breaker.on_response("gemini", 429, None)
    try:
        breaker.allow("gemini")
    except CircuitOpen as exc:
        assert abs(exc.retry_after - 12) < 0.01
    else:
        raise AssertionError("expected open")


def test_success_closes():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, jitter_ratio=0)
    breaker.on_response("groq", 429, "10")
    clock.advance(11)
    ticket = breaker.allow("groq")
    breaker.on_response("groq", 200, ticket=ticket)
    assert breaker.state("groq") == CircuitState.CLOSED


def test_late_success_does_not_close_an_open_circuit():
    """A stale 200 from a previous generation must not close a re-opened circuit."""
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=10, jitter_ratio=0)
    first = breaker.allow("groq")
    breaker.on_response("groq", 429, "10", ticket=first)
    assert breaker.state("groq") == CircuitState.OPEN
    # A late 200 for the original ticket is ignored.
    breaker.on_response("groq", 200, ticket=first)
    assert breaker.state("groq") == CircuitState.OPEN
    # And a 200 with no ticket while OPEN is also ignored.
    breaker.on_response("groq", 200)
    assert breaker.state("groq") == CircuitState.OPEN


def test_half_open_failure_reopens_with_new_retry_after():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=5, jitter_ratio=0)
    breaker.on_response("groq", 429, "10")
    clock.advance(11)
    ticket = breaker.allow("groq")
    breaker.on_response("groq", 429, "20", ticket=ticket)
    assert breaker.state("groq") == CircuitState.OPEN
    snap = breaker.snapshot("groq")
    assert snap.retry_at == 11 + 20


def test_half_open_second_probe_has_positive_retry_after():
    clock = FakeClock(0)
    breaker = CircuitBreaker(
        clock=clock, jitter_ratio=0, probe_timeout_seconds=30, default_cooldown_seconds=10
    )
    breaker.on_response("groq", 429, "5")
    clock.advance(6)
    breaker.allow("groq")
    try:
        breaker.allow("groq")
        raise AssertionError("second probe should be refused")
    except CircuitOpen as exc:
        assert exc.retry_after > 0
        assert exc.retry_after <= 30


def test_hung_probe_times_out_and_reopens_with_positive_retry():
    clock = FakeClock(0)
    breaker = CircuitBreaker(
        clock=clock, jitter_ratio=0, probe_timeout_seconds=8, default_cooldown_seconds=12
    )
    breaker.on_response("groq", 429, "5")
    clock.advance(6)
    breaker.allow("groq")  # probe in flight
    clock.advance(9)  # past probe_deadline
    # _sync should re-open on the next observation.
    assert breaker.state("groq") == CircuitState.OPEN
    try:
        breaker.allow("groq")
        raise AssertionError("expected open after probe timeout")
    except CircuitOpen as exc:
        assert exc.retry_after > 0


def test_consecutive_5xx_opens_after_threshold():
    clock = FakeClock(0)
    breaker = CircuitBreaker(
        clock=clock, open_after_fails=2, default_cooldown_seconds=8, jitter_ratio=0
    )
    assert breaker.on_response("jina", 500) == CircuitState.CLOSED
    assert breaker.on_response("jina", 502) == CircuitState.OPEN
    try:
        breaker.allow("jina")
    except CircuitOpen as exc:
        assert abs(exc.retry_after - 8) < 0.01
    else:
        raise AssertionError("expected open")


def test_503_honours_retry_after():
    clock = FakeClock(50)
    breaker = CircuitBreaker(clock=clock, jitter_ratio=0)
    breaker.record_headers("cohere", 503, {"Retry-After": "15", "content-type": "text/plain"})
    try:
        breaker.allow("cohere")
    except CircuitOpen as exc:
        assert abs(exc.retry_after - 15) < 0.01
    else:
        raise AssertionError("expected open")


def test_401_and_404_do_not_open_the_circuit():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, open_after_fails=1, jitter_ratio=0)
    assert breaker.on_response("groq", 401) == CircuitState.CLOSED
    assert breaker.on_response("groq", 404) == CircuitState.CLOSED
    assert breaker.on_response("groq", 400) == CircuitState.CLOSED
    assert breaker.state("groq") == CircuitState.CLOSED
    breaker.allow("groq")  # still callable


def test_only_one_probe_in_half_open():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, jitter_ratio=0)
    breaker.on_response("groq", 429, "5")
    clock.advance(6)
    breaker.allow("groq")
    try:
        breaker.allow("groq")
        raise AssertionError("second probe should be refused")
    except CircuitOpen:
        pass


def test_quota_exhausted_opens_exhausted_state():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, jitter_ratio=0, quota_exhausted_seconds=3600)
    state = breaker.on_response("groq", 429, quota_exhausted=True)
    assert state == CircuitState.EXHAUSTED
    try:
        breaker.allow("groq")
    except CircuitOpen as exc:
        assert exc.retry_after >= 3600 - 0.01
    else:
        raise AssertionError("expected exhausted")
    # A late success must not close it.
    breaker.on_response("groq", 200)
    assert breaker.state("groq") == CircuitState.EXHAUSTED


def test_cross_alias_shares_upstream_circuit():
    clock = FakeClock(0)
    upstream = "groq/openai/gpt-oss-20b"
    breaker = CircuitBreaker(
        clock=clock,
        jitter_ratio=0,
        default_cooldown_seconds=30,
        alias_upstreams={"fast": (upstream,), "smart": (upstream,), "cheap": ("other/model",)},
    )
    breaker.on_response(upstream, 429, "40")
    # Both aliases that share the upstream are blocked.
    for name in ("fast", "smart"):
        try:
            breaker.allow(name)
            raise AssertionError(f"{name} should be blocked")
        except CircuitOpen as exc:
            assert abs(exc.retry_after - 40) < 0.01
    # An alias on a different upstream is still callable.
    breaker.allow("cheap")


def test_alias_with_one_healthy_upstream_stays_callable():
    clock = FakeClock(0)
    breaker = CircuitBreaker(
        clock=clock,
        jitter_ratio=0,
        alias_upstreams={"fast": ("up-a", "up-b")},
    )
    breaker.on_response("up-a", 429, "30")
    # `fast` still has up-b closed (healthy), so allow succeeds.
    ticket = breaker.allow("fast")
    assert ticket.provider == "up-b"


def test_is_quota_exhausted_detects_caps_not_bare_429():
    assert is_quota_exhausted(status_code=429, message="insufficient_quota")
    assert is_quota_exhausted(status_code=429, message="You have exceeded your weekly quota")
    assert not is_quota_exhausted(status_code=429, message="rate limited, try again")
    # The helper is message-based; the breaker still ignores 401/404.
    assert is_quota_exhausted(status_code=401, message="insufficient_quota")
