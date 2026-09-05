"""Thin repositories over SQLite — one per table, parameterized SQL, no ORM.

Each repository owns exactly two things beyond plain CRUD: mapping a row to its typed
`records` dataclass, and JSON-encoding/decoding the columns that carry JSON. Business
logic (dedupe policy, idempotency conflict resolution, deposit eligibility) belongs to
`usecases/`, not here.

**Repositories never commit.** The caller owns the transaction boundary — via
`db.transaction(conn)`, or the request-scoped transaction in `api.deps.get_connection`.
This is deliberate and load-bearing for later rings: depositing a precedent (Ring 3) has
to write the human action, the audit row, and the precedent itself as *one* atomic unit.
Under per-method autocommit, a crash midway leaves a precedent with no audit trail behind
it, or a confirmed resolution that never deposited — silently corrupting the very corpus
the whole thesis rests on, and breaking NFR-5 (every state change reconstructable from
`audit_log` alone).
"""

import json
import sqlite3

from precedent.adapters.storage.records import (
    AuditLogRecord,
    BankLineRecord,
    ExceptionRecord,
    IdempotencyRecord,
    LedgerEntryRecord,
    PaymentRecord,
    PrecedentRecord,
    RemediationRecord,
    ResolutionRecord,
    WebhookEventRecord,
)


class PaymentsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: PaymentRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO payments
                (payment_id, order_id, amount_paise, fee_paise, tax_paise,
                 captured_at, status, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.payment_id, record.order_id, record.amount_paise,
                record.fee_paise, record.tax_paise, record.captured_at,
                record.status, record.source,
            ),
        )

    def get(self, payment_id: str) -> PaymentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
        ).fetchone()
        return _row_to_payment(row) if row else None

    def list_by_order(self, order_id: str) -> list[PaymentRecord]:
        rows = self._conn.execute(
            "SELECT * FROM payments WHERE order_id = ? ORDER BY captured_at", (order_id,)
        ).fetchall()
        return [_row_to_payment(row) for row in rows]


class BankLinesRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: BankLineRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO bank_lines (line_id, value_date, amount_paise, direction, narration, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.line_id, record.value_date, record.amount_paise,
                record.direction, record.narration, record.source,
            ),
        )

    def get(self, line_id: str) -> BankLineRecord | None:
        row = self._conn.execute(
            "SELECT * FROM bank_lines WHERE line_id = ?", (line_id,)
        ).fetchone()
        return _row_to_bank_line(row) if row else None

    def list_all(self) -> list[BankLineRecord]:
        rows = self._conn.execute("SELECT * FROM bank_lines ORDER BY value_date").fetchall()
        return [_row_to_bank_line(row) for row in rows]


class LedgerEntriesRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: LedgerEntryRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO ledger_entries
                (entry_id, order_id, invoice_no, customer_name, expected_amount_paise, terms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.entry_id, record.order_id, record.invoice_no,
                record.customer_name, record.expected_amount_paise, record.terms,
            ),
        )

    def get_by_order(self, order_id: str) -> LedgerEntryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM ledger_entries WHERE order_id = ?", (order_id,)
        ).fetchone()
        return _row_to_ledger_entry(row) if row else None

    def list_all(self) -> list[LedgerEntryRecord]:
        rows = self._conn.execute("SELECT * FROM ledger_entries").fetchall()
        return [_row_to_ledger_entry(row) for row in rows]


