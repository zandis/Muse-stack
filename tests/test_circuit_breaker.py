from datetime import datetime, timezone

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState
from muse.gateway.clock import FakeClock
from muse.gateway.retry_after import parse_retry_after


def test_parse_retry_after_delta_seconds():
    assert parse_retry_after("30", now=1000.0) == 1030.0


def test_parse_retry_after_http_date():
    when = datetime(2026, 11, 12, 6, 30, tzinfo=timezone.utc)
    header = "Thu, 12 Nov 2026 06:30:00 GMT"
    got = parse_retry_after(header, now=when.timestamp() - 15, default_seconds=99)
    assert got == when.timestamp()


def test_parse_retry_after_missing_uses_default():
    assert parse_retry_after(None, now=10.0, default_seconds=7) == 17.0
    assert parse_retry_after("not-a-date", now=10.0, default_seconds=7) == 17.0


def test_parse_retry_after_is_capped():
    assert parse_retry_after("99999", now=0.0, max_seconds=60) == 60.0


def test_closed_by_default():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock)
    assert breaker.state("groq") == CircuitState.CLOSED
    breaker.allow("groq")  # does not raise


def test_429_with_retry_after_opens_for_that_duration():
    clock = FakeClock(1000)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=30)
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
    breaker.allow("groq")  # probe is allowed
    breaker.on_response("groq", 200)
    assert breaker.state("groq") == CircuitState.CLOSED


def test_429_without_header_uses_default_cooldown():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=12)
    breaker.on_response("gemini", 429, None)
    try:
        breaker.allow("gemini")
    except CircuitOpen as exc:
        assert abs(exc.retry_after - 12) < 0.01
    else:
        raise AssertionError("expected open")


def test_success_closes():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock)
    breaker.on_response("groq", 429, "10")
    clock.advance(11)
    breaker.allow("groq")
    breaker.on_response("groq", 200)
    assert breaker.state("groq") == CircuitState.CLOSED


def test_half_open_failure_reopens_with_new_retry_after():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=5)
    breaker.on_response("groq", 429, "10")
    clock.advance(11)
    breaker.allow("groq")
    breaker.on_response("groq", 429, "20")
    assert breaker.state("groq") == CircuitState.OPEN
    snap = breaker.snapshot("groq")
    assert snap.retry_at == 11 + 20


def test_consecutive_5xx_opens_after_threshold():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, open_after_fails=2, default_cooldown_seconds=8)
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
    breaker = CircuitBreaker(clock=clock)
    breaker.record_headers("cohere", 503, {"Retry-After": "15", "content-type": "text/plain"})
    try:
        breaker.allow("cohere")
    except CircuitOpen as exc:
        assert abs(exc.retry_after - 15) < 0.01
    else:
        raise AssertionError("expected open")


def test_only_one_probe_in_half_open():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock)
    breaker.on_response("groq", 429, "5")
    clock.advance(6)
    breaker.allow("groq")
    try:
        breaker.allow("groq")
        raise AssertionError("second probe should be refused")
    except CircuitOpen:
        pass
