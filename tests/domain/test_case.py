import pytest

from precedent.adapters.storage.records import BankLineRecord, LedgerEntryRecord, PaymentRecord
from precedent.domain.case import ReconciliationCase, format_paise

#: Words that name an exception class or its answer. The case summary is the retrieval query;
#: if any of these leaked into it, retrieval would be matching on the label rather than on the
#: facts, and the Ring 1.3 ablation would measure leakage.
LABEL_WORDS = [
    "tds", "withholding", "netted", "netting", "duplicate", "split", "neft", "rtgs", "imps",
    "unmatchable", "bypass", "refund", "rounding", "tolerance", "exception", "escalate",
]


def payment(order_id="order_1", amount=100_000, fee=2_000, tax=360, at="2026-01-01T10:00:00+00:00"):
    return PaymentRecord(
        payment_id=f"pay_{order_id}", order_id=order_id, amount_paise=amount,
        fee_paise=fee, tax_paise=tax, captured_at=at, status="captured", source="synthetic",
    )


def credit(amount=97_640, date="2026-01-02", narration="NEFT-CR order_1"):
    return BankLineRecord(
        line_id="line_1", value_date=date, amount_paise=amount, direction="credit",
        narration=narration, source="synthetic",
    )


def ledger(order_id="order_1", expects=100_000):
    return LedgerEntryRecord(
        entry_id=f"led_{order_id}", order_id=order_id, expected_amount_paise=expects,
        invoice_no="INV-1234", customer_name="Acme Traders", terms="net_15",
    )


class TestFormatPaise:
    @pytest.mark.parametrize("amount,expected", [
        (0, "INR 0.00"), (1, "INR 0.01"), (100, "INR 1.00"),
        (123_456_789, "INR 1,234,567.89"), (-5_000, "-INR 50.00"),
    ])
    def test_renders_paise_as_rupees(self, amount, expected):
        assert format_paise(amount) == expected


class TestArithmetic:
    def test_net_settlement_deducts_fee_and_tax_per_payment(self):
        case = ReconciliationCase("c1", payments=[payment(), payment("order_2")])
        assert case.net_settlement_paise() == 2 * (100_000 - 2_000 - 360)

    def test_expected_sums_the_ledger_entries(self):
        case = ReconciliationCase("c1", ledger_entries=[ledger(expects=60_000), ledger("o2", 40_000)])
        assert case.expected_paise() == 100_000

    def test_credited_ignores_debit_lines(self):
        debit = BankLineRecord(
            line_id="line_2", value_date="2026-01-03", amount_paise=5_000,
            direction="debit", narration="reversal", source="synthetic",
        )
        case = ReconciliationCase("c1", bank_lines=[credit(amount=97_640), debit])
        assert case.credited_paise() == 97_640


class TestSummarize:
    def test_states_the_facts_a_reader_needs(self):
        case = ReconciliationCase("c1", [payment()], [credit()], [ledger()])
        text = case.summarize()
        assert "order_1" in text
        assert "INR 1,000.00" in text  # the gross payment
        assert "INR 976.40" in text  # what it settles to
        assert "INV-1234" in text
        assert "Acme Traders" in text

    def test_names_what_is_absent_explicitly(self):
        # Absence is often the strongest signal in reconciliation, so it has to be stated
        # rather than left as a missing line the retriever cannot see.
        no_payment = ReconciliationCase("c1", [], [credit()], [ledger()])
        assert "No captured payment record exists" in no_payment.summarize()

        no_ledger = ReconciliationCase("c2", [payment()], [credit()], [])
        assert "No open ledger entry exists" in no_ledger.summarize()

        no_bank = ReconciliationCase("c3", [payment()], [], [ledger()])
        assert "No bank statement line is present" in no_bank.summarize()

    def test_reports_the_gap_as_a_proportion_when_one_exists(self):
        # Proportionality is the discriminating fact between a deduction and rounding, so
        # the summary computes it rather than leaving the model to.
        case = ReconciliationCase("c1", [payment()], [credit(amount=90_000)], [ledger()])
        assert "10.00% of the expected amount" in case.summarize()

    def test_omits_the_gap_line_when_amounts_agree(self):
        case = ReconciliationCase("c1", [payment()], [credit(amount=100_000)], [ledger()])
        assert "exceeds bank credits" not in case.summarize()

    def test_never_names_the_exception_class_or_its_answer(self):
        # The load-bearing test of this module. A summary that says "TDS" hands the
        # retriever the label, and every downstream retrieval number becomes worthless.
        cases = [
            ReconciliationCase("c1", [payment()], [credit()], [ledger()]),
            ReconciliationCase("c2", [], [credit(narration="NEFT/ACMETR482/XFER")], [ledger()]),
            ReconciliationCase("c3", [payment(), payment()], [credit()], [ledger()]),
            ReconciliationCase("c4", [payment()], [], []),
        ]
        for case in cases:
            # Narrations are quoted verbatim from bank data, so exclude them from the scan:
            # a real statement may legitimately contain "NEFT", and redacting it would make
            # the summary false.
            text = case.summarize()
            for line in case.bank_lines:
                text = text.replace(line.narration, "")
            lowered = text.lower()
            for word in LABEL_WORDS:
                assert word not in lowered, f"{word!r} leaked into case {case.case_id}"

    def test_observations_appear_in_the_summary(self):
        case = ReconciliationCase("c1", [payment(), payment("order_2")], [credit()], [ledger()])
        text = case.summarize()
        assert "Observations:" in text
        for note in case.observations():
            assert note in text

    def test_is_deterministic_regardless_of_input_ordering(self):
        a = ReconciliationCase("c1", [payment("o1"), payment("o2")], [], [ledger("o1"), ledger("o2")])
        b = ReconciliationCase("c1", [payment("o2"), payment("o1")], [], [ledger("o2"), ledger("o1")])
        assert a.summarize() == b.summarize()


