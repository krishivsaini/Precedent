"""The ablation harness, exercised end to end against a scripted model.

No network. The point is to prove the harness computes what it claims *before* a real run
spends tokens producing numbers nobody can check — a scoring bug found after the fact would
invalidate the committed result and the kill-criterion decision made from it.
"""

import json

import pytest

from evals.ablation import AblationAborted, DEFAULT_K, build_retriever, run_ablation
from precedent.adapters.llm.base import LLMResponse, LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.corpus.seed import seed_precedent_records

LIMIT = 6


class GoldOracleLLM:
    """A perfectly retrieval-dependent model: right only when the *relevant* precedent is
    in the prompt.

    `needs_relevant_precedent=True` is what separates the grounded arm from the random
    control. An earlier version of this double answered correctly whenever *any* precedent
    block was present, which made both arms score identically — and the harness correctly
    refused to call that a PASS. Depending on block presence rather than block relevance is
    exactly the confound the random control exists to catch, so the double has to model the
    distinction the harness is testing for.
    """

    model = "gold-oracle-test-double"

    def __init__(self, gold_by_summary: dict[str, str], needs_relevant_precedent: bool = True):
        self._gold_by_summary = gold_by_summary
        self._needs_relevant_precedent = needs_relevant_precedent
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        self.prompts.append(user)
        gold = next((g for summary, g in self._gold_by_summary.items() if summary in user), None)
        code = gold or "unmatchable_no_counterpart"
        if gold and self._needs_relevant_precedent and f"reason_code: {gold}" not in user:
            code = "exact_match" if gold != "exact_match" else "tolerance_rounding"
        body = {
            "reason_code": code, "confidence": 0.95,
            "rationale": "scripted", "cited_precedent_ids": [],
        }
        return LLMResponse(json.dumps(body), self.model, 12, 100, 20)


