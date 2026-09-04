"""The replay harness. No network — a scripted model stands in throughout.

The point is to prove the curve is computed honestly *before* spending ~1,900 model calls
producing one. A scoring bug found afterwards would invalidate the headline artifact and the
conclusion drawn from it.
"""

import json

import pytest

from evals.dataset.loader import load_dataset
from evals.replay import (
    SNAPSHOTS,
    build_deposit_sequence,
    corpus_at,
    deposit_order,
    replay_test_set,
    run_replay,
)
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.corpus.seed import seed_precedent_records


def authored(customer="Konark Logistics"):
    return json.dumps({
        "situation": (
            f"Payments from {customer} arrive short of the invoiced amount by a proportion "
            "matching no statutory withholding band, which for this customer is their "
            "negotiated rebate."
        ),
        "resolution": (
            f"{customer} settles under a negotiated rebate agreed in their supply contract; "
            "reconstruct the invoice from the receipt and close it in full."
        ),
        "reason_code": "negotiated_rebate",
        "entities": [customer],
        "amount_signature": "negotiated_rebate",
        "confidence_at_deposit": 0.93,
    })


class StatelessLLM:
    """Answers from the prompt's content, never from a queue position.

    `ScriptedLLM` pops responses in order, so under concurrency one case can receive
    another's answer — which makes it useless for testing that worker count does not change
    the result. This double is order-independent by construction, so any difference between
    one worker and four is the harness's doing.
    """

    model = "stateless-test-double"

    def __init__(self, reason_code="netted_settlement", confidence=0.95):
        self._code = reason_code
        self._confidence = confidence

    def complete(self, system, user, *, temperature=0.0):
        from precedent.adapters.llm.base import LLMResponse

        # Three prompts reach this double, told apart by their own instructions rather than
        # by call order: investigate asks for a tool or `done`, deposit asks for a
        # precedent, resolve asks for a reason code.
        if "The resolution that was confirmed" in user:
            body = json.dumps({
                "situation": (
                    "Payments from this counterparty arrive short of the invoiced amount by "
                    "a proportion matching no statutory band, which for them is a standing "
                    "negotiated arrangement."
                ),
                "resolution": (
                    "Reconstruct the invoice from the receipt, confirm it reproduces the "
                    "invoiced figure, and close the invoice in full."
                ),
                "reason_code": self._code,
                "entities": ["counterparty"],
                "amount_signature": "standing_arrangement",
                "confidence_at_deposit": 0.9,
            })
        elif "declare the investigation finished" in user or "tool" in system[:400].lower():
            body = json.dumps({"done": True, "why": "nothing further"})
        else:
            body = json.dumps({
                "reason_code": self._code, "confidence": self._confidence,
                "rationale": "x", "cited_precedent_ids": [],
            })
        return LLMResponse(body, self.model, 1, prompt_tokens=1, completion_tokens=1)


@pytest.fixture(scope="module")
def scenarios():
    return load_dataset()


class TestSnapshotScale:
    def test_every_snapshot_is_reachable(self, scenarios):
        # The spec's 0/50/100/150/200 is arithmetically impossible here: the corpus tops out
        # at 42 seeds plus 98 depositable pool exceptions. A curve with unreachable points
        # would simply stop early and look like a plateau.
        pool = [s for s in scenarios if s.is_exception and s.pool_or_test == "pool"]
        assert max(SNAPSHOTS) <= len(pool)

    def test_it_starts_at_an_empty_deposit_corpus(self):
        # The first point must be the seeds alone, or there is no baseline to grow from.
        assert SNAPSHOTS[0] == 0


class TestDepositOrder:
    def test_only_pool_exceptions_are_ever_deposited(self, scenarios):
        # The load-bearing rule of the whole eval: depositing a test-set resolution would
        # let the system answer from its memory of that exact case.
        assert all(s.pool_or_test == "pool" for s in deposit_order(scenarios))

    def test_the_order_is_fixed_across_runs(self, scenarios):
        first = [s.scenario_id for s in deposit_order(scenarios)]
        second = [s.scenario_id for s in deposit_order(scenarios)]
        assert first == second

    def test_the_order_is_shuffled_rather_than_grouped_by_class(self, scenarios):
        # Cases arrive in arbitrary order in reality. Depositing class by class would make
        # each snapshot a different experiment rather than a point on one curve.
        kinds = [s.kind for s in deposit_order(scenarios)[:30]]
        assert len(set(kinds)) > 3