class TestObservations:
    """The two-level deduction split. This arithmetic was wrong once — the first version
    measured ledger-to-bank in a single step, which blends the customer's withholding with
    the processor's fee and makes a round-percentage deduction look arbitrary. That is the
    exact error the `tds_and_psp_fee_stacked` seed precedent warns about."""

    def notes(self, case):
        return " ".join(case.observations())

    def test_withholding_is_measured_against_the_payment_not_the_bank_credit(self):
        # Invoice 100000; customer withholds 10% and pays 90000; processor takes 2160 more.
        paid = payment(amount=90_000, fee=1_800, tax=324)
        case = ReconciliationCase(
            "c1", [paid], [credit(amount=90_000 - 1_800 - 324)], [ledger(expects=100_000)]
        )
        assert "ten percent" in self.notes(case)

    def test_the_processor_leg_is_reported_as_fully_explained_in_that_case(self):
        paid = payment(amount=90_000, fee=1_800, tax=324)
        case = ReconciliationCase(
            "c1", [paid], [credit(amount=90_000 - 1_800 - 324)], [ledger(expects=100_000)]
        )
        assert "nothing else was taken out" in self.notes(case)

    def test_a_two_percent_deduction_is_named_in_words(self):
        paid = payment(amount=98_000, fee=1_960, tax=353)
        case = ReconciliationCase(
            "c1", [paid], [credit(amount=98_000 - 1_960 - 353)], [ledger(expects=100_000)]
        )
        assert "two percent" in self.notes(case)

    def test_a_sub_rupee_shortfall_is_distinguished_from_a_proportional_one(self):
        paid = payment(amount=99_950, fee=0, tax=0)
        case = ReconciliationCase("c1", [paid], [credit(amount=99_950)], [ledger(expects=100_000)])
        assert "under one rupee" in self.notes(case)

    def test_a_shortfall_after_fees_is_flagged_as_unexplained_by_any_fee_rate(self):
        # The refund signature: the credit is short of what the payment settles to even
        # after that payment's own fee and tax.
        paid = payment(amount=100_000, fee=2_000, tax=360)
        case = ReconciliationCase(
            "c1", [paid], [credit(amount=100_000 - 2_000 - 360 - 5_000)], [ledger()]
        )
        assert "no fee rate accounts for it" in self.notes(case)

    def test_reports_payment_in_full_when_the_customer_deducted_nothing(self):
        case = ReconciliationCase("c1", [payment()], [credit()], [ledger()])
        assert "paid the invoiced amount in full" in self.notes(case)

    def test_names_payments_sharing_one_order_reference(self):
        case = ReconciliationCase("c1", [payment(), payment()], [credit()], [ledger()])
        assert "share the same order reference" in self.notes(case)

    def test_does_not_claim_a_shortfall_when_payments_share_an_order(self):
        # Two full payments on one order make gross paid exceed the invoice, which would
        # otherwise emit a nonsensical negative-withholding or phantom-refund observation.
        case = ReconciliationCase("c1", [payment(), payment()], [credit()], [ledger()])
        notes = self.notes(case)
        assert "no fee rate accounts for it" not in notes
        assert "paid less than the invoiced amount" not in notes

    def test_names_many_payments_across_distinct_orders_against_one_credit(self):
        payments = [payment("o1"), payment("o2"), payment("o3")]
        settled = sum(p.amount_paise - p.fee_paise - p.tax_paise for p in payments)
        case = ReconciliationCase(
            "c1", payments, [credit(amount=settled)],
            [ledger("o1"), ledger("o2"), ledger("o3")],
        )
        notes = self.notes(case)
        assert "three payments across three distinct order references" in notes
        assert "single bank credit" in notes

    def test_names_a_credit_with_no_payment_behind_it_as_gross(self):
        case = ReconciliationCase("c1", [], [credit(amount=100_000)], [ledger()])
        notes = self.notes(case)
        assert "No captured payment record exists" in notes
        assert "gross" in notes

    def test_names_multiple_entries_under_one_order(self):
        case = ReconciliationCase(
            "c1", [payment()], [credit()], [ledger(expects=60_000), ledger(expects=40_000)]
        )
        assert "share one order reference" in self.notes(case)

    def test_an_orphan_payment_states_both_absences(self):
        case = ReconciliationCase("c1", [payment()], [], [])
        notes = self.notes(case)
        assert "No bank statement line is present" in notes
        assert "No open ledger entry exists" in notes

    def test_retrieval_query_is_the_observations_and_nothing_else(self):
        case = ReconciliationCase("c1", [payment()], [credit()], [ledger()])
        assert case.retrieval_query() == "\n".join(case.observations())
        # The per-record boilerplate that buried the signal must not be in the query.
        assert "INV-1234" not in case.retrieval_query()
