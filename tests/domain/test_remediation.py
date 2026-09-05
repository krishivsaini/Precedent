"""The ceiling and its stopping rule.

These are the cheapest tests in the project to write and the most expensive ones to be
without: every branch here is the difference between a refund going out and not.
"""

import pytest

from precedent.domain.remediation import (
    MIN_IDEMPOTENCY_KEY_LENGTH,
    CeilingUsage,
    RemediationCeiling,
    check_ceiling,
    refund_idempotency_key,
)


class TestCeilingConstruction:
    def test_a_float_limit_is_refused(self):
        # NFR-1 at this boundary too: a ceiling of 500.0 rupees-or-paise is ambiguous, and
        # the ambiguity is only discovered when it fails to stop something.
        with pytest.raises(TypeError):
            RemediationCeiling(max_total_paise=500.0)

    def test_a_negative_limit_is_refused(self):
        with pytest.raises(ValueError):
            RemediationCeiling(max_refunds=-1)

    def test_a_per_call_cap_above_the_total_is_refused(self):
        # It could never bind, which means someone typed the wrong number.
        with pytest.raises(ValueError):
            RemediationCeiling(max_total_paise=100_00, max_single_paise=200_00)

    def test_the_defaults_are_small(self):
        # Deliberately: one operator, no second approver. The worst case should be
        # embarrassing, not expensive.
        ceiling = RemediationCeiling()
        assert ceiling.max_refunds <= 5
        assert ceiling.max_total_paise <= 1000_00


class TestEachLimitBindsIndependently:
    """Three limits exist because each stops something the others let through."""

    def test_the_count_stops_many_small_refunds(self):
        ceiling = RemediationCeiling(max_refunds=3, max_total_paise=500_00,
                                     max_single_paise=250_00)
        decision = check_ceiling(ceiling, CeilingUsage(refunds_made=3, total_paise=300),
                                 amount_paise=100)
        assert not decision.allowed
        assert "3 of 3" in decision.reason

    def test_the_per_call_cap_stops_one_misplaced_decimal(self):
        # The case neither other limit catches on the *first* call — which is the only call
        # that matters, because by the time the total binds the money has gone.
        ceiling = RemediationCeiling(max_refunds=3, max_total_paise=500_00,
                                     max_single_paise=250_00)
        decision = check_ceiling(ceiling, CeilingUsage(), amount_paise=400_00)
        assert not decision.allowed
        assert "single-refund cap" in decision.reason

    def test_the_total_stops_the_last_refund_that_would_cross_it(self):
        ceiling = RemediationCeiling(max_refunds=9, max_total_paise=500_00,
                                     max_single_paise=250_00)
        decision = check_ceiling(ceiling, CeilingUsage(refunds_made=2, total_paise=400_00),
                                 amount_paise=150_00)
        assert not decision.allowed
        assert "total ceiling" in decision.reason

    def test_a_refund_that_lands_exactly_on_the_total_is_allowed(self):
        # The boundary counts as within, matching `money.within_tolerance`. A limit that
        # secretly means "one paise less than stated" is a limit nobody can reason about.
        ceiling = RemediationCeiling(max_refunds=9, max_total_paise=500_00,
                                     max_single_paise=250_00)
        decision = check_ceiling(ceiling, CeilingUsage(1, 400_00), amount_paise=100_00)
        assert decision.allowed

    def test_an_allowed_decision_reports_what_would_remain(self):
        decision = check_ceiling(RemediationCeiling(), CeilingUsage(), 100_00)
        assert decision.allowed
        assert decision.remaining_refunds == 3
        assert decision.remaining_paise == 500_00


