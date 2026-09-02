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