class TestCorpusGrowth:
    def test_zero_deposits_is_the_seed_corpus(self):
        assert len(corpus_at([], 0)) == len(seed_precedent_records())

    def test_the_corpus_grows_by_one_per_successful_deposit(self, scenarios):
        llm = ScriptedLLM([authored()] * 6)
        sequence = build_deposit_sequence(llm, scenarios, limit=5)
        assert len(corpus_at(sequence, 5)) == len(seed_precedent_records()) + 5
        assert len(corpus_at(sequence, 2)) == len(seed_precedent_records()) + 2

    def test_a_failed_authoring_still_advances_the_version(self, scenarios):
        # A deposit that could not be authored is a gap in the corpus. Skipping it would
        # overstate how fast the corpus grows and shift every later snapshot.
        llm = ScriptedLLM([authored(), "not json", authored()])
        sequence = build_deposit_sequence(llm, scenarios, limit=3)
        assert len(sequence) == 3
        assert sum(1 for e in sequence if e["ok"]) == 2
        assert len(corpus_at(sequence, 3)) == len(seed_precedent_records()) + 2

    def test_snapshots_are_prefixes_of_one_sequence(self, scenarios):
        # Version n must mean "after exactly n deposits" — otherwise a replay is
        # approximately repeatable rather than reproducible.
        llm = ScriptedLLM([authored()] * 6)
        sequence = build_deposit_sequence(llm, scenarios, limit=5)
        small = {r.precedent_id for r in corpus_at(sequence, 2)}
        large = {r.precedent_id for r in corpus_at(sequence, 4)}
        assert small < large


class TestReplayScoring:
    def responses(self, code, n=400):
        body = json.dumps({"reason_code": code, "confidence": 0.95,
                           "rationale": "x", "cited_precedent_ids": []})
        return [json.dumps({"done": True}), body] * n

    def test_it_scores_only_the_held_out_test_set(self, scenarios):
        llm = ScriptedLLM(self.responses("netted_settlement"))
        result = replay_test_set(llm, scenarios, seed_precedent_records(), k=5, control=False)
        expected = len([s for s in scenarios if s.is_exception and s.pool_or_test == "test"])
        assert len(result["per_case"]) == expected

    def test_a_perfect_model_scores_one_hundred_percent(self, scenarios):
        test_set = [s for s in scenarios if s.is_exception and s.pool_or_test == "test"]
        queued = []
        for scenario in test_set:
            queued.append(json.dumps({"done": True}))
            queued.append(json.dumps({
                "reason_code": scenario.expected_reason_code, "confidence": 0.95,
                "rationale": "x", "cited_precedent_ids": [],
            }))
        # workers=1 because ScriptedLLM answers in queue order, which can only be matched
        # to cases positionally when the cases run in order. That is a property of this test
        # double, not of the harness — the concurrent path is covered by the tests either
        # side of this one.
        llm = ScriptedLLM(queued)
        result = replay_test_set(
            llm, scenarios, seed_precedent_records(), k=5, control=False, workers=1
        )
        assert result["resolution_rate"] == 1.0

    def test_the_counterparty_subset_is_reported_separately(self, scenarios):
        # Seven of nine classes already sit at the ceiling with no corpus at all, so a flat
        # headline could conceal a real effect in the only subset with headroom.
        llm = ScriptedLLM(self.responses("negotiated_rebate"))
        result = replay_test_set(llm, scenarios, seed_precedent_records(), k=5, control=False)
        assert result["counterparty_cases"] > 0
        assert result["counterparty_resolution_rate"] is not None

    def test_escalations_are_not_counted_as_correct(self, scenarios):
        low = json.dumps({"reason_code": "exact_match", "confidence": 0.1,
                          "rationale": "unsure", "cited_precedent_ids": []})
        llm = ScriptedLLM([json.dumps({"done": True}), low] * 400)
        result = replay_test_set(llm, scenarios, seed_precedent_records(), k=5, control=False)
        assert result["escalation_rate"] == 1.0
        assert result["resolution_rate"] == 0.0

    def test_the_control_uses_the_same_corpus_and_k(self, scenarios):
        # The control must differ in exactly one respect — which precedents are chosen.
        llm = ScriptedLLM(self.responses("netted_settlement"))
        control = replay_test_set(llm, scenarios, seed_precedent_records(), k=5, control=True)
        assert len(control["per_case"]) == len(
            [s for s in scenarios if s.is_exception and s.pool_or_test == "test"]
        )


class TestConcurrencyDoesNotChangeTheResult:
    def test_the_same_cases_are_scored_either_way(self, scenarios):
        # Order is restored from the input list, so worker count must not affect which
        # cases appear or in what order they are reported.
        one = replay_test_set(StatelessLLM(), scenarios,
                              seed_precedent_records(), 5, False, workers=1)
        many = replay_test_set(StatelessLLM(), scenarios,
                               seed_precedent_records(), 5, False, workers=6)
        assert [c["scenario_id"] for c in one["per_case"]] == [
            c["scenario_id"] for c in many["per_case"]
        ]
        assert one["resolution_rate"] == many["resolution_rate"]


