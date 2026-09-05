from precedent.domain.reasons import (
    REASON_CODE_FOR_KIND,
    ReasonCode,
    reason_code_for,
)


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


class TestTheKindToReasonCodeMap:
    """`REASON_CODE_FOR_KIND` restates a mapping that already exists in committed data.

    A scenario `kind` names what the generator produced; a `ReasonCode` names what a
    resolution concluded. Four of them differ, and the gap is where the approval gate used to
    fail: `duplicate_payment` is not a `ReasonCode`, so confirming a duplicate charge could
    not file a precedent — and Ring 5, which only ever triggers on
    `duplicate_payment_rejected`, was unreachable without a correction.
    """

    def test_it_agrees_with_the_gold_data_every_result_was_scored_against(self):
        # The point of the test. If someone edits the table by hand and it stops matching
        # the file the eval scored against, the two have silently forked.
        import json
        from pathlib import Path

        gold = Path(__file__).resolve().parents[2] / "evals" / "gold.jsonl"
        committed = {}
        for line in gold.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            committed[row["kind"]] = row["expected_reason_code"]

        mapped = {k: v.value for k, v in REASON_CODE_FOR_KIND.items()}
        assert mapped == committed

    def test_a_reason_code_resolves_to_itself(self):
        assert reason_code_for("tds_short_payment") is ReasonCode.TDS_SHORT_PAYMENT

    def test_an_exception_kind_resolves_to_the_code_it_concludes_to(self):
        assert reason_code_for("duplicate_payment") is ReasonCode.DUPLICATE_PAYMENT_REJECTED
        assert reason_code_for("unmatchable") is ReasonCode.UNMATCHABLE_NO_COUNTERPART

    def test_the_duplicate_case_reaches_the_code_ring_5_acts_on(self):
        # The whole reason this map exists: remediation triggers on this exact value.
        from precedent.usecases.remediate import REMEDIABLE_REASON_CODES

        assert reason_code_for("duplicate_payment").value in REMEDIABLE_REASON_CODES

    def test_nonsense_resolves_to_nothing_rather_than_a_guess(self):
        # A precedent filed under a code nothing retrieves on is worse than no precedent.
        assert reason_code_for("not_a_thing") is None
        assert reason_code_for("") is None
