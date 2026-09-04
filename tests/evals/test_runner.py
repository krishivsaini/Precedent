from evals.dataset.loader import load_dataset
from evals.runner import run_baseline_rules_alone


class TestLoadDataset:
    def test_loads_all_240_committed_scenarios(self):
        assert len(load_dataset()) == 240

    def test_scenario_members_are_resolved_from_the_id_lists(self):
        scenarios = load_dataset()
        clean = next(s for s in scenarios if s.kind == "clean_match")
        assert len(clean.payments) == 1
        assert len(clean.bank_lines) == 1
        assert len(clean.ledger_entries) == 1

    def test_unmatchable_scenarios_have_no_bank_line_or_ledger_entry(self):
        scenarios = load_dataset()
        unmatchable = [s for s in scenarios if s.kind == "unmatchable"]
        assert unmatchable
        assert all(s.bank_lines == [] and s.ledger_entries == [] for s in unmatchable)

    def test_committed_dataset_matches_the_generator_output(self):
        # Guards against the committed JSON drifting out of sync with generate.py.
        from evals.dataset.generate import generate_dataset

        generated = generate_dataset()
        loaded = load_dataset()
        assert [s.scenario_id for s in generated] == [s.scenario_id for s in loaded]
        assert [s.kind for s in generated] == [s.kind for s in loaded]
        assert [s.pool_or_test for s in generated] == [s.pool_or_test for s in loaded]


class TestBaselineRulesAlone:
    def test_reports_every_record(self):
        result = run_baseline_rules_alone()
        assert result["dataset"]["total_records"] == 240

    def test_matcher_and_gold_labels_fully_agree(self):
        # The load-bearing check: if the deterministic matcher ever resolves something
        # gold calls an exception (or fails something gold calls clean), the dataset or
        # the matcher is wrong, and every downstream metric is built on sand.
        result = run_baseline_rules_alone()
        assert result["gold_matcher_agreement"]["mismatches"] == 0, (
            result["gold_matcher_agreement"]["mismatch_detail"]
        )

    def test_no_duplicate_payment_false_accepts(self):
        # spec §5: the duplicate-payment class exists specifically to test false accepts.
        result = run_baseline_rules_alone()
        assert result["metrics"]["false_accept_count"] == 0

    def test_resolves_the_rules_resolvable_classes_completely(self):
        result = run_baseline_rules_alone()
        breakdown = result["per_class_breakdown"]
        assert breakdown["clean_match"]["resolution_rate"] == 1.0
        assert breakdown["rounding_delta"]["resolution_rate"] == 1.0

    def test_resolves_none_of_the_agent_classes(self):
        result = run_baseline_rules_alone()
        breakdown = result["per_class_breakdown"]
        for kind in (
            "netted_settlement", "direct_neft_bypass", "tds_short_payment",
            "split_payment", "refund_netted", "duplicate_payment", "unmatchable",
        ):
            assert breakdown[kind]["resolution_rate"] == 0.0, kind

    def test_test_set_resolution_rate_is_zero_for_rules_alone(self):
        # Every held-out test scenario is an exception by construction, so a rules-only
        # baseline must score 0 on it — this is the floor the agent has to beat.
        result = run_baseline_rules_alone()
        assert result["metrics"]["autonomous_resolution_rate_on_test_set"] == 0.0

    def test_exception_list_is_not_empty_and_covers_every_unresolved_record(self):
        result = run_baseline_rules_alone()
        assert len(result["exception_list"]) == result["dataset"]["exceptions"]
        assert len(result["exception_list"]) == 194