class TestCurveShape:
    def test_the_control_runs_at_every_snapshot(self, scenarios):
        # Automatic, because a control that has to be remembered is one that gets skipped
        # under time pressure (spec §6: "run this unprompted").
        llm = ScriptedLLM([authored()] * 4 + [json.dumps({"done": True}),
                          json.dumps({"reason_code": "netted_settlement", "confidence": 0.9,
                                      "rationale": "x", "cited_precedent_ids": []})] * 800)
        result = run_replay(llm, k=5, snapshots=(0, 2), deposit_limit=2)
        assert len(result["curve"]) == 2
        for point in result["curve"]:
            assert "random_control" in point
            assert point["random_control"]["per_case"]

    def test_it_records_that_the_test_set_was_never_deposited(self, scenarios):
        llm = ScriptedLLM([authored()] * 2 + [json.dumps({"done": True}),
                          json.dumps({"reason_code": "netted_settlement", "confidence": 0.9,
                                      "rationale": "x", "cited_precedent_ids": []})] * 400)
        result = run_replay(llm, k=5, snapshots=(0,), deposit_limit=1)
        assert result["test_set"]["never_deposited"] is True

    def test_it_states_that_the_default_curve_is_an_upper_bound(self, scenarios):
        # Without a reviewer every resolution is confirmed at the correct answer, so the
        # corpus grows one precedent per case — the best case the mechanism can have.
        llm = ScriptedLLM([authored()] * 2 + [json.dumps({"done": True}),
                          json.dumps({"reason_code": "netted_settlement", "confidence": 0.9,
                                      "rationale": "x", "cited_precedent_ids": []})] * 400)
        result = run_replay(llm, k=5, snapshots=(0,), deposit_limit=1)
        assert any("optimistic bound" in c for c in result["caveats"])
        assert result["deposit_sequence"]["reviewer"]["simulated"] is False


class TestSimulatedReviewer:
    """The reviewer turns the curve from a ceiling into an estimate, and exercises the
    `corrected` path — which spec §7 calls the higher-value deposit and which the
    all-confirmed sequence never produced at all."""

    def test_a_rejection_deposits_nothing(self, scenarios):
        from evals.human import SimulatedReviewer

        always = SimulatedReviewer(rejection_rate=1.0)
        sequence = build_deposit_sequence(
            StatelessLLM(), scenarios, limit=6, workers=2,
            reviewer=always, retriever_for_pool=None,
        )
        assert all(not e["ok"] for e in sequence)
        assert all(e["human_action"] == "rejected" for e in sequence)
        assert len(corpus_at(sequence, 6)) == len(seed_precedent_records())

    def test_the_corpus_grows_more_slowly_with_a_reviewer(self, scenarios):
        from evals.human import SimulatedReviewer

        optimistic = build_deposit_sequence(
            StatelessLLM(), scenarios, limit=20, workers=4
        )
        reviewed = build_deposit_sequence(
            StatelessLLM(), scenarios, limit=20, workers=4,
            reviewer=SimulatedReviewer(rejection_rate=0.5), retriever_for_pool=None,
        )
        assert sum(1 for e in reviewed if e["ok"]) < sum(1 for e in optimistic if e["ok"])

    def test_a_wrong_proposal_becomes_a_correction_not_a_rejection(self, scenarios):
        # The agent here always answers `netted_settlement`, so any case of another class
        # is wrong and must be corrected — corrections are the higher-value deposit.
        from evals.human import SimulatedReviewer

        sequence = build_deposit_sequence(
            StatelessLLM(reason_code="netted_settlement"), scenarios, limit=20, workers=4,
            reviewer=SimulatedReviewer(rejection_rate=0.0), retriever_for_pool=None,
        )
        actions = {e["human_action"] for e in sequence}
        assert "corrected" in actions

    def test_the_reviewer_is_deterministic_regardless_of_worker_count(self, scenarios):
        # A reviewer whose verdicts moved with thread scheduling would make the curve
        # irreproducible.
        from evals.human import SimulatedReviewer

        def actions(workers):
            seq = build_deposit_sequence(
                StatelessLLM(), scenarios, limit=15, workers=workers,
                reviewer=SimulatedReviewer(rejection_rate=0.4), retriever_for_pool=None,
            )
            return [(e["scenario_id"], e["human_action"]) for e in seq]

        assert actions(1) == actions(5)

    def test_the_result_records_the_rejection_rate_as_an_assumption(self, scenarios):
        from evals.human import SimulatedReviewer

        result = run_replay(StatelessLLM(), k=5, snapshots=(0,), deposit_limit=3,
                            rejection_rate=0.2)
        reviewer = result["deposit_sequence"]["reviewer"]
        assert reviewer["simulated"] is True
        assert reviewer["rejection_rate"] == 0.2
        assert any("stated assumption" in c for c in result["caveats"])