class WebhookEventsRepository:
    """`event_id` is the primary key; that PK is the entire dedupe mechanism (spec §4)."""

    _VALID_EVENT_TYPES = {"payment.captured", "refund.processed"}

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert_if_new(self, record: WebhookEventRecord) -> bool:
        """Returns True if this event was newly recorded, False if it was a duplicate delivery.

        Validates `event_type` explicitly rather than relying on the table's CHECK
        constraint: `INSERT OR IGNORE` silently swallows CHECK violations exactly like it
        swallows a duplicate-key conflict, which would make a genuinely malformed event
        indistinguishable from a harmless replay.
        """
        if record.event_type not in self._VALID_EVENT_TYPES:
            raise ValueError(f"Unsupported webhook event_type: {record.event_type!r}")
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
                (event_id, event_type, raw_body, signature_valid, received_at, processed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id, record.event_type, record.raw_body,
                int(record.signature_valid), record.received_at, record.processed_at,
            ),
        )
        return cursor.rowcount == 1

    def mark_processed(self, event_id: str, processed_at: str) -> None:
        self._conn.execute(
            "UPDATE webhook_events SET processed_at = ? WHERE event_id = ?",
            (processed_at, event_id),
        )

    def get(self, event_id: str) -> WebhookEventRecord | None:
        row = self._conn.execute(
            "SELECT * FROM webhook_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _row_to_webhook_event(row) if row else None


class ExceptionsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: ExceptionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO exceptions
                (exception_id, batch_id, kind, member_refs, detected_at, status, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.exception_id, record.batch_id, record.kind,
                json.dumps(record.member_refs), record.detected_at,
                record.status, record.correlation_id,
            ),
        )

    def get(self, exception_id: str) -> ExceptionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (exception_id,)
        ).fetchone()
        return _row_to_exception(row) if row else None

    def list_by_batch(self, batch_id: str) -> list[ExceptionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM exceptions WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        return [_row_to_exception(row) for row in rows]


class ResolutionsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: ResolutionRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO resolutions
                (resolution_id, exception_id, proposed_by, confidence, rationale,
                 cited_precedents, verified, human_action, corrected_payload, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.resolution_id, record.exception_id, record.proposed_by,
                record.confidence, record.rationale, json.dumps(record.cited_precedents),
                int(record.verified), record.human_action,
                json.dumps(record.corrected_payload) if record.corrected_payload is not None else None,
                record.resolved_at,
            ),
        )

    def get(self, resolution_id: str) -> ResolutionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM resolutions WHERE resolution_id = ?", (resolution_id,)
        ).fetchone()
        return _row_to_resolution(row) if row else None

    def record_human_action(
        self, resolution_id: str, human_action: str, resolved_at: str, corrected_payload: dict | None = None
    ) -> None:
        self._conn.execute(
            """
            UPDATE resolutions
            SET human_action = ?, resolved_at = ?, corrected_payload = ?
            WHERE resolution_id = ?
            """,
            (
                human_action, resolved_at,
                json.dumps(corrected_payload) if corrected_payload is not None else None,
                resolution_id,
            ),
        )


class PrecedentsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: PrecedentRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO precedents
                (precedent_id, situation, resolution, reason_code, entities,
                 amount_signature, confidence_at_deposit, embedding,
                 derived_from_resolution, deposited_at, corpus_version,
                 times_retrieved, times_cited_correctly)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.precedent_id, record.situation, record.resolution,
                record.reason_code, json.dumps(record.entities), record.amount_signature,
                record.confidence_at_deposit, record.embedding,
                record.derived_from_resolution, record.deposited_at,
                record.corpus_version, record.times_retrieved, record.times_cited_correctly,
            ),
        )

    def get(self, precedent_id: str) -> PrecedentRecord | None:
        row = self._conn.execute(
            "SELECT * FROM precedents WHERE precedent_id = ?", (precedent_id,)
        ).fetchone()
        return _row_to_precedent(row) if row else None

    def list_as_of_corpus_version(self, max_corpus_version: int) -> list[PrecedentRecord]:
        """The corpus snapshot as it existed at `max_corpus_version` deposits (spec §9 Ring 3.4)."""
        rows = self._conn.execute(
            "SELECT * FROM precedents WHERE corpus_version <= ? ORDER BY corpus_version",
            (max_corpus_version,),
        ).fetchall()
        return [_row_to_precedent(row) for row in rows]


class AuditLogRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def append(self, record: AuditLogRecord) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO audit_log
                (correlation_id, stage, actor, input_digest, output_digest,
                 model, latency_ms, tokens, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.correlation_id, record.stage, record.actor, record.input_digest,
                record.output_digest, record.model, record.latency_ms, record.tokens,
                record.reason, record.created_at,
            ),
        )
        return cursor.lastrowid

    def list_by_correlation_id(self, correlation_id: str) -> list[AuditLogRecord]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log WHERE correlation_id = ? ORDER BY id", (correlation_id,)
        ).fetchall()
        return [_row_to_audit_log(row) for row in rows]


class RemediationsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def insert(self, record: RemediationRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO remediations
                (remediation_id, resolution_id, payment_id, amount_paise, idempotency_key,
                 refund_id, status, approved_by, reason, correlation_id, created_at,
                 executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.remediation_id, record.resolution_id, record.payment_id,
                record.amount_paise, record.idempotency_key, record.refund_id,
                record.status, record.approved_by, record.reason, record.correlation_id,
                record.created_at, record.executed_at,
            ),
        )

    def get(self, remediation_id: str) -> RemediationRecord | None:
        row = self._conn.execute(
            "SELECT * FROM remediations WHERE remediation_id = ?", (remediation_id,)
        ).fetchone()
        return _row_to_remediation(row) if row else None

    def get_by_idempotency_key(self, key: str) -> RemediationRecord | None:
        """The live claim on this key, if any: a retry of the same intent derives the same
        key and is recognised here, before a request is sent. The `X-Refund-Idempotency`
        header is the backstop for the window between sending and recording.

        Only 'approved' and 'executed' rows count, matching `idx_remediations_live_key` —
        a refusal sent nothing and must not block a later approval of the same intent.
        """
        row = self._conn.execute(
            "SELECT * FROM remediations WHERE idempotency_key = ? "
            "AND status IN ('approved', 'executed') LIMIT 1",
            (key,),
        ).fetchone()
        return _row_to_remediation(row) if row else None

    def list_by_resolution(self, resolution_id: str) -> list[RemediationRecord]:
        rows = self._conn.execute(
            "SELECT * FROM remediations WHERE resolution_id = ? ORDER BY created_at",
            (resolution_id,),
        ).fetchall()
        return [_row_to_remediation(row) for row in rows]

    def mark_executed(self, remediation_id: str, refund_id: str, executed_at: str) -> None:
        self._conn.execute(
            "UPDATE remediations SET status = 'executed', refund_id = ?, executed_at = ? "
            "WHERE remediation_id = ?",
            (refund_id, executed_at, remediation_id),
        )

    def mark_failed(self, remediation_id: str, reason: str) -> None:
        """Terminal failure — the request was *refused*, so no money moved and the
        reservation is released. Never use this for a timeout: an unknown outcome must stay
        'approved' and keep holding its amount against the ceiling."""
        self._conn.execute(
            "UPDATE remediations SET status = 'failed', reason = ? WHERE remediation_id = ?",
            (reason, remediation_id),
        )

    def usage(self) -> tuple[int, int]:
        """`(refund_count, total_paise)` counted against the ceiling.

        Counts 'approved' alongside 'executed'. An approved-but-unconfirmed row is a refund
        that may have landed, and budget released on a maybe is budget that can be spent
        twice. 'failed' and 'refused' rows are excluded because for those the API told us,
        in so many words, that nothing happened.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise), 0) AS total "
            "FROM remediations WHERE status IN ('approved', 'executed')"
        ).fetchone()
        return int(row["n"]), int(row["total"])


class IdempotencyRepository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, key: str) -> IdempotencyRecord | None:
        row = self._conn.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
        return _row_to_idempotency(row) if row else None

    def put(self, record: IdempotencyRecord) -> None:
        self._conn.execute(
            "INSERT INTO idempotency (key, request_digest, response, created_at) VALUES (?, ?, ?, ?)",
            (record.key, record.request_digest, json.dumps(record.response), record.created_at),
        )


def _row_to_payment(row: sqlite3.Row) -> PaymentRecord:
    return PaymentRecord(
        payment_id=row["payment_id"], order_id=row["order_id"],
        amount_paise=row["amount_paise"], fee_paise=row["fee_paise"],
        tax_paise=row["tax_paise"], captured_at=row["captured_at"],
        status=row["status"], source=row["source"],
    )


def _row_to_bank_line(row: sqlite3.Row) -> BankLineRecord:
    return BankLineRecord(
        line_id=row["line_id"], value_date=row["value_date"],
        amount_paise=row["amount_paise"], direction=row["direction"],
        narration=row["narration"], source=row["source"],
    )


def _row_to_ledger_entry(row: sqlite3.Row) -> LedgerEntryRecord:
    return LedgerEntryRecord(
        entry_id=row["entry_id"], order_id=row["order_id"],
        expected_amount_paise=row["expected_amount_paise"],
        invoice_no=row["invoice_no"], customer_name=row["customer_name"], terms=row["terms"],
    )


def _row_to_webhook_event(row: sqlite3.Row) -> WebhookEventRecord:
    return WebhookEventRecord(
        event_id=row["event_id"], event_type=row["event_type"], raw_body=row["raw_body"],
        signature_valid=bool(row["signature_valid"]), received_at=row["received_at"],
        processed_at=row["processed_at"],
    )


def _row_to_exception(row: sqlite3.Row) -> ExceptionRecord:
    return ExceptionRecord(
        exception_id=row["exception_id"], batch_id=row["batch_id"], kind=row["kind"],
        member_refs=json.loads(row["member_refs"]), detected_at=row["detected_at"],
        status=row["status"], correlation_id=row["correlation_id"],
    )


def _row_to_resolution(row: sqlite3.Row) -> ResolutionRecord:
    return ResolutionRecord(
        resolution_id=row["resolution_id"], exception_id=row["exception_id"],
        proposed_by=row["proposed_by"], confidence=row["confidence"],
        rationale=row["rationale"], cited_precedents=json.loads(row["cited_precedents"]),
        verified=bool(row["verified"]), human_action=row["human_action"],
        corrected_payload=json.loads(row["corrected_payload"]) if row["corrected_payload"] else None,
        resolved_at=row["resolved_at"],
    )


def _row_to_precedent(row: sqlite3.Row) -> PrecedentRecord:
    return PrecedentRecord(
        precedent_id=row["precedent_id"], situation=row["situation"],
        resolution=row["resolution"], reason_code=row["reason_code"],
        entities=json.loads(row["entities"]), amount_signature=row["amount_signature"],
        confidence_at_deposit=row["confidence_at_deposit"],
        embedding=row["embedding"], derived_from_resolution=row["derived_from_resolution"],
        deposited_at=row["deposited_at"], corpus_version=row["corpus_version"],
        times_retrieved=row["times_retrieved"], times_cited_correctly=row["times_cited_correctly"],
    )


def _row_to_audit_log(row: sqlite3.Row) -> AuditLogRecord:
    return AuditLogRecord(
        id=row["id"], correlation_id=row["correlation_id"], stage=row["stage"],
        actor=row["actor"], input_digest=row["input_digest"], output_digest=row["output_digest"],
        model=row["model"], latency_ms=row["latency_ms"], tokens=row["tokens"],
        reason=row["reason"], created_at=row["created_at"],
    )


def _row_to_remediation(row: sqlite3.Row) -> RemediationRecord:
    return RemediationRecord(
        remediation_id=row["remediation_id"], resolution_id=row["resolution_id"],
        payment_id=row["payment_id"], amount_paise=row["amount_paise"],
        idempotency_key=row["idempotency_key"], refund_id=row["refund_id"],
        status=row["status"], approved_by=row["approved_by"], reason=row["reason"],
        correlation_id=row["correlation_id"], created_at=row["created_at"],
        executed_at=row["executed_at"],
    )


def _row_to_idempotency(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        key=row["key"], request_digest=row["request_digest"],
        response=json.loads(row["response"]), created_at=row["created_at"],
    )
