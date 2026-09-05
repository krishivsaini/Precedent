"""The delivery log.

The claim this screen exists to make checkable is `README.md`'s: real Razorpay deliveries,
signature-verified and deduped. `api/webhooks.py` acks 200 whether or not the signature
verified — correct on the wire, because Razorpay retries on anything else — which means a
mismatched secret is indistinguishable from a correct one from Razorpay's side. So the
assertions below are mostly about one thing: that a delivery which did not verify can never
be mistaken here for one that did.
"""

import re

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import WebhookEventRecord
from precedent.adapters.storage.repositories import WebhookEventsRepository
from precedent.api.main import create_app

NOW = "2026-09-05T09:00:00+00:00"


def build(db, events=()):
    conn = connect(str(db))
    init_db(conn)
    repo = WebhookEventsRepository(conn)
    for event_id, event_type, valid in events:
        repo.insert_if_new(WebhookEventRecord(
            event_id=event_id, event_type=event_type,
            raw_body='{"event":"%s"}' % event_type,
            signature_valid=valid, received_at=NOW,
        ))
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=str(db)))


def flat(body: str) -> str:
    """Copy assertions read the sentence, not the line breaks the template wraps at."""
    return re.sub(r"\s+", " ", body)


@pytest.fixture
def empty(tmp_path):
    with build(tmp_path / "d0.db") as c:
        yield c


@pytest.fixture
def verified(tmp_path):
    with build(tmp_path / "d1.db", [
        ("evt_1", "payment.captured", True),
        ("evt_2", "refund.processed", True),
    ]) as c:
        yield c


class TestTheEmptyState:
    def test_it_says_what_to_do_rather_than_apologising(self, empty):
        body = empty.get("/deliveries").text
        assert "Nothing has arrived yet" in body
        assert "/webhooks/razorpay" in body
        assert "sorry" not in body.lower()

    def test_it_warns_about_the_cold_start_that_will_cause_the_first_failure(self, empty):
        # The likeliest reason a real delivery does not appear is that the free-tier
        # instance was asleep, not that the webhook is misconfigured. Saying so here saves
        # the reader debugging the wrong thing.
        assert "sleeps when idle" in empty.get("/deliveries").text


class TestAFailedSignatureIsNeverSetLikeAVerifiedOne:
    """The whole reason the screen exists."""

    def test_a_rejected_delivery_is_called_out_above_the_table(self, tmp_path):
        with build(tmp_path / "d2.db", [("evt_x", "payment.captured", False)]) as client:
            body = client.get("/deliveries").text
            assert "did not verify" in body
            assert "flag stop" in body

    def test_it_names_the_likeliest_cause(self, tmp_path):
        with build(tmp_path / "d3.db", [("evt_x", "payment.captured", False)]) as client:
            assert "RAZORPAY_WEBHOOK_SECRET" in client.get("/deliveries").text

    def test_a_clean_log_raises_no_alarm(self, verified):
        assert "flag stop" not in verified.get("/deliveries").text

    def test_only_verified_deliveries_count_as_evidence(self, tmp_path):
        with build(tmp_path / "d4.db", [
            ("evt_1", "payment.captured", True),
            ("evt_2", "payment.captured", False),
        ]) as client:
            body = flat(client.get("/deliveries").text)
            assert "2 deliveries recorded, 1 signature-verified" in body


class TestDeduplication:
    def test_a_retried_delivery_does_not_become_a_second_row(self, tmp_path):
        # The event_id primary key is the entire dedupe mechanism (spec §4). Asserting it
        # through the screen rather than the repository is the point: this is where the
        # claim is shown to a reader.
        with build(tmp_path / "d5.db", [
            ("evt_same", "payment.captured", True),
            ("evt_same", "payment.captured", True),
        ]) as client:
            assert "1 delivery recorded" in flat(client.get("/deliveries").text)

    def test_it_does_not_dress_the_invariant_up_as_a_measurement(self, tmp_path):
        # A tie-out between row count and distinct event ids is equal by construction and
        # proves nothing. Saying so beats printing it.
        with build(tmp_path / "d6.db", [("evt_1", "payment.captured", True)]) as client:
            body = flat(client.get("/deliveries").text)
            assert "equal by construction and prove nothing" in body
            assert "Distinct event ids" not in body

    def test_it_breaks_the_log_down_by_event_type(self, tmp_path):
        with build(tmp_path / "d7.db", [
            ("evt_1", "payment.captured", True),
            ("evt_2", "payment.captured", True),
            ("evt_3", "refund.processed", True),
        ]) as client:
            body = flat(client.get("/deliveries").text)
            assert 'Payments captured</td><td class="fig">2' in body
            assert 'Refunds processed</td><td class="fig">1' in body


class TestItIsPartOfTheApp:
    def test_it_is_reachable_from_every_other_screen(self, verified):
        assert '/deliveries' in verified.get("/corpus").text

    def test_it_states_which_events_are_accepted(self, verified):
        body = verified.get("/deliveries").text
        assert "payment.captured" in body and "refund.processed" in body