class TestDepositProvenance:
    """Spec §7 asserts a corrected resolution is the higher-value precedent. That was
    untestable while every deposit was a confirmation; with a reviewer producing both, it
    becomes a measurement."""

    def sequence(self, scenarios):
        from evals.human import SimulatedReviewer
        from evals.replay import deposit_provenance

        seq = build_deposit_sequence(
            StatelessLLM(reason_code="netted_settlement"), scenarios, limit=25, workers=4,
            reviewer=SimulatedReviewer(rejection_rate=0.2), retriever_for_pool=None,
        )
        return seq, deposit_provenance(seq)

    def test_every_deposited_precedent_is_attributed(self, scenarios):
        seq, provenance = self.sequence(scenarios)
        deposited = {e["record"].precedent_id for e in seq if e["record"]}
        assert deposited == set(provenance)

    def test_rejected_cases_contribute_no_precedent_to_attribute(self, scenarios):
        seq, provenance = self.sequence(scenarios)
        rejected = [e for e in seq if e.get("human_action") == "rejected"]
        assert rejected, "fixture assumption: some cases are rejected"
        assert all(e["record"] is None for e in rejected)

    def test_both_origins_are_present_to_compare(self, scenarios):
        _seq, provenance = self.sequence(scenarios)
        assert {"confirmed", "corrected"} <= set(provenance.values())

    def test_a_seed_citation_is_attributed_to_the_seed_corpus(self, scenarios):
        # Precedents nobody deposited must not be counted as either kind of deposit.
        result = replay_test_set(
            StatelessLLM(), scenarios, seed_precedent_records(), 5, False, 4, provenance={}
        )
        by_origin = result["precision_by_deposit_origin"]
        assert by_origin["confirmed"]["cited"] == 0
        assert by_origin["corrected"]["cited"] == 0

    def test_precision_is_none_rather_than_zero_when_nothing_was_cited(self, scenarios):
        result = replay_test_set(
            StatelessLLM(), scenarios, seed_precedent_records(), 5, False, 4, provenance={}
        )
        assert result["precision_by_deposit_origin"]["corrected"]["precision"] is None


class TestOutageCannotBecomeACurve:
    """The guard the ablation had and the replay did not, carried here after it cost a run.

    A replay that loses the model partway through produces a *shape* — rising, then
    collapsing — and a shape is far more convincing than a single bad number. The first
    realistic run showed 85.0% at 67 deposits falling to 3.3% at 134 with the control at
    exactly 0.0%. It read like corpus poisoning. It was 58 of 60 cases failing to reach the
    provider.
    """

    def test_a_snapshot_that_cannot_reach_the_model_aborts(self, scenarios):
        from evals.replay import ReplayAborted
        from precedent.adapters.llm.base import LLMUnavailable

        with pytest.raises(ReplayAborted, match="measures the provider"):
            replay_test_set(
                ScriptedLLM([LLMUnavailable("quota exhausted")] * 400),
                scenarios, seed_precedent_records(), 5, False, workers=2,
            )

    def test_no_curve_is_written_when_a_snapshot_aborts(self, scenarios):
        from evals.replay import ReplayAborted
        from precedent.adapters.llm.base import LLMUnavailable

        with pytest.raises(ReplayAborted):
            run_replay(
                ScriptedLLM([LLMUnavailable("down")] * 999),
                k=5, snapshots=(0,), deposit_limit=1, workers=2,
            )

    def test_a_few_transient_failures_do_not_abort(self, scenarios):
        # Tolerable noise must not trip the breaker, or no long run ever completes.
        class MostlyFine:
            model = "mostly-fine"

            def __init__(self):
                self.calls = 0

            def complete(self, system, user, *, temperature=0.0):
                from precedent.adapters.llm.base import LLMResponse, LLMUnavailable

                self.calls += 1
                # Call 2, not 1: an outage during `investigate` is swallowed by design —
                # evidence gathering degrades without aborting the case — so a blip there
                # would never reach the outcome at all.
                if self.calls == 2:
                    raise LLMUnavailable("one blip")
                return StatelessLLM().complete(system, user, temperature=temperature)

        # It must not raise, and the outage must not be confused with the escalations a
        # one-answer model legitimately earns by failing verification. That distinction is
        # the whole job of the breaker: escalating because the arithmetic does not close is
        # a result, escalating because the provider is down is not.
        result = replay_test_set(
            MostlyFine(), scenarios, seed_precedent_records(), 5, False, workers=1
        )
        unavailable = sum(
            1 for c in result["per_case"] if c["said"] == "escalated_model_unavailable"
        )
        assert unavailable == 1
        assert result["escalation_rate"] > unavailable / len(result["per_case"])
