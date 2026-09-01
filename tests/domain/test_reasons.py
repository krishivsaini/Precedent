from precedent.domain.reasons import ReasonCode


def test_reason_code_is_a_string_enum():
    # precedents.reason_code and audit_log rows persist this as plain text (spec §4) —
    # it must serialize as its value, not as "ReasonCode.EXACT_MATCH".
    assert ReasonCode.EXACT_MATCH == "exact_match"
    assert str(ReasonCode.EXACT_MATCH.value) == "exact_match"


def test_all_values_are_unique():
    values = [member.value for member in ReasonCode]
    assert len(values) == len(set(values))


def test_escalation_codes_are_distinguishable_from_resolution_codes():
    escalation_codes = {code for code in ReasonCode if code.value.startswith("escalated_")}
    assert ReasonCode.ESCALATED_LOW_CONFIDENCE in escalation_codes
    assert ReasonCode.EXACT_MATCH not in escalation_codes
