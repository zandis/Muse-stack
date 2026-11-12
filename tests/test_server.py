import logging
import threading

from fastapi.testclient import TestClient

from muse.secrets import redact
from muse.server import JobLimiter, create_app
from muse.settlement.constants import BASE_SEPOLIA_USDC, default_pay_to
from muse.settlement.mock_payment import make_mock_payment
from muse.settlement.x402 import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
)


def _client(fake_env) -> TestClient:
    app = create_app(environ=fake_env)
    return TestClient(app)


def _auth(fake_env) -> dict[str, str]:
    return {"Authorization": f"Bearer {fake_env['LITELLM_MASTER_KEY']}"}


def _list_verified(client: TestClient, *, result_id: str = "listed-verified-1", proof: str = "rfl"):
    """Inject a real-verified result. Mock Lean never lists a result for sale."""
    return client.app.state.muse.results.add(
        result_hash="ab" * 32,
        problem_id="sample.nat_add_zero",
        title="Adding zero",
        verdict="verified",
        verifier="lean4",
        model="smart",
        proof=proof,
        statement="theorem sample_nat_add_zero (a : Nat) : a + 0 = a := by\n  sorry",
        price_atomic=10_000,
        result_id=result_id,
    )


def test_health_requires_auth_when_master_key_is_set(fake_env):
    client = _client(fake_env)
    denied = client.get("/health")
    assert denied.status_code == 401
    assert "unauthorized" in denied.text
    r = client.get("/health", headers=_auth(fake_env))
    assert r.status_code == 200
    assert r.json()["facilitator"] == "mock"
    assert r.json()["network"] == "eip155:84532"
    # /docs is also behind the key
    assert client.get("/docs").status_code == 401
    assert client.get("/docs", headers=_auth(fake_env)).status_code == 200


def test_health_and_keys_never_echo_secrets(fake_env):
    client = _client(fake_env)
    headers = _auth(fake_env)
    denied = client.get("/v1/providers/keys")
    assert denied.status_code == 401
    keys = client.get("/v1/providers/keys", headers=headers)
    assert keys.status_code == 200
    body = keys.json()
    assert body["keys"]["GROQ_API_KEY"] == "configured"
    assert body["keys"]["OPENROUTER_API_KEY"] == "missing"
    blob = keys.text
    assert "groq-secret-value-should-not-appear" not in blob
    assert "gemini-secret-value-should-not-appear" not in blob
    assert "sk-test-master-key-not-real" not in blob


def test_refuses_to_start_without_master_key_off_loopback(fake_env, caplog):
    env = {**fake_env, "LITELLM_MASTER_KEY": "", "MUSE_PHASE0_HOST": "0.0.0.0"}
    try:
        create_app(environ=env)
        raise AssertionError("expected startup refusal")
    except ValueError as exc:
        assert "LITELLM_MASTER_KEY" in str(exc)
        assert "0.0.0.0" in str(exc)

    # Loopback without a key is permitted, with a loud warning.
    env = {**fake_env, "LITELLM_MASTER_KEY": "", "MUSE_PHASE0_HOST": "127.0.0.1"}
    with caplog.at_level(logging.WARNING, logger="muse.server"):
        app = create_app(environ=env)
    assert "WITHOUT a LITELLM_MASTER_KEY" in caplog.text
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200


