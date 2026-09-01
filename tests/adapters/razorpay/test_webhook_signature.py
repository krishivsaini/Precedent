from pathlib import Path

from precedent.adapters.razorpay.webhook_signature import (
    compute_signature,
    verify_webhook_signature,
)

FIXTURE_PATH = Path(__file__).parent.parent.parent / "fixtures" / "webhook_payment_captured.json"
WEBHOOK_SECRET = "test_webhook_secret_do_not_use_in_prod"


def test_verifies_a_correctly_signed_fixture_body():
    raw_body = FIXTURE_PATH.read_bytes()
    signature = compute_signature(raw_body, WEBHOOK_SECRET)

    assert verify_webhook_signature(raw_body, signature, WEBHOOK_SECRET) is True


def test_rejects_a_tampered_body_against_the_original_signature():
    # The classic failure (spec §10): raw-body signature verification against fixture
    # bytes. A single byte changed in the body (amount tampered from 500000 to 900000)
    # must invalidate a signature that was computed over the original bytes.
    original_body = FIXTURE_PATH.read_bytes()
    signature = compute_signature(original_body, WEBHOOK_SECRET)

    tampered_body = original_body.replace(b'"amount":500000', b'"amount":900000')
    assert tampered_body != original_body  # sanity check the tamper actually changed bytes

    assert verify_webhook_signature(tampered_body, signature, WEBHOOK_SECRET) is False


def test_rejects_the_correct_body_signed_with_the_wrong_secret():
    raw_body = FIXTURE_PATH.read_bytes()
    signature = compute_signature(raw_body, "a_completely_different_secret")

    assert verify_webhook_signature(raw_body, signature, WEBHOOK_SECRET) is False


def test_rejects_an_empty_signature():
    raw_body = FIXTURE_PATH.read_bytes()
    assert verify_webhook_signature(raw_body, "", WEBHOOK_SECRET) is False
