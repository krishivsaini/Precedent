"""Typed row records returned by the repository layer.

Distinct from `domain.matching`'s value objects: those are the lean shape the pure
matcher needs; these carry every column in the schema, including adapter-only concerns
(status, source, audit metadata) the domain layer has no business knowing about.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PaymentRecord:
    payment_id: str
    order_id: str
    amount_paise: int
    captured_at: str
    status: str
    source: str
    fee_paise: int = 0
    tax_paise: int = 0


@dataclass(frozen=True)
class BankLineRecord:
    line_id: str
    value_date: str
    amount_paise: int
    direction: str
    narration: str = ""
    source: str = "synthetic"


@dataclass(frozen=True)
class LedgerEntryRecord:
    entry_id: str
    order_id: str
    expected_amount_paise: int
    invoice_no: str = ""
    customer_name: str = ""
    terms: str = ""


@dataclass(frozen=True)
class WebhookEventRecord:
    event_id: str
    event_type: str
    raw_body: str
    signature_valid: bool
    received_at: str
    processed_at: str | None = None


@dataclass(frozen=True)
class ExceptionRecord:
    exception_id: str
    batch_id: str
    kind: str
    member_refs: list
    detected_at: str
    status: str
    correlation_id: str


@dataclass(frozen=True)
class ResolutionRecord:
    resolution_id: str
    exception_id: str
    proposed_by: str
    confidence: float
    rationale: str
    cited_precedents: list
    verified: bool
    resolved_at: str | None = None
    human_action: str | None = None
    corrected_payload: dict | None = None


@dataclass(frozen=True)
class PrecedentRecord:
    precedent_id: str
    situation: str
    resolution: str
    reason_code: str
    entities: list
    amount_signature: str
    confidence_at_deposit: float
    deposited_at: str
    corpus_version: int
    embedding: bytes | None = None
    derived_from_resolution: str | None = None
    times_retrieved: int = 0
    times_cited_correctly: int = 0


@dataclass(frozen=True)
class AuditLogRecord:
    correlation_id: str
    stage: str
    actor: str
    created_at: str
    id: int | None = None
    input_digest: str | None = None
    output_digest: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    tokens: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    request_digest: str
    response: dict
    created_at: str
