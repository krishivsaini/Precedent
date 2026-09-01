from datetime import date, datetime

import pytest

from precedent.domain.matching import (
    BankLine,
    LedgerEntry,
    Match,
    Payment,
    UnmatchedException,
    is_exact_match,
    is_netted_group_match,
    is_tolerance_match,
    is_within_date_window,
    match_batch,
    net_settlement_amount_paise,
)
from precedent.domain.reasons import ReasonCode


def make_payment(payment_id, order_id, amount_paise, captured_at=None, fee_paise=0, tax_paise=0):
    return Payment(
        payment_id=payment_id,
        order_id=order_id,
        amount_paise=amount_paise,
        captured_at=captured_at or datetime(2026, 1, 1, 10, 0),
        fee_paise=fee_paise,
        tax_paise=tax_paise,
    )


def make_bank_line(line_id, amount_paise, value_date=None, direction="credit", narration=""):
    return BankLine(
        line_id=line_id,
        value_date=value_date or date(2026, 1, 1),
        amount_paise=amount_paise,
        direction=direction,
        narration=narration,
    )


def make_ledger_entry(entry_id, order_id, expected_amount_paise):
    return LedgerEntry(entry_id=entry_id, order_id=order_id, expected_amount_paise=expected_amount_paise)


class TestIsExactMatch:
    def test_matches_when_amounts_and_order_agree(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 10_000)
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_exact_match(payment, bank_line, ledger_entry) is True

    def test_fails_on_amount_mismatch(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 9_900)
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_exact_match(payment, bank_line, ledger_entry) is False

    def test_fails_on_order_id_mismatch(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 10_000)
        ledger_entry = make_ledger_entry("led_1", "order_2", 10_000)
        assert is_exact_match(payment, bank_line, ledger_entry) is False

    def test_fails_on_debit_direction(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 10_000, direction="debit")
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_exact_match(payment, bank_line, ledger_entry) is False

    def test_bank_line_is_compared_net_of_fee_and_tax_not_gross(self):
        # Regression: the bank never sees the gross payment amount — Razorpay deducts
        # fee+tax before crediting. A bank_line equal to the *gross* amount must NOT
        # match when the payment actually carried a fee; every other test in this class
        # uses fee_paise=0 by default, which made this bug invisible until non-zero fees
        # were exercised (see FAILURES.md, 2026-09-01).
        payment = make_payment("pay_1", "order_1", 10_000, fee_paise=236, tax_paise=36)
        bank_line_at_gross = make_bank_line("line_1", 10_000)  # wrong: should be net (9_728)
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_exact_match(payment, bank_line_at_gross, ledger_entry) is False

        bank_line_at_net = make_bank_line("line_2", 10_000 - 236 - 36)
        assert is_exact_match(payment, bank_line_at_net, ledger_entry) is True


class TestIsToleranceMatch:
    def test_matches_within_rounding_delta(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 10_099)
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_tolerance_match(payment, bank_line, ledger_entry) is True

    def test_fails_beyond_tolerance(self):
        payment = make_payment("pay_1", "order_1", 10_000)
        bank_line = make_bank_line("line_1", 10_500)
        ledger_entry = make_ledger_entry("led_1", "order_1", 10_000)
        assert is_tolerance_match(payment, bank_line, ledger_entry) is False


class TestIsWithinDateWindow:
    def test_same_day_is_within_window(self):
        payment = make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 23, 0))
        bank_line = make_bank_line("line_1", 10_000, value_date=date(2026, 1, 1))
        assert is_within_date_window(payment, bank_line, window_days=2) is True

    def test_beyond_window_fails(self):
        payment = make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 10, 0))
        bank_line = make_bank_line("line_1", 10_000, value_date=date(2026, 1, 10))
        assert is_within_date_window(payment, bank_line, window_days=2) is False

    def test_exactly_at_window_edge_is_inclusive(self):
        payment = make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 10, 0))
        bank_line = make_bank_line("line_1", 10_000, value_date=date(2026, 1, 3))
        assert is_within_date_window(payment, bank_line, window_days=2) is True


class TestNettedGroupMatch:
    def test_net_settlement_amount_subtracts_fees_and_tax(self):
        payments = [
            make_payment("pay_1", "order_1", 10_000, fee_paise=200, tax_paise=36),
            make_payment("pay_2", "order_2", 5_000, fee_paise=100, tax_paise=18),
        ]
        assert net_settlement_amount_paise(payments) == (10_000 - 200 - 36) + (5_000 - 100 - 18)

    def test_matches_credit_within_tolerance_of_net_amount(self):
        payments = [
            make_payment("pay_1", "order_1", 10_000, fee_paise=200, tax_paise=36),
            make_payment("pay_2", "order_2", 5_000, fee_paise=100, tax_paise=18),
        ]
        net = net_settlement_amount_paise(payments)
        bank_line = make_bank_line("line_1", net)
        assert is_netted_group_match(payments, bank_line) is True

    def test_fails_when_credit_is_far_from_net_amount(self):
        payments = [
            make_payment("pay_1", "order_1", 10_000, fee_paise=200, tax_paise=36),
        ]
        bank_line = make_bank_line("line_1", 5_000)
        assert is_netted_group_match(payments, bank_line) is False

    def test_fails_on_debit_direction(self):
        payments = [make_payment("pay_1", "order_1", 10_000)]
        bank_line = make_bank_line("line_1", 10_000, direction="debit")
        assert is_netted_group_match(payments, bank_line) is False


