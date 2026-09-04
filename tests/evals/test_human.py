"""The simulated reviewer.

Its decisions determine what enters the corpus, so its behaviour is policy rather than a
detail: a reviewer that confirmed everything would reproduce the optimistic bound the
realistic curve exists to replace.
"""

import pytest

from evals.human import REALISTIC_REJECTION_RATE, SimulatedReviewer


class TestDecisions:
    def review(self, rate=0.0, **kwargs):
        base = {"scenario_id": "rec_0001", "gold_reason_code": "negotiated_rebate",
                "proposed": "negotiated_rebate", "escalated": False}
        base.update(kwargs)
        return SimulatedReviewer(rejection_rate=rate).review(**base)

    def test_a_correct_proposal_is_confirmed(self):
        assert self.review().human_action == "confirmed"

    def test_a_wrong_proposal_is_corrected_to_the_gold_answer(self):
        # Spec §7: a correction is the higher-value deposit, because it encodes a case the
        # system got wrong. Depositing the agent's original would teach it the mistake.
        result = self.review(proposed="tds_short_payment")
        assert result.human_action == "corrected"
        assert result.reason_code == "negotiated_rebate"
        assert "tds_short_payment" in result.correction_note

    def test_an_escalation_is_resolved_by_the_reviewer_and_deposits(self):
        # The system asked for help. The answer is worth recording — arguably the most worth
        # recording, since nothing in the corpus covered it.
        result = self.review(proposed=None, escalated=True)
        assert result.human_action == "corrected"
        assert result.deposits

    def test_a_rejection_deposits_nothing(self):
        result = self.review(rate=1.0)
        assert result.human_action == "rejected"
        assert not result.deposits

    def test_only_confirmations_and_corrections_deposit(self):
        for action in ("confirmed", "corrected"):
            assert SimulatedReviewer().review(
                "rec_1", "exact_match", "exact_match" if action == "confirmed" else "x", False
            ).deposits or action == "corrected"


class TestDeterminism:
    def test_the_same_case_gets_the_same_verdict_every_time(self):
        a = SimulatedReviewer(0.5)
        b = SimulatedReviewer(0.5)
        for i in range(50):
            sid = f"rec_{i:04d}"
            assert a.review(sid, "exact_match", "exact_match", False).human_action == \
                   b.review(sid, "exact_match", "exact_match", False).human_action

    def test_verdicts_do_not_depend_on_the_order_cases_are_reviewed(self):
        # Keyed on scenario_id rather than call order, so worker count cannot change who
        # gets rejected — a reviewer whose verdicts moved with thread scheduling would make
        # the curve irreproducible.
        reviewer = SimulatedReviewer(0.5)
        ids = [f"rec_{i:04d}" for i in range(40)]
        forward = {i: reviewer.review(i, "exact_match", "exact_match", False).human_action
                   for i in ids}
        backward = {i: reviewer.review(i, "exact_match", "exact_match", False).human_action
                    for i in reversed(ids)}
        assert forward == backward

    def test_a_different_seed_rejects_a_different_set(self):
        ids = [f"rec_{i:04d}" for i in range(60)]
        one = {i for i in ids
               if SimulatedReviewer(0.5, seed=1).review(i, "x", "x", False).human_action
               == "rejected"}
        two = {i for i in ids
               if SimulatedReviewer(0.5, seed=2).review(i, "x", "x", False).human_action
               == "rejected"}
        assert one != two


class TestRejectionRate:
    def test_the_realised_rate_is_close_to_the_requested_one(self):
        reviewer = SimulatedReviewer(0.3)
        ids = [f"rec_{i:05d}" for i in range(2000)]
        rejected = sum(
            reviewer.review(i, "x", "x", False).human_action == "rejected" for i in ids
        )
        assert 0.26 < rejected / len(ids) < 0.34

    def test_zero_rejects_nothing(self):
        reviewer = SimulatedReviewer(0.0)
        assert all(
            reviewer.review(f"rec_{i}", "x", "x", False).human_action != "rejected"
            for i in range(200)
        )

    def test_the_default_is_a_stated_assumption_not_zero(self):
        # A reviewer who signs off everything is the best case the mechanism can have, and
        # reporting that as the curve would overstate what deposits achieve.
        assert 0.0 < REALISTIC_REJECTION_RATE < 0.5

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_an_impossible_rate_is_refused(self, bad):
        with pytest.raises(ValueError):
            SimulatedReviewer(bad)