def test_health_usage_and_event_circuit(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    ev = client.post(
        "/v1/providers/events",
        headers=auth,
        json={"provider": "groq", "statusCode": 429, "outcome": "failed", "retryAfter": "30"},
    )
    assert ev.status_code == 200
    health = client.get("/v1/providers/health", headers=auth).json()
    groq = next(p for p in health["providers"] if p["provider"] == "groq")
    assert groq["state"] == "open"
    assert groq["health"] == "unavailable"
    assert groq["usage"]["rate_limited"] == 1
    usage = client.get("/v1/providers/usage", headers=auth).json()
    assert any(p["provider"] == "groq" and p["state"] == "open" for p in usage["providers"])


def test_malformed_retry_after_is_400_without_exception_text(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    r = client.post(
        "/v1/providers/events",
        headers=auth,
        json={"provider": "groq", "statusCode": 429, "outcome": "failed", "retryAfter": "²"},
    )
    assert r.status_code == 400
    assert r.json() == {"error": "malformed Retry-After header"}
    assert "ValueError" not in r.text
    assert "Traceback" not in r.text
    assert "InvalidRetryAfter" not in r.text


def test_no_job_at_construction_and_problems_list_has_no_path(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    listing = client.get("/v1/results", headers=auth).json()
    assert listing["results"] == []
    problems = client.get("/v1/work/problems", headers=auth).json()["problems"]
    assert any(p["problemId"] == "sample.nat_add_comm" for p in problems)
    for p in problems:
        assert "path" not in p
        blob = str(p)
        assert "/workspace" not in blob
        assert "/home" not in blob
        assert "muse/useful_work" not in blob


def test_useful_work_job_never_returns_proof_and_mock_is_not_sold(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    created = client.post(
        "/v1/work/jobs",
        headers=auth,
        json={"problemId": "sample.nat_add_zero"},
    )
    assert created.status_code == 200
    job = created.json()
    assert job["status"] == "completed"
    assert job["verified"] is False
    assert job["mode"] == "mock"
    assert job["listedForSale"] is None
    assert job.get("proof") in (None, "")
    assert "proof" not in job or job["proof"] is None
    job_id = job["jobId"]
    fetched = client.get(f"/v1/work/jobs/{job_id}", headers=auth)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body.get("proof") is None
    assert "Nat.add" not in fetched.text or body.get("proof") is None
    # The paid good is not on this path even as a nested field.
    assert "listedForSale" not in body or body.get("listedForSale") is None


def test_x402_sale_of_injected_verified_result(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    record = _list_verified(client, proof="by Nat.add_zero")
    result_id = record.result_id

    unpaid = client.get(f"/v1/results/{result_id}", headers=auth)
    assert unpaid.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in unpaid.headers
    body = unpaid.json()
    assert body["accepts"][0]["asset"] == BASE_SEPOLIA_USDC
    assert body["result"]["proof"] is None
    assert body["result"]["resultHash"] == record.result_hash
    quote = body["quote"]
    assert quote["quoteId"]
    assert quote["nonce"]
    assert quote["expiresAt"]

    payment = make_mock_payment(
        amount=10_000,
        pay_to=default_pay_to(),
        quote_id=quote["quoteId"],
        nonce=quote["nonce"],
    )
    paid = client.get(
        f"/v1/results/{result_id}",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: payment},
    )
    assert paid.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in paid.headers
    goods = paid.json()
    assert goods["proof"] == "by Nat.add_zero"
    assert goods["receipt"]["settled"] is False
    assert goods["receipt"]["mode"] == "mock"
    assert "no funds moved" in goods["receipt"]["detail"]
    assert "groq-secret-value-should-not-appear" not in paid.text
    # Sale is recorded after delivery.
    assert client.app.state.muse.results.get(result_id).sold_count == 1

    # Replay is rejected.
    again = client.get(
        f"/v1/results/{result_id}",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: payment},
    )
    assert again.status_code == 402
    assert again.json()["rejection"]["code"] == "replay"
    assert client.app.state.muse.results.get(result_id).sold_count == 1


def test_malformed_payment_header_is_400(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    _list_verified(client, result_id="r-malformed")
    r = client.get(
        "/v1/results/r-malformed",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: "not-valid-base64%%%"},
    )
    assert r.status_code == 400
    assert r.json() == {"error": "malformed payment header"}
    assert "ValueError" not in r.text
    assert "Unicode" not in r.text
    # Non-ASCII "²" is covered by test_malformed_payment_header_raises_typed_error
    # (httpx refuses to send a non-ASCII header value).
    assert "Traceback" not in r.text


def test_sale_not_recorded_when_delivery_fails(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    secret = fake_env["GROQ_API_KEY"]
    record = _list_verified(client, result_id="leaky", proof=f"proof leaked {secret}")
    unpaid = client.get(f"/v1/results/{record.result_id}", headers=auth)
    quote = unpaid.json()["quote"]
    payment = make_mock_payment(
        amount=10_000,
        pay_to=default_pay_to(),
        quote_id=quote["quoteId"],
        nonce=quote["nonce"],
    )
    paid = client.get(
        f"/v1/results/{record.result_id}",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: payment},
    )
    assert paid.status_code == 500
    assert paid.json() == {"error": "internal_error"}
    assert secret not in paid.text
    assert client.app.state.muse.results.get(record.result_id).sold_count == 0


def test_bad_payment_stays_402(fake_env):
    client = _client(fake_env)
    auth = _auth(fake_env)
    _list_verified(client, result_id="r-bad")
    bad = client.get(
        "/v1/results/r-bad",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: "mock:ok"},
    )
    # shorthand token without a signed authorization is rejected, still 402
    assert bad.status_code == 402
    assert "rejection" in bad.json()


def test_job_limiter_rejects_when_busy():
    limiter = JobLimiter(max_concurrent=1, min_interval_seconds=0)
    assert limiter.acquire() is True
    assert limiter.acquire(timeout=0.01) is False
    limiter.release()
    assert limiter.acquire(timeout=0.01) is True
    limiter.release()


def test_jobs_return_429_when_concurrent_cap_hit(fake_env):
    app = create_app(environ=fake_env)
    state = app.state.muse
    started = threading.Event()
    release = threading.Event()
    original = state.runner.run

    def blocked(problem_id, *, models=None):
        started.set()
        release.wait(timeout=5)
        return original(problem_id, models=models)

    state.runner.run = blocked  # type: ignore[method-assign]
    # Shrink the limiter so a single in-flight job fills it.
    state.job_limiter = JobLimiter(max_concurrent=1)

    client = TestClient(app)
    auth = _auth(fake_env)
    statuses: list[int] = []

    def first():
        r = client.post(
            "/v1/work/jobs",
            headers=auth,
            json={"problemId": "sample.nat_add_comm"},
        )
        statuses.append(r.status_code)

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=5)
    second = client.post(
        "/v1/work/jobs",
        headers=auth,
        json={"problemId": "sample.nat_add_zero"},
    )
    assert second.status_code == 429
    assert second.json() == {"error": "too many concurrent jobs"}
    release.set()
    t.join(timeout=5)
    assert statuses == [200]


def test_redact_helper():
    env = {"GROQ_API_KEY": "super-secret-groq-key-value", "OTHER": "ok"}
    assert "super-secret" not in redact("failed with super-secret-groq-key-value", env)
    assert redact("no secrets here", env) == "no secrets here"
