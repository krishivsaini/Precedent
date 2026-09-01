from collections import Counter

from evals.dataset.convert import to_domain_bank_line, to_domain_ledger_entry, to_domain_payment
from evals.dataset.generate import CLASS_COUNTS, TEST_SET_SIZE, generate_dataset
from precedent.domain.matching import match_batch


def run_matcher(scenario):
    payments = [to_domain_payment(p) for p in scenario.payments]
    bank_lines = [to_domain_bank_line(b) for b in scenario.bank_lines]
    ledger_entries = [to_domain_ledger_entry(l) for l in scenario.ledger_entries]
    return match_batch(payments, bank_lines, ledger_entries)


class TestGenerateDataset:
    def test_produces_exactly_240_records(self):
        assert len(generate_dataset()) == 240

    def test_class_counts_match_the_configured_distribution(self):
        scenarios = generate_dataset()
        counts = Counter(s.kind for s in scenarios)
        assert dict(counts) == CLASS_COUNTS

    def test_is_deterministic_across_runs(self):
        run_a = generate_dataset()
        run_b = generate_dataset()
        assert [s.scenario_id for s in run_a] == [s.scenario_id for s in run_b]
        assert [tuple(p.payment_id for p in s.payments) for s in run_a] == [
            tuple(p.payment_id for p in s.payments) for s in run_b
        ]
        assert [s.pool_or_test for s in run_a] == [s.pool_or_test for s in run_b]

    def test_order_ids_are_globally_unique(self):
        scenarios = generate_dataset()
        order_ids = []
        for s in scenarios:
            order_ids.extend(p.order_id for p in s.payments)
            order_ids.extend(l.order_id for l in s.ledger_entries)
        # split_payment scenarios legitimately reuse one order_id across two ledger
        # entries within the *same* scenario, so dedupe within-scenario before checking.
        per_scenario_order_ids = [
            {p.order_id for p in s.payments} | {l.order_id for l in s.ledger_entries}
            for s in scenarios
        ]
        flattened = [oid for group in per_scenario_order_ids for oid in group]
        assert len(flattened) == len(set(flattened))

    def test_real_payments_are_each_used_at_most_once(self):
        scenarios = generate_dataset()
        real_payment_ids = [
            p.payment_id for s in scenarios for p in s.payments if p.source == "razorpay"
        ]
        assert len(real_payment_ids) == len(set(real_payment_ids))
        assert len(real_payment_ids) == 21

    def test_exception_scenarios_are_split_between_pool_and_test(self):
        scenarios = generate_dataset()
        exceptions = [s for s in scenarios if s.is_exception]
        non_exceptions = [s for s in scenarios if not s.is_exception]

        assert all(s.pool_or_test in ("pool", "test") for s in exceptions)
        assert all(s.pool_or_test is None for s in non_exceptions)
        assert sum(1 for s in exceptions if s.pool_or_test == "test") == TEST_SET_SIZE

    def test_every_exception_class_contributes_to_the_test_set(self):
        # A stratified split should never leave a whole class entirely out of test.
        scenarios = generate_dataset()
        exception_kinds = {s.kind for s in scenarios if s.is_exception}
        test_kinds = {s.kind for s in scenarios if s.pool_or_test == "test"}
        assert test_kinds == exception_kinds

    def test_every_non_exception_scenario_resolves_via_match_batch(self):
        scenarios = generate_dataset()
        for s in scenarios:
            if s.is_exception:
                continue
            result = run_matcher(s)
            assert len(result.matches) == 1, f"{s.scenario_id} ({s.kind}) did not resolve"
            assert result.exceptions == []

    def test_every_exception_scenario_resists_match_batch(self):
        scenarios = generate_dataset()
        for s in scenarios:
            if not s.is_exception:
                continue
            result = run_matcher(s)
            if s.kind == "duplicate_payment":
                # exactly the earlier payment resolves; the duplicate is rejected
                assert len(result.matches) == 1, s.scenario_id
            else:
                assert result.matches == [], f"{s.scenario_id} ({s.kind}) matched but shouldn't have"
            assert len(result.exceptions) > 0, f"{s.scenario_id} produced no exception record"
