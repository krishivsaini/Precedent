from precedent.adapters.storage.records import WebhookEventRecord
from precedent.adapters.storage.repositories import WebhookEventsRepository


def make_event(event_id="evt_1", event_type="payment.captured", signature_valid=True):
    return WebhookEventRecord(
        event_id=event_id,
        event_type=event_type,
        raw_body='{"payment_id": "pay_1"}',
        signature_valid=signature_valid,
        received_at="2026-01-01T00:00:00Z",
    )


def test_insert_if_new_records_a_first_delivery(conn):
    repo = WebhookEventsRepository(conn)
    assert repo.insert_if_new(make_event()) is True
    assert repo.get("evt_1") is not None


def test_insert_if_new_is_a_no_op_on_replay(conn):
    # This is the idempotent-replay test called out explicitly in spec §10: the same
    # webhook event delivered twice must not create a second row or reprocess.
    repo = WebhookEventsRepository(conn)
    repo.insert_if_new(make_event())

    was_new = repo.insert_if_new(make_event())

    assert was_new is False
    rows = conn.execute("SELECT COUNT(*) AS n FROM webhook_events").fetchone()
    assert rows["n"] == 1


def test_mark_processed_sets_processed_at(conn):
    repo = WebhookEventsRepository(conn)
    repo.insert_if_new(make_event())

    repo.mark_processed("evt_1", "2026-01-01T00:00:05Z")

    event = repo.get("evt_1")
    assert event.processed_at == "2026-01-01T00:00:05Z"


def test_rejects_an_event_type_outside_the_two_real_webhook_types(conn):
    import pytest

    # Deliberately not `sqlite3.IntegrityError`: `INSERT OR IGNORE` swallows CHECK
    # violations the same way it swallows a duplicate-key conflict (rowcount 0, no
    # exception) — the repo validates explicitly so a malformed event_type is loud
    # instead of silently indistinguishable from a harmless replay.
    repo = WebhookEventsRepository(conn)
    with pytest.raises(ValueError):
        repo.insert_if_new(make_event(event_type="order.paid"))
