"""The closed reason-code vocabulary for resolutions and precedents.

Every resolution — whether produced by the deterministic matcher (Ring 0) or the
investigation agent (Ring 2+) — is tagged with exactly one of these. Nothing downstream
(precedent deposit, the eval report's exception list, audit logging) is allowed to invent
an ad hoc string instead of extending this enum.
"""

from enum import Enum


class ReasonCode(str, Enum):
    # Deterministic matches
    EXACT_MATCH = "exact_match"
    TOLERANCE_ROUNDING = "tolerance_rounding"
    DATE_WINDOW_TIMING = "date_window_timing"
    NETTED_SETTLEMENT = "netted_settlement"

    # Agent-resolved cases (spec §5 exception classes)
    DIRECT_NEFT_BYPASS = "direct_neft_bypass"
    TDS_SHORT_PAYMENT = "tds_short_payment"
    SPLIT_PAYMENT = "split_payment"
    REFUND_NETTED = "refund_netted"

    # Counterparty knowledge — NOT derivable from the case (Ring 2.5).
    #
    # Every class above can be worked out from the evidence in front of the agent: a round
    # percentage can be tested for, a netted sum can be computed. That is why the Ring 2
    # measurements found the investigation tools substituting for the precedent corpus —
    # a precedent could only ever tell the agent what it could have derived.
    #
    # These two cannot be derived. The shortfall is a rate no statute produces, or an
    # amount held from an earlier transaction: nothing in the case says so. The only way to
    # resolve one is to have seen that counterparty before, which is precisely the knowledge
    # a deposited precedent carries and a tool cannot manufacture.
    NEGOTIATED_REBATE = "negotiated_rebate"
    ADVANCE_ADJUSTED = "advance_adjusted"

    # Rejections — a match must NOT be made
    DUPLICATE_PAYMENT_REJECTED = "duplicate_payment_rejected"

    # Terminal, unresolved
    UNMATCHABLE_NO_COUNTERPART = "unmatchable_no_counterpart"

    # Escalation — every LLM call path must terminate here on failure (spec §7)
    ESCALATED_LOW_CONFIDENCE = "escalated_low_confidence"
    ESCALATED_VERIFY_FAILED = "escalated_verify_failed"
    ESCALATED_PARSE_FAILURE = "escalated_parse_failure"
    ESCALATED_MODEL_UNAVAILABLE = "escalated_model_unavailable"


#: The reason code an exception of each generated *kind* resolves to.
#:
#: Two vocabularies meet here and they are not the same one. A scenario `kind` names what the
#: generator produced — the shape of the situation. A `ReasonCode` names what a resolution
#: concluded — and for four of them the two words differ, because the conclusion says more
#: than the shape did. `duplicate_payment` is a description; `duplicate_payment_rejected` is
#: a decision, and it is the decision that gets filed and later retrieved on.
#:
#: The mapping is not invented here. It is the one already committed in `evals/gold.jsonl` as
#: `expected_reason_code`, which every measured result in this repo was scored against; the
#: test beside it asserts the two still agree, so this table cannot drift from the data the
#: numbers came from.
REASON_CODE_FOR_KIND: dict[str, "ReasonCode"] = {
    "clean_match": ReasonCode.EXACT_MATCH,
    "rounding_delta": ReasonCode.TOLERANCE_ROUNDING,
    "netted_settlement": ReasonCode.NETTED_SETTLEMENT,
    "direct_neft_bypass": ReasonCode.DIRECT_NEFT_BYPASS,
    "tds_short_payment": ReasonCode.TDS_SHORT_PAYMENT,
    "split_payment": ReasonCode.SPLIT_PAYMENT,
    "refund_netted": ReasonCode.REFUND_NETTED,
    "negotiated_rebate": ReasonCode.NEGOTIATED_REBATE,
    "advance_adjusted": ReasonCode.ADVANCE_ADJUSTED,
    "duplicate_payment": ReasonCode.DUPLICATE_PAYMENT_REJECTED,
    "unmatchable": ReasonCode.UNMATCHABLE_NO_COUNTERPART,
}


def reason_code_for(value: str) -> "ReasonCode | None":
    """The reason code for a code *or* an exception kind, or `None` if it is neither.

    Callers hold one of two things and often cannot tell which: a corrected code chosen by a
    reviewer is already a `ReasonCode`, while an agent's own classification arrives as the
    exception's `kind`. Resolving both here keeps that accident of provenance out of the
    call sites — and returns `None` rather than guessing, because a precedent filed under a
    code nothing retrieves on is worse than no precedent at all.
    """
    if not value:
        return None
    try:
        return ReasonCode(value)
    except ValueError:
        return REASON_CODE_FOR_KIND.get(value)
