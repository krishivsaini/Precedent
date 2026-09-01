import random
from datetime import date
from decimal import Decimal

import pytest

from evals.dataset import builders
from evals.dataset.convert import to_domain_bank_line, to_domain_ledger_entry, to_domain_payment
from evals.dataset.synthetic_payments import generate_synthetic_payment
from precedent.domain.matching import is_netted_group_match, match_batch
from precedent.domain.money import net_of_tds_paise
from precedent.domain.reasons import ReasonCode

FEE_RATE, TAX_RATE = Decimal("0.0236"), Decimal("0.152")


def make_payment(order_id="order_1", amount_paise=100_000, seed=1):
    return generate_synthetic_payment(
        random.Random(seed), order_id, amount_paise, FEE_RATE, TAX_RATE,
        captured_at="2026-01-01T10:00:00+00:00",
    )


def run_matcher(scenario):
    payments = [to_domain_payment(p) for p in scenario.payments]
    bank_lines = [to_domain_bank_line(b) for b in scenario.bank_lines]
    ledger_entries = [to_domain_ledger_entry(l) for l in scenario.ledger_entries]
    return match_batch(payments, bank_lines, ledger_entries)


class TestBuildCleanMatch:
    def test_resolves_cleanly_via_exact_match(self):
        payment = make_payment()
        scenario = builders.build_clean_match(random.Random(2), "rec_1", payment)

        assert scenario.is_exception is False
        result = run_matcher(scenario)
        assert len(result.matches) == 1
        assert result.matches[0].reason_code == ReasonCode.EXACT_MATCH
        assert result.exceptions == []


class TestBuildRoundingDelta:
    def test_resolves_via_tolerance_match_not_escalated(self):
        payment = make_payment()
        scenario = builders.build_rounding_delta(random.Random(3), "rec_2", payment)

        assert scenario.is_exception is False
        result = run_matcher(scenario)
        assert len(result.matches) == 1
        assert result.matches[0].reason_code in (
            ReasonCode.EXACT_MATCH, ReasonCode.TOLERANCE_ROUNDING,
        )
        assert result.exceptions == []


class TestBuildNettedSettlement:
    def test_resists_the_deterministic_matcher(self):
        payments = [
            make_payment("order_a", 50_000, seed=1),
            make_payment("order_b", 75_000, seed=2),
            make_payment("order_c", 30_000, seed=3),
        ]
        scenario = builders.build_netted_settlement(random.Random(4), "rec_3", payments)

        assert scenario.is_exception is True
        result = run_matcher(scenario)
        assert result.matches == []
        assert len(result.exceptions) > 0

    def test_is_recognized_by_the_netted_group_checker(self):
        payments = [make_payment("order_a", 50_000, seed=1), make_payment("order_b", 75_000, seed=2)]
        scenario = builders.build_netted_settlement(random.Random(5), "rec_4", payments)
        bank_line = to_domain_bank_line(scenario.bank_lines[0])
        domain_payments = [to_domain_payment(p) for p in payments]
        assert is_netted_group_match(domain_payments, bank_line) is True

    def test_rejects_a_single_payment_group(self):
        with pytest.raises(ValueError):
            builders.build_netted_settlement(random.Random(1), "rec_x", [make_payment()])


class TestBuildDirectNeftBypass:
    def test_resists_the_deterministic_matcher_with_no_payment_at_all(self):
        scenario = builders.build_direct_neft_bypass(
            random.Random(6), "rec_5", "order_neft_1", 42_000, "Acme Retail Pvt Ltd",
            date(2026, 2, 1),
        )
        assert scenario.is_exception is True
        assert scenario.payments == []

        result = run_matcher(scenario)
        assert result.matches == []
        ledger_orphans = [e for e in result.exceptions if e.ledger_entry_ids]
        bank_line_orphans = [e for e in result.exceptions if e.bank_line_ids]
        assert len(ledger_orphans) == 1
        assert len(bank_line_orphans) == 1


