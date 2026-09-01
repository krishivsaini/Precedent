import random

import pytest

from evals.dataset.generators import (
    generate_bank_line_for_payment,
    generate_ledger_entry_for_payment,
    generate_netted_bank_line,
)
from precedent.adapters.storage.records import PaymentRecord


def make_payment(payment_id="pay_1", order_id="order_1", amount_paise=10_000, fee_paise=200,
                  tax_paise=36, captured_at="2026-01-01T10:00:00+00:00"):
    return PaymentRecord(
        payment_id=payment_id, order_id=order_id, amount_paise=amount_paise,
        fee_paise=fee_paise, tax_paise=tax_paise, captured_at=captured_at,
        status="captured", source="razorpay",
    )


class TestGenerateBankLineForPayment:
    def test_nets_out_fee_and_tax(self):
        payment = make_payment(amount_paise=10_000, fee_paise=200, tax_paise=36)
        line = generate_bank_line_for_payment(payment, random.Random(42))
        assert line.amount_paise == 10_000 - 200 - 36

    def test_value_date_is_after_captured_at_within_lag_range(self):
        payment = make_payment(captured_at="2026-01-01T10:00:00+00:00")
        line = generate_bank_line_for_payment(payment, random.Random(42), lag_days=(1, 3))
        from datetime import date
        assert date(2026, 1, 2) <= date.fromisoformat(line.value_date) <= date(2026, 1, 4)

    def test_is_a_synthetic_credit_line(self):
        payment = make_payment()
        line = generate_bank_line_for_payment(payment, random.Random(1))
        assert line.direction == "credit"
        assert line.source == "synthetic"

    def test_same_seed_produces_identical_output(self):
        payment = make_payment()
        line_a = generate_bank_line_for_payment(payment, random.Random(7))
        line_b = generate_bank_line_for_payment(payment, random.Random(7))
        assert line_a == line_b

    def test_different_seed_produces_a_different_line_id(self):
        payment = make_payment()
        line_a = generate_bank_line_for_payment(payment, random.Random(1))
        line_b = generate_bank_line_for_payment(payment, random.Random(2))
        assert line_a.line_id != line_b.line_id


class TestGenerateNettedBankLine:
    def test_amount_is_the_sum_net_of_fees_and_tax(self):
        payments = [
            make_payment("pay_1", "order_1", 10_000, fee_paise=200, tax_paise=36),
            make_payment("pay_2", "order_2", 5_000, fee_paise=100, tax_paise=18),
        ]
        line = generate_netted_bank_line(payments, random.Random(3))
        assert line.amount_paise == (10_000 - 200 - 36) + (5_000 - 100 - 18)

    def test_value_date_is_relative_to_the_latest_payment(self):
        from datetime import date
        payments = [
            make_payment("pay_1", "order_1", captured_at="2026-01-01T10:00:00+00:00"),
            make_payment("pay_2", "order_2", captured_at="2026-01-05T10:00:00+00:00"),
        ]
        line = generate_netted_bank_line(payments, random.Random(3), lag_days=(2, 2))
        assert date.fromisoformat(line.value_date) == date(2026, 1, 7)

    def test_rejects_an_empty_payment_list(self):
        with pytest.raises(ValueError):
            generate_netted_bank_line([], random.Random(1))


class TestGenerateLedgerEntryForPayment:
    def test_defaults_expected_amount_to_the_payment_amount(self):
        payment = make_payment(amount_paise=10_000)
        entry = generate_ledger_entry_for_payment(payment, random.Random(5))
        assert entry.expected_amount_paise == 10_000

    def test_allows_a_deliberate_mismatch_for_exception_scenarios(self):
        payment = make_payment(amount_paise=10_000)
        entry = generate_ledger_entry_for_payment(payment, random.Random(5), expected_amount_paise=9_800)
        assert entry.expected_amount_paise == 9_800

    def test_order_id_matches_the_payment(self):
        payment = make_payment(order_id="order_xyz")
        entry = generate_ledger_entry_for_payment(payment, random.Random(5))
        assert entry.order_id == "order_xyz"

    def test_same_seed_produces_identical_output(self):
        payment = make_payment()
        entry_a = generate_ledger_entry_for_payment(payment, random.Random(9))
        entry_b = generate_ledger_entry_for_payment(payment, random.Random(9))
        assert entry_a == entry_b
