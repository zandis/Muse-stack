from fastapi.testclient import TestClient

from muse.secrets import redact
from muse.server import create_app
from muse.settlement.constants import BASE_SEPOLIA_USDC, default_pay_to
from muse.settlement.mock_payment import make_mock_payment
from muse.settlement.x402 import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER


def _client(fake_env) -> TestClient:
    app = create_app(environ=fake_env)
    return TestClient(app)


def test_health_and_keys_never_echo_secrets(fake_env):
    client = _client(fake_env)
    # /health is unauthenticated
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["facilitator"] == "mock"
    assert r.json()["network"] == "eip155:84532"
    # keys require the master key
    denied = client.get("/v1/providers/keys")
    assert denied.status_code == 401
    headers = {"Authorization": "Bearer sk-test-master-key-not-real"}
    keys = client.get("/v1/providers/keys", headers=headers)
    assert keys.status_code == 200
    body = keys.json()
    assert body["keys"]["GROQ_API_KEY"] == "configured"
    assert body["keys"]["OPENROUTER_API_KEY"] == "missing"
    blob = keys.text
    assert "groq-secret-value-should-not-appear" not in blob
    assert "gemini-secret-value-should-not-appear" not in blob
    assert "sk-test-master-key-not-real" not in blob


def test_health_usage_and_event_circuit(fake_env):
    client = _client(fake_env)
    auth = {"Authorization": "Bearer sk-test-master-key-not-real"}
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


def test_useful_work_job_and_x402_sale(fake_env):
    client = _client(fake_env)
    auth = {"Authorization": "Bearer sk-test-master-key-not-real"}
    problems = client.get("/v1/work/problems", headers=auth).json()["problems"]
    assert any(p["problemId"] == "sample.nat_add_comm" for p in problems)

    created = client.post(
        "/v1/work/jobs",
        headers=auth,
        json={"problemId": "sample.nat_add_zero"},
    )
    assert created.status_code == 201
    job = created.json()
    assert job["status"] == "verified"
    assert job["resultHash"]
    result_id = job["listedForSale"]

    unpaid = client.get(f"/v1/results/{result_id}", headers=auth)
    assert unpaid.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in unpaid.headers
    body = unpaid.json()
    assert body["accepts"][0]["asset"] == BASE_SEPOLIA_USDC
    assert body["result"]["proof"] is None
    assert body["result"]["resultHash"] == job["resultHash"]

    payment = make_mock_payment(amount=10_000, pay_to=default_pay_to())
    paid = client.get(
        f"/v1/results/{result_id}",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: payment},
    )
    assert paid.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in paid.headers
    goods = paid.json()
    assert goods["proof"]
    assert "sorry" not in goods["proof"]
    assert goods["receipt"]["settled"] is False
    assert goods["receipt"]["mode"] == "mock"
    assert "no funds moved" in goods["receipt"]["detail"]
    # secrets stay out
    assert "groq-secret-value-should-not-appear" not in paid.text


def test_demo_result_is_seeded_and_bad_payment_stays_402(fake_env):
    client = _client(fake_env)
    auth = {"Authorization": "Bearer sk-test-master-key-not-real"}
    listing = client.get("/v1/results", headers=auth).json()
    ids = {item["resultId"] for item in listing["results"]}
    assert "demo-nat-add-comm" in ids
    bad = client.get(
        "/v1/results/demo-nat-add-comm",
        headers={**auth, PAYMENT_SIGNATURE_HEADER: "mock:ok"},
    )
    # shorthand token without amount/payTo is rejected, still 402
    assert bad.status_code == 402
    assert "rejection" in bad.json()


def test_redact_helper():
    env = {"GROQ_API_KEY": "super-secret-groq-key-value", "OTHER": "ok"}
    assert "super-secret" not in redact("failed with super-secret-groq-key-value", env)
    assert redact("no secrets here", env) == "no secrets here"
