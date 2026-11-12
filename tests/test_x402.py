import pytest

from muse.config import load_settings
from muse.settlement.constants import (
    BASE_MAINNET_CAIP2,
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_USDC,
    default_pay_to,
    is_allowed_network,
    is_mainnet_network,
)
from muse.settlement.mock_payment import make_mock_payment
from muse.settlement.quotes import QuoteStore, quote_nonce
from muse.settlement.verifier import MockVerifier, PaymentRejected
from muse.settlement.x402 import (
    PAYMENT_REQUIRED_HEADER,
    InvalidPaymentHeader,
    encode_payment,
    payment_required,
    read_payment_header,
    usdc_sepolia_option,
)


def _verifier(*, quotes: QuoteStore | None = None, clock=None) -> MockVerifier:
    return MockVerifier(
        network=BASE_SEPOLIA_CAIP2,
        asset=BASE_SEPOLIA_USDC,
        pay_to=default_pay_to(),
        price_atomic=10_000,
        quotes=quotes,
        clock_wall=clock,
    )


def test_payment_required_document_is_sepolia_usdc():
    option = usdc_sepolia_option(amount_atomic=10_000, pay_to=default_pay_to())
    body, headers = payment_required(
        option=option,
        resource_url="http://127.0.0.1:4100/v1/results/demo",
        description="verified result",
    )
    assert body["x402Version"] == 1
    accept = body["accepts"][0]
    assert accept["network"] == "base-sepolia"
    assert accept["asset"] == BASE_SEPOLIA_USDC
    assert accept["maxAmountRequired"] == "10000"
    assert accept["payTo"] == default_pay_to()
    assert PAYMENT_REQUIRED_HEADER in headers


def test_mock_payment_is_accepted():
    verifier = _verifier()
    payload = make_mock_payment(amount=10_000, as_header=False)
    result = verifier.verify(payload)
    assert result.accepted is True
    assert result.settled is False
    assert result.mode == "mock"
    assert result.network == BASE_SEPOLIA_CAIP2
    assert "no funds moved" in result.detail
    assert result.nonce == quote_nonce("test-quote")


def test_insufficient_amount_rejected():
    verifier = _verifier()
    payload = make_mock_payment(amount=1, as_header=False)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "insufficient"


def test_overpay_is_also_rejected_exact_scheme():
    verifier = _verifier()
    payload = make_mock_payment(amount=10_001, as_header=False)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "wrong_amount"


def test_scheme_must_be_exact():
    verifier = _verifier()
    payload = make_mock_payment(amount=10_000, as_header=False, scheme="upto")
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "wrong_scheme"
    payload = make_mock_payment(amount=10_000, as_header=False, scheme="EXACT")
    # scheme is folded to lowercase
    assert verifier.verify(payload).accepted


def test_mainnet_payload_is_refused():
    verifier = _verifier()
    for network, _ in (
        ("eip155:8453", "mainnet_refused"),
        ("EIP155:8453", "mainnet_refused"),
        ("base-mainnet", "mainnet_refused"),
        ("Mainnet", "mainnet_refused"),
        ("eip155:1", "mainnet_refused"),
    ):
        payload = make_mock_payment(amount=10_000, network=network, as_header=False)
        with pytest.raises(PaymentRejected) as exc:
            verifier.verify(payload)
        assert exc.value.code in {"mainnet_refused", "wrong_network"}
    assert is_mainnet_network(BASE_MAINNET_CAIP2)
    assert not is_allowed_network("Mainnet")
    assert not is_allowed_network("EIP155:8453")
    assert not is_allowed_network("base-mainnet")
    assert is_allowed_network("EIP155:84532")
    assert is_allowed_network("Base-Sepolia")


def test_settings_refuse_mainnet_startup():
    with pytest.raises(ValueError, match="mainnet"):
        load_settings({"MUSE_X402_NETWORK": "eip155:8453", "MUSE_X402_FACILITATOR": "mock"})
    with pytest.raises(ValueError, match="mainnet"):
        load_settings({"MUSE_X402_NETWORK": "base", "MUSE_X402_FACILITATOR": "mock"})
    # The old denylist missed these spellings; the allow-list must catch them.
    for value in ("Mainnet", "EIP155:8453", "base-mainnet"):
        with pytest.raises(ValueError, match="mainnet"):
            load_settings({"MUSE_X402_NETWORK": value, "MUSE_X402_FACILITATOR": "mock"})


def test_settings_accept_sepolia_aliases_case_insensitive():
    for value in ("eip155:84532", "EIP155:84532", "base-sepolia", "Base_Sepolia"):
        s = load_settings({"MUSE_X402_NETWORK": value, "MUSE_X402_FACILITATOR": "mock"})
        assert s.x402_network == BASE_SEPOLIA_CAIP2


def test_settings_refuse_live_facilitator():
    with pytest.raises(ValueError, match="mock verifier only"):
        load_settings({"MUSE_X402_FACILITATOR": "https://x402.org/facilitator"})


def test_non_mock_signature_rejected():
    verifier = _verifier()
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": BASE_SEPOLIA_CAIP2,
        "asset": BASE_SEPOLIA_USDC,
        "quoteId": "test-quote",
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0x1111111111111111111111111111111111111111",
                "to": default_pay_to(),
                "value": "10000",
                "nonce": quote_nonce("test-quote"),
            },
        },
    }
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "not_mock"