class TestMatchBatch:
    def test_single_exact_match(self):
        payments = [make_payment("pay_1", "order_1", 10_000)]
        bank_lines = [make_bank_line("line_1", 10_000)]
        ledger_entries = [make_ledger_entry("led_1", "order_1", 10_000)]

        result = match_batch(payments, bank_lines, ledger_entries)

        assert len(result.matches) == 1
        assert result.matches[0].reason_code == ReasonCode.EXACT_MATCH
        assert result.matches[0].payment_ids == ("pay_1",)
        assert result.exceptions == []

    def test_tolerance_match_when_amount_is_off_by_a_rounding_delta(self):
        payments = [make_payment("pay_1", "order_1", 10_000)]
        bank_lines = [make_bank_line("line_1", 10_050)]
        ledger_entries = [make_ledger_entry("led_1", "order_1", 10_000)]

        result = match_batch(payments, bank_lines, ledger_entries)

        assert len(result.matches) == 1
        assert result.matches[0].reason_code == ReasonCode.TOLERANCE_ROUNDING

    def test_date_window_match_when_settlement_lags(self):
        payments = [
            make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 10, 0))
        ]
        bank_lines = [make_bank_line("line_1", 10_000, value_date=date(2026, 1, 5))]
        ledger_entries = [make_ledger_entry("led_1", "order_1", 10_000)]

        result = match_batch(payments, bank_lines, ledger_entries)

        assert len(result.matches) == 1
        assert result.matches[0].reason_code == ReasonCode.DATE_WINDOW_TIMING

    def test_duplicate_customer_payment_is_not_double_matched(self):
        # FR-2.5: must not double-match a duplicate customer payment against the same
        # ledger entry. Two payments land on the same order; only the earlier one may
        # match the (single) ledger entry, and there is only one bank line to match
        # against, so a naive matcher could wrongly match both payments to it.
        earlier = make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 9, 0))
        later = make_payment("pay_2", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 9, 5))
        bank_lines = [make_bank_line("line_1", 10_000)]
        ledger_entries = [make_ledger_entry("led_1", "order_1", 10_000)]

        result = match_batch([earlier, later], bank_lines, ledger_entries)

        assert len(result.matches) == 1
        assert result.matches[0].payment_ids == ("pay_1",)

        duplicate_exceptions = [
            e for e in result.exceptions if e.reason_code == ReasonCode.DUPLICATE_PAYMENT_REJECTED
        ]
        assert len(duplicate_exceptions) == 1
        assert duplicate_exceptions[0].payment_ids == ("pay_2",)

    def test_payment_with_no_ledger_entry_is_unmatchable(self):
        payments = [make_payment("pay_1", "order_orphan", 10_000)]
        result = match_batch(payments, bank_lines=[], ledger_entries=[])

        assert result.matches == []
        assert len(result.exceptions) == 1
        assert result.exceptions[0].reason_code == ReasonCode.UNMATCHABLE_NO_COUNTERPART
        assert result.exceptions[0].payment_ids == ("pay_1",)

    def test_ledger_entry_with_no_payment_is_an_exception_not_a_silent_gap(self):
        # FR-2.6 / direct NEFT bypass (spec §5): a bank credit landed and a ledger entry
        # expects it, but no payment ever came in through the PSP for that order. This
        # must surface as an exception, not vanish because there's no payment to anchor on.
        bank_lines = [make_bank_line("line_1", 10_000)]
        ledger_entries = [make_ledger_entry("led_1", "order_1", 10_000)]

        result = match_batch([], bank_lines, ledger_entries)

        ledger_orphans = [
            e for e in result.exceptions
            if e.reason_code == ReasonCode.UNMATCHABLE_NO_COUNTERPART and e.ledger_entry_ids
        ]
        assert len(ledger_orphans) == 1
        assert ledger_orphans[0].ledger_entry_ids == ("led_1",)

    def test_unclaimed_bank_line_is_an_exception_not_a_silent_gap(self):
        # Symmetric case: a credit line exists but nothing (payment or ledger entry)
        # claims it.
        bank_lines = [make_bank_line("line_1", 10_000)]

        result = match_batch([], bank_lines, [])

        bank_line_orphans = [
            e for e in result.exceptions
            if e.reason_code == ReasonCode.UNMATCHABLE_NO_COUNTERPART and e.bank_line_ids
        ]
        assert len(bank_line_orphans) == 1
        assert bank_line_orphans[0].bank_line_ids == ("line_1",)

    def test_bank_line_already_consumed_is_not_reused(self):
        # Two orders happen to want the same amount; the single available bank line can
        # only satisfy one of them, and the other must escalate rather than double-spend
        # the bank line.
        payments = [
            make_payment("pay_1", "order_1", 10_000, captured_at=datetime(2026, 1, 1, 9, 0)),
            make_payment("pay_2", "order_2", 10_000, captured_at=datetime(2026, 1, 1, 9, 0)),
        ]
        bank_lines = [make_bank_line("line_1", 10_000)]
        ledger_entries = [
            make_ledger_entry("led_1", "order_1", 10_000),
            make_ledger_entry("led_2", "order_2", 10_000),
        ]

        result = match_batch(payments, bank_lines, ledger_entries)

        assert len(result.matches) == 1
        matched_order = result.matches[0].payment_ids
        unmatched = [e for e in result.exceptions if e.reason_code == ReasonCode.UNMATCHABLE_NO_COUNTERPART]
        assert len(unmatched) == 1
        assert matched_order[0] != unmatched[0].payment_ids[0]
