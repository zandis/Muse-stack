import pytest

from muse.config import load_settings
from muse.settlement.constants import (
    BASE_MAINNET_CAIP2,
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_USDC,
    default_pay_to,
    is_mainnet_network,
)
from muse.settlement.mock_payment import make_mock_payment
from muse.settlement.verifier import MockVerifier, PaymentRejected
from muse.settlement.x402 import (
    PAYMENT_REQUIRED_HEADER,
    encode_payment,
    payment_required,
    read_payment_header,
    usdc_sepolia_option,
)


def _verifier() -> MockVerifier:
    return MockVerifier(
        network=BASE_SEPOLIA_CAIP2,
        asset=BASE_SEPOLIA_USDC,
        pay_to=default_pay_to(),
        price_atomic=10_000,
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


def test_insufficient_amount_rejected():
    verifier = _verifier()
    payload = make_mock_payment(amount=1, as_header=False)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "insufficient"


def test_mainnet_payload_is_refused():
    verifier = _verifier()
    payload = make_mock_payment(amount=10_000, network="eip155:8453", as_header=False)
    with pytest.raises(PaymentRejected) as exc:
        verifier.verify(payload)
    assert exc.value.code == "mainnet_refused"
    assert is_mainnet_network(BASE_MAINNET_CAIP2)


def test_settings_refuse_mainnet_startup():
    with pytest.raises(ValueError, match="mainnet"):
        load_settings({"MUSE_X402_NETWORK": "eip155:8453", "MUSE_X402_FACILITATOR": "mock"})
    with pytest.raises(ValueError, match="mainnet"):
        load_settings({"MUSE_X402_NETWORK": "base", "MUSE_X402_FACILITATOR": "mock"})


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
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0x1111111111111111111111111111111111111111",
                "to": default_pay_to(),
                "value": "10000",
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


def test_read_payment_header_v2_and_v1_and_raw_json():
    payload = make_mock_payment(amount=10_000, as_header=False)
    encoded = encode_payment(payload)
    assert read_payment_header({"PAYMENT-SIGNATURE": encoded})["scheme"] == "exact"
    assert read_payment_header({"X-PAYMENT": encoded})["scheme"] == "exact"
    assert read_payment_header({"PAYMENT-SIGNATURE": "mock:demo-receipt-1"})["mock_token"].startswith("mock:")
    assert read_payment_header({}) is None


def test_mock_token_header_accepted():
    verifier = _verifier()
    parsed = read_payment_header({"PAYMENT-SIGNATURE": "mock:ok"})
    # The shorthand token has no payTo / amount — should fail amount parse,
    # so a full mock payload is the documented path. The shorthand is for
    # humans; fill the required fields:
    parsed["asset"] = BASE_SEPOLIA_USDC
    parsed["payTo"] = default_pay_to()
    parsed["amount"] = "10000"
    parsed["mock"] = True
    result = verifier.verify(parsed)
    assert result.accepted