def test_wrong_payee_rejected():
    verifier = _verifier()
    payload = make_mock_payment(
        amount=10_000,
        pay_to="0x2222222222222222222222222222222222222222",
        as_header=False,
    )
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "wrong_payee"


def test_unsigned_top_level_amount_and_payto_are_ignored():
    """A crafted top-level amount/payTo must not override the signed fields."""
    verifier = _verifier()
    payload = make_mock_payment(amount=1, as_header=False)
    # Unsigned fields claim the correct values; signed value is still 1.
    payload["amount"] = "10000"
    payload["payTo"] = default_pay_to()
    payload["value"] = "10000"
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "insufficient"

    # Signed payTo is wrong, unsigned top-level is right.
    payload = make_mock_payment(
        amount=10_000,
        pay_to="0x2222222222222222222222222222222222222222",
        as_header=False,
    )
    payload["payTo"] = default_pay_to()
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "wrong_payee"


def test_nonce_must_equal_hash_of_quote_id_and_cannot_be_replayed():
    store = QuoteStore()
    verifier = _verifier(quotes=store)
    payload = make_mock_payment(amount=10_000, as_header=False, quote_id="quote-abc")
    assert payload["payload"]["authorization"]["nonce"] == quote_nonce("quote-abc")
    first = verifier.verify(payload)
    assert first.accepted
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "replay"

    # A payment with a nonce that is not SHA-256(quoteId) is rejected.
    other = make_mock_payment(
        amount=10_000, as_header=False, quote_id="quote-xyz", nonce="0xdead"
    )
    with pytest.raises(PaymentRejected) as exc:
        _verifier().verify(other)
    assert exc.value.code == "wrong_nonce"


def test_validity_window_and_quote_expiry():
    now = 1_700_000_000.0
    verifier = _verifier(clock=lambda: now)
    future = make_mock_payment(
        amount=10_000, as_header=False, valid_after=str(int(now) + 10)
    )
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(future)
    assert exc.value.code == "not_yet_valid"

    expired = make_mock_payment(
        amount=10_000, as_header=False, valid_before=str(int(now) - 1)
    )
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(expired)
    assert exc.value.code == "expired"

    store = QuoteStore()
    quote = store.issue(
        result_id="r1",
        amount="10000",
        pay_to=default_pay_to(),
        asset=BASE_SEPOLIA_USDC,
        network=BASE_SEPOLIA_CAIP2,
        ttl_seconds=30,
        now_wall=now - 60,  # already expired
    )
    verifier = _verifier(quotes=store, clock=lambda: now)
    payload = make_mock_payment(amount=10_000, as_header=False, quote=quote)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload, quote=quote, result_id="r1")
    assert exc.value.code == "quote_expired"


def test_payment_bound_to_result():
    store = QuoteStore()
    now = 1_700_000_000.0
    quote = store.issue(
        result_id="r1",
        amount="10000",
        pay_to=default_pay_to(),
        asset=BASE_SEPOLIA_USDC,
        network=BASE_SEPOLIA_CAIP2,
        ttl_seconds=60,
        now_wall=now,
    )
    verifier = _verifier(quotes=store, clock=lambda: now)
    payload = make_mock_payment(amount=10_000, as_header=False, quote=quote)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload, quote=quote, result_id="someone-elses-result")
    assert exc.value.code == "wrong_resource"
    assert verifier.verify(payload, quote=quote, result_id="r1").accepted


def test_read_payment_header_v2_and_v1_and_raw_json():
    payload = make_mock_payment(amount=10_000, as_header=False)
    encoded = encode_payment(payload)
    assert read_payment_header({"PAYMENT-SIGNATURE": encoded})["scheme"] == "exact"
    assert read_payment_header({"X-PAYMENT": encoded})["scheme"] == "exact"
    assert read_payment_header({"PAYMENT-SIGNATURE": "mock:demo-receipt-1"})["mock_token"].startswith("mock:")
    assert read_payment_header({}) is None


def test_malformed_payment_header_raises_typed_error():
    with pytest.raises(InvalidPaymentHeader):
        read_payment_header({"PAYMENT-SIGNATURE": "²"})
    with pytest.raises(InvalidPaymentHeader):
        read_payment_header({"PAYMENT-SIGNATURE": "not-valid-base64%%%"})
    with pytest.raises(InvalidPaymentHeader):
        read_payment_header({"X-PAYMENT": "[[["})


def test_mock_token_header_needs_signed_authorization():
    verifier = _verifier()
    parsed = read_payment_header({"PAYMENT-SIGNATURE": "mock:ok"})
    # Top-level unsigned amount / payTo must not be enough.
    parsed["asset"] = BASE_SEPOLIA_USDC
    parsed["payTo"] = default_pay_to()
    parsed["amount"] = "10000"
    parsed["mock"] = True
    parsed["quoteId"] = "test-quote"
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(parsed)
    assert exc.value.code in {"missing_nonce", "wrong_payee", "bad_amount", "unbound"}
    # A full signed authorization is the documented path:
    parsed["payload"] = {
        "signature": "0xmock",
        "authorization": {
            "from": "0x1111111111111111111111111111111111111111",
            "to": default_pay_to(),
            "value": "10000",
            "validAfter": "0",
            "validBefore": "9999999999",
            "nonce": quote_nonce("test-quote"),
        },
    }
    assert verifier.verify(parsed).accepted
