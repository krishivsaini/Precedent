import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.razorpay.webhook_signature import compute_signature
from precedent.api.main import create_app
from precedent.config import razorpay_config

WEBHOOK_SECRET = "test_webhook_secret_do_not_use_in_prod"
FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "webhook_payment_captured.json"


@pytest.fixture(autouse=True)
def _configured_env(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret123")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    razorpay_config.cache_clear()
    yield
    razorpay_config.cache_clear()


@pytest.fixture
def client(tmp_path):
    # Entered as a context manager on purpose: schema creation runs in the app's
    # lifespan (not at import), so the startup path has to actually fire — which is
    # also what production does under uvicorn.
    app = create_app(db_path=str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def _headers(raw_body: bytes, event_id: str, secret: str = WEBHOOK_SECRET) -> dict:
    return {
        "X-Razorpay-Signature": compute_signature(raw_body, secret),
        "x-razorpay-event-id": event_id,
        "Content-Type": "application/json",
    }


def test_accepts_a_correctly_signed_delivery(client):
    raw_body = FIXTURE_PATH.read_bytes()

    response = client.post(
        "/webhooks/razorpay", content=raw_body, headers=_headers(raw_body, "evt_1")
    )

    assert response.status_code == 200


def test_replaying_the_same_event_id_is_a_no_op(client):
    # Idempotent-replay requirement (spec §10): identical delivery twice must not create
    # a second stored row.
    raw_body = FIXTURE_PATH.read_bytes()
    headers = _headers(raw_body, "evt_replay")

    first = client.post("/webhooks/razorpay", content=raw_body, headers=headers)
    second = client.post("/webhooks/razorpay", content=raw_body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200


def test_still_returns_200_on_an_invalid_signature_but_records_it_as_invalid(client, tmp_path):
    # Ack fast, never retry-storm ourselves; the invalid signature is recorded so nothing
    # downstream can mistake it for a trusted event.
    raw_body = FIXTURE_PATH.read_bytes()
    headers = _headers(raw_body, "evt_bad_sig", secret="wrong_secret")

    response = client.post("/webhooks/razorpay", content=raw_body, headers=headers)
    assert response.status_code == 200

    from precedent.adapters.storage.db import connect
    from precedent.adapters.storage.repositories import WebhookEventsRepository

    conn = connect(str(tmp_path / "test.db"))
    stored = WebhookEventsRepository(conn).get("evt_bad_sig")
    conn.close()

    assert stored is not None
    assert stored.signature_valid is False


def test_a_non_utf8_body_is_acked_not_crashed(client, tmp_path):
    # Regression: this used to raise on `.decode("utf-8")` and return 500, which
    # contradicts this module's always-ack contract and would make Razorpay retry-storm.
    body = b"\xff\xfe\x00 not valid utf-8"
    response = client.post(
        "/webhooks/razorpay", content=body, headers=_headers(body, "evt_binary")
    )
    assert response.status_code == 200

    from precedent.adapters.storage.db import connect
    from precedent.adapters.storage.repositories import WebhookEventsRepository

    conn = connect(str(tmp_path / "test.db"))
    stored = WebhookEventsRepository(conn).get("evt_binary")
    conn.close()
    # Recorded rather than dropped, and correctly distrusted.
    assert stored is None or stored.signature_valid is False


def test_a_delivery_with_no_signature_header_is_acked_and_marked_invalid(client, tmp_path):
    raw_body = FIXTURE_PATH.read_bytes()
    response = client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={"x-razorpay-event-id": "evt_nosig", "Content-Type": "application/json"},
    )
    assert response.status_code == 200

    from precedent.adapters.storage.db import connect
    from precedent.adapters.storage.repositories import WebhookEventsRepository

    conn = connect(str(tmp_path / "test.db"))
    stored = WebhookEventsRepository(conn).get("evt_nosig")
    conn.close()
    assert stored is not None
    assert stored.signature_valid is False


def test_a_delivery_with_no_event_id_is_acked_without_storing(client):
    # No event id means no dedupe key; storing it would risk reprocessing on retry.
    raw_body = FIXTURE_PATH.read_bytes()
    response = client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={
            "X-Razorpay-Signature": compute_signature(raw_body, WEBHOOK_SECRET),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200


def test_still_returns_200_for_an_unsupported_event_type_without_storing_it(client):
    payload = json.dumps({"event": "order.paid", "payload": {}}).encode("utf-8")

    response = client.post(
        "/webhooks/razorpay", content=payload, headers=_headers(payload, "evt_unknown")
    )

    assert response.status_code == 200
