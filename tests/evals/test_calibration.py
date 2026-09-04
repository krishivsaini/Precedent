"""Calibration and the auto-resolve threshold.

The threshold is the single control governing spec §6's "number that gets someone fired", so
these tests are about the analysis being honest rather than about it being convenient: that
escalations are excluded from reliability, that the sweep is counterfactual over real
outcomes, and that a recommendation states its price rather than only its benefit.
"""

import pytest

from evals.calibration import (
    CANDIDATES,
    load_scored_cases,
    recommend,
    reliability,
    run_calibration,
    sweep,
)
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD


def case(confidence, correct, escalated=False, risk=0, sid="rec_0001"):
    return {"engine": "graph", "arm": "grounded", "scenario_id": sid, "kind": "k",
            "confidence": confidence, "correct": correct, "escalated": escalated,
            "amount_at_risk_paise": risk}


class TestReliability:
    def test_a_confidence_on_a_bin_edge_lands_in_that_bin(self):
        # Floating point `0.90 // 0.05` is 17, not 18. A model that emits round numbers puts
        # nearly every case on an edge, so getting this wrong mislabels the whole table.
        assert reliability([case(0.90, True)])[0]["stated_confidence"] == 0.90
        assert reliability([case(0.95, True)])[0]["stated_confidence"] == 0.95
        assert reliability([case(1.0, True)])[0]["stated_confidence"] == 1.0

    def test_it_reports_observed_accuracy_against_the_claim(self):
        cases = [case(0.9, True)] * 7 + [case(0.9, False)] * 3
        row = reliability(cases)[0]
        assert row["observed_accuracy"] == 0.7
        assert row["gap"] == pytest.approx(-0.2)

    def test_a_perfectly_calibrated_agent_has_no_gap(self):
        cases = [case(0.9, True)] * 9 + [case(0.9, False)]
        assert reliability(cases)[0]["gap"] == pytest.approx(0.0)

    def test_escalated_cases_are_excluded(self):
        # The agent declined to answer, so there is no confidence claim to check against an
        # outcome. Counting them would score a refusal as a wrong prediction.
        cases = [case(0.9, True)] * 5 + [case(0.1, False, escalated=True)] * 50
        rows = reliability(cases)
        assert sum(r["n"] for r in rows) == 5

    def test_overconfidence_shows_as_a_negative_gap(self):
        # Signed deliberately: over- and under-confidence need different fixes.
        assert reliability([case(0.95, False)] * 10)[0]["gap"] < 0


class TestSweep:
    def test_raising_the_threshold_never_increases_exposure(self):
        cases = [case(0.8, False, risk=10_000, sid="a"),
                 case(0.9, True, sid="b"),
                 case(0.95, False, risk=5_000, sid="c")]
        rows = sweep(cases)
        exposures = [r["false_resolution_cost_inr"] for r in rows]
        assert exposures == sorted(exposures, reverse=True)

    def test_raising_the_threshold_never_increases_resolution(self):
        cases = [case(0.8, True, sid="a"), case(0.9, True, sid="b"),
                 case(0.95, True, sid="c")]
        rates = [r["resolution_rate"] for r in sweep(cases)]
        assert rates == sorted(rates, reverse=True)

    def test_the_three_outcomes_always_account_for_every_case(self):
        cases = [case(0.8, False, risk=100, sid="a"), case(0.92, True, sid="b"),
                 case(0.5, False, escalated=True, sid="c")]
        for row in sweep(cases):
            total = (row["resolution_rate"] + row["escalation_rate"] + row["error_rate"])
            # Rates are rounded to 4dp for the JSON, so exact equality is not available.
            assert total == pytest.approx(1.0, abs=1e-3)

    def test_an_impossible_threshold_escalates_everything(self):
        rows = sweep([case(0.9, True)], candidates=(0.99,))
        assert rows[0]["escalation_rate"] == 1.0
        assert rows[0]["false_resolution_cost_inr"] == 0

    def test_candidates_are_dense_where_the_confidences_are(self):
        # Sweeping 0.1 to 0.5 would produce a tidy table about nothing: the agent's stated
        # confidences all sit between 0.8 and 1.0.
        assert len([c for c in CANDIDATES if 0.85 <= c <= 0.95]) >= 4


class TestRecommendation:
    def test_it_states_the_price_not_only_the_benefit(self):
        cases = [case(0.8, False, risk=50_000, sid="a"), case(0.9, True, sid="b")]
        rec = recommend(sweep(cases))
        assert "resolution_given_up_pp" in rec
        assert "exposure_saved_inr" in rec

    def test_it_separates_a_free_improvement_from_a_priced_one(self):
        # Collapsing these into one number hides the more useful of the two.
        rec = recommend(sweep(load_scored_cases()))
        assert "free_improvement" in rec and "best_value_for_money" in rec

    def test_it_records_the_preference_as_a_judgement_not_a_computation(self):
        # Choosing exposure over coverage is a domain call, and saying so is the difference
        # between a measurement and a recommendation wearing one's clothes.
        rec = recommend(sweep(load_scored_cases()))
        assert "judgement" in rec

    def test_it_declines_to_recommend_when_nothing_helps(self):
        rec = recommend(sweep([case(0.99, True)]))
        assert rec["recommended"] == 0.80
        assert "no candidate reduced exposure" in rec["why"]


class TestAgainstTheShippedThreshold:
    def test_the_shipped_threshold_is_the_one_the_data_recommends(self):
        # The whole point of Ring 4: the constant is derived, and stays derived. If a re-run
        # moves the recommendation, this fails rather than the constant silently drifting
        # out of step with the evidence for it.
        rec = run_calibration()["recommendation"]
        assert DEFAULT_AUTO_RESOLVE_THRESHOLD == rec["recommended"]

    def test_it_is_no_longer_the_ring_zero_placeholder(self):
        assert DEFAULT_AUTO_RESOLVE_THRESHOLD != 0.8

    def test_the_result_records_that_it_is_not_portable(self):
        result = run_calibration()
        assert any("not evidence about another" in c or "another" in c
                   for c in result["caveats"])