@pytest.fixture(scope="module")
def gold_by_scenario():
    from evals.dataset.loader import load_dataset

    pool = [s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"]
    return {s.scenario_id: s.expected_reason_code for s in pool[:LIMIT]}


class TestBuildRetriever:
    def test_none_yields_no_retriever(self):
        assert build_retriever("none", seed_precedent_records()) is None

    def test_rejects_an_unknown_name(self):
        with pytest.raises(ValueError, match="unknown retriever"):
            build_retriever("word2vec", seed_precedent_records())


class TestArmIsolation:
    def test_the_zero_shot_arm_is_shown_no_precedents(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        run_ablation(llm, limit=LIMIT, workers=2)
        zero_shot_prompts = [p for p in llm.prompts if "corpus is empty" in p]
        assert len(zero_shot_prompts) == LIMIT

    def test_the_grounded_and_control_arms_are_both_shown_k_precedents(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        run_ablation(llm, limit=LIMIT, workers=2)
        grounded_prompts = [p for p in llm.prompts if "corpus is empty" not in p]
        assert len(grounded_prompts) == 2 * LIMIT
        for prompt in grounded_prompts:
            assert prompt.count("confidence_when_deposited") == DEFAULT_K

    def test_all_three_arms_see_the_same_cases(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        seen = [
            [case["scenario_id"] for case in arm["per_case"]] for arm in result["arms"]
        ]
        assert seen[0] == seen[1] == seen[2]


class TestScoring:
    def test_a_model_that_is_always_right_scores_one_hundred_percent(self, gold_by_scenario):
        # Scenario ids are not in the prompt, so the double answers in call order rather
        # than by looking the case up.
        codes = list(gold_by_scenario.values())
        llm = ScriptedLLM([
            json.dumps({"reason_code": c, "confidence": 0.95, "rationale": "x",
                        "cited_precedent_ids": []})
            for c in codes * 3
        ])
        result = run_ablation(llm, limit=LIMIT, workers=1)
        assert result["arms"][0]["metrics"]["autonomous_resolution_rate"] == 1.0

    def test_a_wrong_answer_counts_as_a_false_resolution_with_a_rupee_cost(self):
        wrong = json.dumps({"reason_code": "exact_match", "confidence": 0.99,
                            "rationale": "x", "cited_precedent_ids": []})
        result = run_ablation(ScriptedLLM([wrong] * (LIMIT * 3)), limit=LIMIT, workers=1)
        metrics = result["arms"][0]["metrics"]
        assert metrics["false_resolution_count"] > 0
        # spec §6: the headline is rupees, not counts — "the number that gets someone fired".
        assert metrics["false_resolution_cost_inr"] > 0
        assert len(result["arms"][0]["false_resolutions"]) == metrics["false_resolution_count"]

    def test_an_unavailable_model_aborts_the_run_instead_of_reporting_zero(self):
        # The most important test in this file. A provider outage produces exactly the same
        # numbers as a system that cannot resolve anything, and the first real smoke run of
        # this ablation printed "FAIL — the retrieval thesis is not supported" purely
        # because two arms were rate-limited (FAILURES.md, 2026-09-01). Returning metrics
        # here would let an infrastructure fault masquerade as a scientific finding.
        llm = ScriptedLLM([LLMUnavailable("down")] * (LIMIT * 3))
        with pytest.raises(AblationAborted, match="measures the provider"):
            run_ablation(llm, limit=LIMIT, workers=1)

    def test_a_few_transient_failures_do_not_abort_the_run(self):
        # Tolerable noise must not trip the breaker, or no run ever completes.
        ok = json.dumps({"reason_code": "netted_settlement", "confidence": 0.95,
                         "rationale": "x", "cited_precedent_ids": []})
        responses = [LLMUnavailable("blip")] + [ok] * 200
        result = run_ablation(ScriptedLLM(responses), limit=20, workers=1)
        assert result["arms"][0]["metrics"]["escalation_rate"] < 0.15

    def test_an_escalated_case_is_never_counted_as_a_false_resolution(self):
        # Escalating is the honest outcome, not a wrong answer. Conflating the two would
        # make the false-resolution cost — the number the eval leads with — meaningless.
        low = json.dumps({"reason_code": "exact_match", "confidence": 0.1,
                          "rationale": "unsure", "cited_precedent_ids": []})
        result = run_ablation(ScriptedLLM([low] * (LIMIT * 3)), limit=LIMIT, workers=1)
        metrics = result["arms"][0]["metrics"]
        assert metrics["escalation_rate"] == 1.0
        assert metrics["false_resolution_count"] == 0
        assert metrics["false_resolution_cost_inr"] == 0

    def test_rates_across_the_three_outcomes_account_for_every_case(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        for arm in result["arms"]:
            m = arm["metrics"]
            resolved = m["autonomous_resolution_rate"] * LIMIT
            wrong = m["false_resolution_count"]
            escalated = m["escalation_rate"] * LIMIT
            assert round(resolved + wrong + escalated) == LIMIT


class TestCitationAccounting:
    def test_a_citation_of_an_unretrieved_precedent_counts_as_hallucinated(self):
        body = json.dumps({"reason_code": "exact_match", "confidence": 0.95,
                           "rationale": "x", "cited_precedent_ids": ["prec_seed_9999"]})
        result = run_ablation(ScriptedLLM([body] * (LIMIT * 3)), limit=LIMIT, workers=1)
        assert result["arms"][0]["metrics"]["hallucinated_citations"] == LIMIT

    def test_precedent_precision_is_none_when_nothing_was_cited(self):
        body = json.dumps({"reason_code": "exact_match", "confidence": 0.95,
                           "rationale": "x", "cited_precedent_ids": []})
        result = run_ablation(ScriptedLLM([body] * (LIMIT * 3)), limit=LIMIT, workers=1)
        # Reported as absent rather than as 0.0 — no citations is not zero precision.
        assert result["arms"][0]["metrics"]["precedent_precision"] is None


class TestKillCriterion:
    def test_passes_when_only_the_grounded_arm_answers_correctly(self, gold_by_scenario):
        markers = {}
        from evals.dataset.loader import load_dataset

        pool = [s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"][:LIMIT]
        for scenario in pool:
            from evals.retrieval_eval import scenario_to_case

            markers[scenario_to_case(scenario).summarize()] = scenario.expected_reason_code

        llm = GoldOracleLLM(markers, needs_relevant_precedent=True)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        kill = result["kill_criterion"]
        assert kill["grounded_beats_zero_shot"]
        assert kill["verdict"].startswith("PASS")

    def test_fails_when_grounding_makes_no_difference(self):
        body = json.dumps({"reason_code": "exact_match", "confidence": 0.95,
                           "rationale": "x", "cited_precedent_ids": []})
        result = run_ablation(ScriptedLLM([body] * (LIMIT * 3)), limit=LIMIT, workers=1)
        kill = result["kill_criterion"]
        assert not kill["grounded_beats_zero_shot"]
        assert kill["verdict"].startswith("FAIL")

    def test_the_verdict_requires_beating_the_random_control_too(self):
        # A grounded win over zero-shot alone is unattributable: the grounded prompt is also
        # simply longer. The control is what makes the claim about *relevance*.
        body = json.dumps({"reason_code": "exact_match", "confidence": 0.95,
                           "rationale": "x", "cited_precedent_ids": []})
        result = run_ablation(ScriptedLLM([body] * (LIMIT * 3)), limit=LIMIT, workers=1)
        assert "grounded_beats_random_control" in result["kill_criterion"]


class TestResultShape:
    def test_records_the_model_and_settings_behind_the_numbers(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        assert result["model"] == "gold-oracle-test-double"
        assert result["settings"]["temperature"] == 0.0
        assert result["settings"]["k"] == DEFAULT_K

    def test_states_that_the_test_split_was_untouched(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        assert "untouched" in result["dataset"]["split"]

    def test_is_json_serialisable(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        json.dumps(run_ablation(llm, limit=LIMIT, workers=2))


class TestSignificance:
    """Paired significance on the arms. Exists because the headline numbers invite
    over-claiming: a 6.5-point gap on 62 cases is four cases, and reporting that as a
    demonstrated improvement without asking whether four cases could be noise is the same
    category of error as the rate limit that masqueraded as a kill-criterion failure."""

    def test_no_disagreement_is_not_evidence_of_a_difference(self):
        from evals.ablation import mcnemar_exact

        same = {"a": True, "b": False}
        result = mcnemar_exact(same, dict(same))
        assert result["discordant_pairs"] == 0
        assert result["p_value"] == 1.0
        assert not result["significant_at_05"]

    def test_concordant_cases_carry_no_information(self):
        # Both arms right, or both wrong, say nothing about which is better — only the
        # discordant pairs do. Pairing is the whole point of using McNemar here.
        from evals.ablation import mcnemar_exact

        a = {f"c{i}": True for i in range(50)} | {"x": True}
        b = {f"c{i}": True for i in range(50)} | {"x": False}
        assert mcnemar_exact(a, b)["discordant_pairs"] == 1

    def test_four_wins_and_no_losses_does_not_reach_significance(self):
        # The actual grounded-vs-random-control result. Directionally consistent, never
        # losing, and still p=0.125 — a direction, not a demonstrated effect.
        from evals.ablation import mcnemar_exact

        a = {f"w{i}": True for i in range(4)} | {f"c{i}": True for i in range(58)}
        b = {f"w{i}": False for i in range(4)} | {f"c{i}": True for i in range(58)}
        result = mcnemar_exact(a, b)
        assert result["a_wins"] == 4 and result["b_wins"] == 0
        assert result["p_value"] == 0.125
        assert not result["significant_at_05"]

    def test_nine_wins_and_no_losses_does_reach_significance(self):
        # The actual grounded-vs-zero-shot result.
        from evals.ablation import mcnemar_exact

        a = {f"w{i}": True for i in range(9)}
        b = {f"w{i}": False for i in range(9)}
        result = mcnemar_exact(a, b)
        assert result["p_value"] < 0.05
        assert result["significant_at_05"]

    def test_the_test_is_symmetric(self):
        from evals.ablation import mcnemar_exact

        a = {f"w{i}": True for i in range(6)} | {f"l{i}": False for i in range(2)}
        b = {f"w{i}": False for i in range(6)} | {f"l{i}": True for i in range(2)}
        assert mcnemar_exact(a, b)["p_value"] == mcnemar_exact(b, a)["p_value"]

    def test_the_run_reports_significance_and_the_effect_split(self):
        llm = GoldOracleLLM({}, needs_relevant_precedent=False)
        result = run_ablation(llm, limit=LIMIT, workers=2)
        assert set(result["significance"]) == {
            "grounded_vs_zero_shot", "grounded_vs_random_control",
            "random_control_vs_zero_shot",
        }
        assert "having_precedents_at_all_pp" in result["effect_decomposition"]
        assert "relevance_of_precedents_pp" in result["effect_decomposition"]
        assert "caveat" in result["kill_criterion"]
