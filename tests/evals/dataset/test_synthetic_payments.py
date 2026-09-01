import random
from decimal import Decimal
from datetime import date

import pytest

from evals.dataset.synthetic_payments import (
    REFERENCE_DATE,
    calibrate_fee_model,
    generate_synthetic_payment,
    random_amount_paise,
    random_captured_at,
)
from precedent.adapters.storage.records import PaymentRecord


def make_real_payment(amount_paise, fee_paise, tax_paise):
    return PaymentRecord(
        payment_id="pay_x", order_id="order_x", amount_paise=amount_paise,
        fee_paise=fee_paise, tax_paise=tax_paise, captured_at="2026-01-01T00:00:00+00:00",
        status="captured", source="razorpay",
    )


class TestCalibrateFeeModel:
    def test_computes_mean_fee_and_tax_rates(self):
        payments = [
            make_real_payment(10_000, 236, 36),
            make_real_payment(20_000, 472, 72),
        ]
        fee_rate, tax_rate = calibrate_fee_model(payments)
        assert fee_rate == Decimal("0.0236")
        assert tax_rate == Decimal(36) / Decimal(236)

    def test_rejects_an_empty_payment_list(self):
        with pytest.raises(ValueError):
            calibrate_fee_model([])

    def test_handles_zero_fee_without_dividing_by_zero(self):
        fee_rate, tax_rate = calibrate_fee_model([make_real_payment(100, 0, 0)])
        assert fee_rate == Decimal(0)
        assert tax_rate == Decimal(0)


class TestRandomAmountAndCapturedAt:
    def test_random_amount_is_a_whole_rupee_value(self):
        rng = random.Random(1)
        for _ in range(20):
            amount = random_amount_paise(rng)
            assert amount % 100 == 0
            assert amount > 0

    def test_random_captured_at_falls_within_the_reference_window(self):
        rng = random.Random(1)
        for _ in range(20):
            captured_at = random_captured_at(rng)
            captured_date = date.fromisoformat(captured_at[:10])
            assert captured_date <= REFERENCE_DATE
            assert (REFERENCE_DATE - captured_date).days <= 75

    def test_same_seed_produces_identical_sequence(self):
        seq_a = [random_amount_paise(random.Random(3)) for _ in range(5)]
        seq_b = [random_amount_paise(random.Random(3)) for _ in range(5)]
        assert seq_a == seq_b


class TestGenerateSyntheticPayment:
    def test_applies_fee_and_tax_rates(self):
        payment = generate_synthetic_payment(
            random.Random(1), order_id="order_1", amount_paise=10_000,
            fee_rate=Decimal("0.0236"), tax_rate=Decimal("0.18"),
        )
        assert payment.fee_paise == 236  # 10_000 * 0.0236, half-up to the paise
        assert payment.tax_paise == 42  # 236 * 0.18 = 42.48 -> 42

    def test_is_tagged_synthetic_and_captured(self):
        payment = generate_synthetic_payment(random.Random(1), "order_1", 10_000, Decimal("0.02"), Decimal("0.18"))
        assert payment.source == "synthetic"
        assert payment.status == "captured"

    def test_uses_the_given_captured_at_when_provided(self):
        payment = generate_synthetic_payment(
            random.Random(1), "order_1", 10_000, Decimal("0.02"), Decimal("0.18"),
            captured_at="2026-03-15T12:00:00+00:00",
        )
        assert payment.captured_at == "2026-03-15T12:00:00+00:00"