class TestBuildTdsShortPayment:
    def test_payment_amount_reflects_the_tds_deduction(self):
        scenario = builders.build_tds_short_payment(
            random.Random(7), "rec_6", "order_tds_1", 100_000, Decimal("0.02"), FEE_RATE, TAX_RATE,
        )
        payment = scenario.payments[0]
        assert payment.amount_paise == net_of_tds_paise(100_000, Decimal("0.02"))

    def test_resists_the_deterministic_matcher(self):
        scenario = builders.build_tds_short_payment(
            random.Random(8), "rec_7", "order_tds_2", 100_000, Decimal("0.10"), FEE_RATE, TAX_RATE,
        )
        result = run_matcher(scenario)
        assert result.matches == []
        assert len(result.exceptions) > 0

    def test_can_anchor_on_a_real_payment(self):
        real_payment = make_payment(seed=9)
        object.__setattr__(real_payment, "source", "razorpay")
        scenario = builders.build_tds_short_payment(
            random.Random(9), "rec_8", "ignored", 200_000, Decimal("0.02"), FEE_RATE, TAX_RATE,
            payment=real_payment,
        )
        assert scenario.uses_real_payment is True
        assert scenario.payments[0] is real_payment


class TestBuildSplitPayment:
    def test_two_ledger_entries_sum_to_the_payment_amount(self):
        scenario = builders.build_split_payment(
            random.Random(10), "rec_9", "order_split_1", 30_000, 45_000, FEE_RATE, TAX_RATE,
        )
        total_expected = sum(l.expected_amount_paise for l in scenario.ledger_entries)
        assert total_expected == scenario.payments[0].amount_paise
        assert len(scenario.ledger_entries) == 2

    def test_resists_the_deterministic_matcher(self):
        scenario = builders.build_split_payment(
            random.Random(11), "rec_10", "order_split_2", 20_000, 25_000, FEE_RATE, TAX_RATE,
        )
        result = run_matcher(scenario)
        assert result.matches == []
        assert len(result.exceptions) > 0


class TestBuildRefundNetted:
    def test_bank_credit_is_net_of_the_refund(self):
        scenario = builders.build_refund_netted(
            random.Random(12), "rec_11", "order_refund_1", 100_000, 20_000, FEE_RATE, TAX_RATE,
        )
        payment = scenario.payments[0]
        expected_net = payment.amount_paise - payment.fee_paise - payment.tax_paise - 20_000
        assert scenario.bank_lines[0].amount_paise == expected_net

    def test_resists_the_deterministic_matcher(self):
        scenario = builders.build_refund_netted(
            random.Random(13), "rec_12", "order_refund_2", 100_000, 20_000, FEE_RATE, TAX_RATE,
        )
        result = run_matcher(scenario)
        assert result.matches == []
        assert len(result.exceptions) > 0

    def test_rejects_a_refund_larger_than_the_net_settlement(self):
        with pytest.raises(ValueError):
            builders.build_refund_netted(
                random.Random(1), "rec_x", "order_x", 10_000, 50_000, FEE_RATE, TAX_RATE,
            )


class TestBuildDuplicatePayment:
    def test_earlier_payment_matches_cleanly_later_is_rejected(self):
        scenario = builders.build_duplicate_payment(
            random.Random(14), "rec_13", "order_dup_1", 60_000, FEE_RATE, TAX_RATE,
        )
        assert len(scenario.payments) == 2
        result = run_matcher(scenario)

        assert len(result.matches) == 1
        earlier_id = min(scenario.payments, key=lambda p: p.captured_at).payment_id
        assert result.matches[0].payment_ids == (earlier_id,)

        rejections = [e for e in result.exceptions if e.reason_code == ReasonCode.DUPLICATE_PAYMENT_REJECTED]
        assert len(rejections) == 1


class TestBuildUnmatchable:
    def test_has_no_ledger_entry_or_bank_line(self):
        scenario = builders.build_unmatchable(
            random.Random(15), "rec_14", "order_orphan_1", 15_000, FEE_RATE, TAX_RATE,
        )
        assert scenario.bank_lines == []
        assert scenario.ledger_entries == []

    def test_resists_the_deterministic_matcher(self):
        scenario = builders.build_unmatchable(
            random.Random(16), "rec_15", "order_orphan_2", 15_000, FEE_RATE, TAX_RATE,
        )
        result = run_matcher(scenario)
        assert result.matches == []
        assert len(result.exceptions) == 1
        assert result.exceptions[0].reason_code == ReasonCode.UNMATCHABLE_NO_COUNTERPART
