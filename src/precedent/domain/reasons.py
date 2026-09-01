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

    # Rejections — a match must NOT be made
    DUPLICATE_PAYMENT_REJECTED = "duplicate_payment_rejected"

    # Terminal, unresolved
    UNMATCHABLE_NO_COUNTERPART = "unmatchable_no_counterpart"

    # Escalation — every LLM call path must terminate here on failure (spec §7)
    ESCALATED_LOW_CONFIDENCE = "escalated_low_confidence"
    ESCALATED_VERIFY_FAILED = "escalated_verify_failed"
    ESCALATED_PARSE_FAILURE = "escalated_parse_failure"
    ESCALATED_MODEL_UNAVAILABLE = "escalated_model_unavailable"