class TestStoppingRuleUnderExhaustion:
    def test_driving_the_ceiling_to_exhaustion_stops_at_the_stated_limit(self):
        # Ring 5.2 asks for the stopping rule to be *driven* to exhaustion rather than
        # reviewed. Spend the budget one refund at a time and assert both where it stops
        # and that it never went over.
        ceiling = RemediationCeiling(max_refunds=5, max_total_paise=500_00,
                                     max_single_paise=200_00)
        usage = CeilingUsage()
        approved, spent = 0, 0
        for _ in range(50):
            decision = check_ceiling(ceiling, usage, amount_paise=150_00)
            if not decision.allowed:
                break
            approved += 1
            spent += 150_00
            usage = CeilingUsage(approved, spent)
        else:  # pragma: no cover - only reached if the rule never stops
            pytest.fail("the ceiling never refused; the stopping rule does not stop")

        assert approved == 3, "₹150 x 3 = ₹450; a fourth would cross ₹500"
        assert spent <= ceiling.max_total_paise
        assert approved <= ceiling.max_refunds

    def test_an_exhausted_ceiling_refuses_every_later_amount_including_one_paise(self):
        ceiling = RemediationCeiling(max_refunds=3, max_total_paise=500_00,
                                     max_single_paise=250_00)
        exhausted = CeilingUsage(refunds_made=3, total_paise=500_00)
        for amount in (1, 100, 250_00):
            assert not check_ceiling(ceiling, exhausted, amount).allowed

    def test_a_zero_refund_ceiling_permits_nothing(self):
        # The kill switch. Setting max_refunds to 0 must be a true stop, not an off-by-one.
        ceiling = RemediationCeiling(max_refunds=0, max_total_paise=0, max_single_paise=0)
        assert not check_ceiling(ceiling, CeilingUsage(), 1).allowed


class TestAmountValidation:
    @pytest.mark.parametrize("bad", [0, -1, -100_00])
    def test_a_non_positive_amount_is_refused(self, bad):
        with pytest.raises(ValueError):
            check_ceiling(RemediationCeiling(), CeilingUsage(), bad)

    def test_a_float_amount_is_refused(self):
        with pytest.raises(TypeError):
            check_ceiling(RemediationCeiling(), CeilingUsage(), 100.0)

    def test_a_bool_is_not_an_int_here(self):
        # `isinstance(True, int)` is True in Python, so True would otherwise pass as one
        # paise. Worth a test because the bug is invisible on reading.
        with pytest.raises(TypeError):
            check_ceiling(RemediationCeiling(), CeilingUsage(), True)


class TestIdempotencyKey:
    def test_the_same_intent_derives_the_same_key(self):
        # The whole point: a retry after a crash must reuse the key, or it refunds twice.
        a = refund_idempotency_key("res_1", "pay_1", 100_00)
        b = refund_idempotency_key("res_1", "pay_1", 100_00)
        assert a == b

    @pytest.mark.parametrize("changed", [
        ("res_2", "pay_1", 100_00),
        ("res_1", "pay_2", 100_00),
        ("res_1", "pay_1", 200_00),
    ])
    def test_a_different_intent_derives_a_different_key(self, changed):
        # A changed amount under the same key is the live-measured 409 case. The key must
        # move with the request body so that never happens by accident.
        assert refund_idempotency_key(*changed) != refund_idempotency_key(
            "res_1", "pay_1", 100_00
        )

    def test_the_key_clears_razorpays_measured_minimum_length(self):
        # Measured, not read: a 5-character key returns HTTP 400 with
        # `input_validation_failed` from the live test-mode API.
        key = refund_idempotency_key("r", "p", 1)
        assert len(key) >= MIN_IDEMPOTENCY_KEY_LENGTH

    def test_the_key_is_stable_across_processes(self):
        # blake2b rather than hash(): Python's hash() is salted per process, so a key built
        # on it would differ after a restart — exactly when idempotency has to hold.
        assert refund_idempotency_key("res_1", "pay_1", 100_00) == "rem-" + __import__(
            "hashlib"
        ).blake2b(b"res_1|pay_1|10000", digest_size=8).hexdigest()
