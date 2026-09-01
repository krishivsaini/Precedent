import sqlite3

import pytest

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord
from precedent.adapters.storage.repositories import (
    BankLinesRepository,
    LedgerEntriesRepository,
    PaymentsRepository,
)


class TestPaymentsRepository:
    def test_insert_and_get_round_trip(self, conn):
        repo = PaymentsRepository(conn)
        record = PaymentRecord(
            payment_id="pay_1", order_id="order_1", amount_paise=10_000,
            fee_paise=200, tax_paise=36, captured_at="2026-01-01T00:00:00Z",
            status="captured", source="razorpay",
        )
        repo.insert(record)
        assert repo.get("pay_1") == record

    def test_get_missing_payment_returns_none(self, conn):
        repo = PaymentsRepository(conn)
        assert repo.get("does_not_exist") is None

    def test_list_by_order_is_sorted_by_captured_at(self, conn):
        repo = PaymentsRepository(conn)
        later = PaymentRecord(
            payment_id="pay_later", order_id="order_1", amount_paise=1_000,
            captured_at="2026-01-02T00:00:00Z", status="captured", source="razorpay",
        )
        earlier = PaymentRecord(
            payment_id="pay_earlier", order_id="order_1", amount_paise=1_000,
            captured_at="2026-01-01T00:00:00Z", status="captured", source="razorpay",
        )
        repo.insert(later)
        repo.insert(earlier)

        results = repo.list_by_order("order_1")

        assert [r.payment_id for r in results] == ["pay_earlier", "pay_later"]

    def test_rejects_a_non_razorpay_non_synthetic_source(self, conn):
        repo = PaymentsRepository(conn)
        record = PaymentRecord(
            payment_id="pay_1", order_id="order_1", amount_paise=1_000,
            captured_at="2026-01-01T00:00:00Z", status="captured", source="made_up",
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert(record)

    def test_rejects_a_float_amount_paise(self, conn):
        # Storage-layer half of NFR-1: even a caller that bypasses domain.money must not
        # be able to persist a float paise amount.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO payments
                    (payment_id, order_id, amount_paise, captured_at, status, source)
                VALUES ('pay_1', 'order_1', 100.5, '2026-01-01T00:00:00Z', 'captured', 'razorpay')
                """
            )


class TestBankLinesRepository:
    def test_insert_and_get_round_trip(self, conn):
        repo = BankLinesRepository(conn)
        record = BankLineRecord(
            line_id="line_1", value_date="2026-01-01", amount_paise=10_000,
            direction="credit", narration="NEFT credit",
        )
        repo.insert(record)
        assert repo.get("line_1") == record

    def test_rejects_a_direction_outside_credit_or_debit(self, conn):
        repo = BankLinesRepository(conn)
        record = BankLineRecord(line_id="line_1", value_date="2026-01-01", amount_paise=100, direction="sideways")
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert(record)

    def test_list_all_orders_by_value_date(self, conn):
        repo = BankLinesRepository(conn)
        repo.insert(BankLineRecord(line_id="line_2", value_date="2026-01-02", amount_paise=100, direction="credit"))
        repo.insert(BankLineRecord(line_id="line_1", value_date="2026-01-01", amount_paise=100, direction="credit"))
        assert [r.line_id for r in repo.list_all()] == ["line_1", "line_2"]


class TestLedgerEntriesRepository:
    def test_insert_and_get_by_order(self, conn):
        repo = LedgerEntriesRepository(conn)
        record = LedgerEntryRecord(
            entry_id="led_1", order_id="order_1", expected_amount_paise=10_000,
            invoice_no="INV-001", customer_name="Acme Co",
        )
        repo.insert(record)
        assert repo.get_by_order("order_1") == record

    def test_get_by_order_missing_returns_none(self, conn):
        repo = LedgerEntriesRepository(conn)
        assert repo.get_by_order("no_such_order") is None
