"""Transaction-boundary behaviour: repositories must never commit on their own.

This is what makes Ring 3's deposit loop safe — writing the human action, the audit row,
and the precedent as one atomic unit, so a crash can't leave a precedent with no audit
trail behind it (NFR-5).
"""

import pytest

from precedent.adapters.storage.db import connect, init_db, transaction
from precedent.adapters.storage.records import PaymentRecord
from precedent.adapters.storage.repositories import PaymentsRepository


def make_payment(payment_id="pay_1"):
    return PaymentRecord(
        payment_id=payment_id, order_id="order_1", amount_paise=10_000,
        captured_at="2026-01-01T00:00:00Z", status="captured", source="razorpay",
    )


def test_repository_write_is_not_committed_on_its_own(tmp_path):
    db_path = str(tmp_path / "t.db")
    writer = connect(db_path)
    init_db(writer)

    PaymentsRepository(writer).insert(make_payment())

    # Visible on the writing connection (it reads its own uncommitted writes)...
    assert PaymentsRepository(writer).get("pay_1") is not None
    # ...but not to anyone else, because nothing committed.
    reader = connect(db_path)
    assert PaymentsRepository(reader).get("pay_1") is None

    writer.close()
    reader.close()


def test_transaction_commits_on_success(tmp_path):
    db_path = str(tmp_path / "t.db")
    writer = connect(db_path)
    init_db(writer)

    with transaction(writer):
        PaymentsRepository(writer).insert(make_payment())

    reader = connect(db_path)
    assert PaymentsRepository(reader).get("pay_1") is not None
    writer.close()
    reader.close()


def test_transaction_rolls_back_every_write_on_failure(tmp_path):
    # The whole point: a multi-write unit either lands entirely or not at all.
    db_path = str(tmp_path / "t.db")
    writer = connect(db_path)
    init_db(writer)

    with pytest.raises(RuntimeError):
        with transaction(writer):
            PaymentsRepository(writer).insert(make_payment("pay_1"))
            PaymentsRepository(writer).insert(make_payment("pay_2"))
            raise RuntimeError("boom, midway through the unit")

    reader = connect(db_path)
    repo = PaymentsRepository(reader)
    assert repo.get("pay_1") is None
    assert repo.get("pay_2") is None
    writer.close()
    reader.close()
