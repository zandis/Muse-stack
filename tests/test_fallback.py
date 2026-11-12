from muse.gateway.circuit_breaker import CircuitBreaker, CircuitState
from muse.gateway.clock import FakeClock
from muse.gateway.fallback import CallFailed, FallbackChain, FallbackExhausted
from muse.gateway.router import GatewayRouter


def test_first_success_wins():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock)
    chain = FallbackChain(breaker, ["gemini", "groq"])
    calls = []

    def call(provider: str) -> str:
        calls.append(provider)
        return f"ok:{provider}"

    result = chain.execute(call)
    assert result.provider == "gemini"
    assert result.value == "ok:gemini"
    assert calls == ["gemini"]
    assert breaker.state("gemini") == CircuitState.CLOSED


def test_skips_open_circuit_and_tries_next():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock)
    breaker.on_response("gemini", 429, "30")
    chain = FallbackChain(breaker, ["gemini", "groq"])

    def call(provider: str) -> str:
        if provider == "gemini":
            raise AssertionError("open circuit must not be called")
        return "from-groq"

    result = chain.execute(call)
    assert result.provider == "groq"
    assert result.attempts[0].outcome == "skipped"
    assert result.attempts[1].outcome == "success"


def test_429_trips_breaker_and_continues():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, default_cooldown_seconds=30)
    chain = FallbackChain(breaker, ["gemini", "groq"])

    def call(provider: str) -> str:
        if provider == "gemini":
            raise CallFailed("gemini", "rate limited", status_code=429, retry_after="45")
        return "ok"

    result = chain.execute(call)
    assert result.provider == "groq"
    assert breaker.state("gemini") == CircuitState.OPEN
    snap = breaker.snapshot("gemini")
    assert snap.retry_at == 45


def test_all_fail_raises_exhausted_with_attempts():
    clock = FakeClock(0)
    breaker = CircuitBreaker(clock=clock, open_after_fails=1)
    chain = FallbackChain(breaker, ["a", "b"])

    def call(provider: str) -> str:
        raise CallFailed(provider, "down", status_code=500)

    try:
        chain.execute(call)
        raise AssertionError("expected exhausted")
    except FallbackExhausted as exc:
        assert exc.chain == ["a", "b"]
        assert len(exc.attempts) == 2
        assert all(a.outcome == "failed" for a in exc.attempts)


def test_empty_chain():
    breaker = CircuitBreaker(clock=FakeClock(0))
    try:
        FallbackChain(breaker, []).execute(lambda p: p)
        raise AssertionError("expected exhausted")
    except FallbackExhausted as exc:
        assert exc.errors == ["empty chain"]


def test_router_counts_usage_without_double_tripping():
    clock = FakeClock(0)
    router = GatewayRouter(providers=["gemini", "groq"], default_cooldown_seconds=10, clock=clock)

    def call(provider: str) -> str:
        if provider == "gemini":
            raise CallFailed("gemini", "429", status_code=429, retry_after="20")
        return "ok"

    result = router.execute(call)
    assert result.provider == "groq"
    health = router.health.health()
    by_name = {p["provider"]: p for p in health["providers"]}
    assert by_name["gemini"]["usage"]["rate_limited"] == 1
    assert by_name["gemini"]["usage"]["failures"] == 1
    assert by_name["gemini"]["state"] == "open"
    assert by_name["groq"]["usage"]["successes"] == 1
    # A single 429 must not have been applied twice (which would still be open,
    # but retry_at would be computed twice from the same clock and stay 20).
    assert by_name["gemini"]["retry_at"] == 20
