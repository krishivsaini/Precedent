"""SQLite schema, exactly per spec §4.

CHECK(typeof(...) = 'integer') on every paise column is the storage-layer half of NFR-1
("money is integer paise everywhere, no floats, ever") — `domain.money` refuses floats at
the application boundary; this refuses them at the last line of defense too, in case a
future caller bypasses the domain layer.

`precedents.reason_code` is constrained to the same closed vocabulary as
`domain.reasons.ReasonCode` — the two must be kept in sync by hand; there is no single
source of truth shared between SQL and Python here.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id      TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (typeof(amount_paise) = 'integer'),
    fee_paise       INTEGER NOT NULL DEFAULT 0 CHECK (typeof(fee_paise) = 'integer'),
    tax_paise       INTEGER NOT NULL DEFAULT 0 CHECK (typeof(tax_paise) = 'integer'),
    captured_at     TEXT NOT NULL,
    status          TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('razorpay', 'synthetic'))
);

CREATE TABLE IF NOT EXISTS bank_lines (
    line_id         TEXT PRIMARY KEY,
    value_date      TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL CHECK (typeof(amount_paise) = 'integer'),
    direction       TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
    narration       TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'synthetic'
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id                TEXT PRIMARY KEY,
    order_id                TEXT NOT NULL,
    invoice_no              TEXT NOT NULL DEFAULT '',
    customer_name           TEXT NOT NULL DEFAULT '',
    expected_amount_paise   INTEGER NOT NULL CHECK (typeof(expected_amount_paise) = 'integer'),
    terms                   TEXT NOT NULL DEFAULT ''
);

-- x-razorpay-event-id is the primary key; that PK IS the dedupe mechanism (spec §4).
-- INSERT OR IGNORE on this table, return 200, then process from storage.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL CHECK (event_type IN ('payment.captured', 'refund.processed')),
    raw_body        TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK (signature_valid IN (0, 1)),
    received_at     TEXT NOT NULL,
    processed_at    TEXT
);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id    TEXT PRIMARY KEY,
    batch_id        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    member_refs     TEXT NOT NULL,  -- JSON
    detected_at     TEXT NOT NULL,
    status          TEXT NOT NULL,
    correlation_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id       TEXT PRIMARY KEY,
    exception_id        TEXT NOT NULL REFERENCES exceptions(exception_id),
    proposed_by         TEXT NOT NULL CHECK (proposed_by IN ('rule', 'agent')),
    confidence          REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    rationale           TEXT NOT NULL DEFAULT '',
    cited_precedents    TEXT NOT NULL DEFAULT '[]',  -- JSON
    verified            INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    human_action        TEXT CHECK (human_action IN ('confirmed', 'corrected', 'rejected')),
    corrected_payload   TEXT,  -- JSON, only set when human_action = 'corrected'
    resolved_at         TEXT
);

-- derived_from_resolution is nullable: the ~40 hand-written seed precedents (Ring 1) are
-- authored directly, not derived from a system resolution.
CREATE TABLE IF NOT EXISTS precedents (
    precedent_id                TEXT PRIMARY KEY,
    situation                   TEXT NOT NULL,
    resolution                  TEXT NOT NULL,
    reason_code                 TEXT NOT NULL CHECK (reason_code IN (
        'exact_match', 'tolerance_rounding', 'date_window_timing', 'netted_settlement',
        'direct_neft_bypass', 'tds_short_payment', 'split_payment', 'refund_netted',
        'negotiated_rebate', 'advance_adjusted',
        'duplicate_payment_rejected', 'unmatchable_no_counterpart',
        'escalated_low_confidence', 'escalated_verify_failed',
        'escalated_parse_failure', 'escalated_model_unavailable'
    )),
    entities                    TEXT NOT NULL DEFAULT '[]',  -- JSON
    amount_signature             TEXT NOT NULL DEFAULT '',
    confidence_at_deposit       REAL NOT NULL CHECK (confidence_at_deposit BETWEEN 0.0 AND 1.0),
    embedding                   BLOB,
    derived_from_resolution     TEXT REFERENCES resolutions(resolution_id),
    deposited_at                TEXT NOT NULL,
    corpus_version               INTEGER NOT NULL,
    times_retrieved              INTEGER NOT NULL DEFAULT 0,
    times_cited_correctly        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id  TEXT NOT NULL,
    stage           TEXT NOT NULL CHECK (stage IN (
        'detected', 'retrieved', 'decided', 'verified', 'gated', 'acted'
    )),
    actor           TEXT NOT NULL,
    input_digest    TEXT,
    output_digest   TEXT,
    model           TEXT,
    latency_ms      INTEGER,
    tokens          INTEGER,
    reason          TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key             TEXT PRIMARY KEY,
    request_digest  TEXT NOT NULL,
    response        TEXT NOT NULL,  -- JSON
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resolutions_exception_id ON resolutions(exception_id);
CREATE INDEX IF NOT EXISTS idx_precedents_corpus_version ON precedents(corpus_version);
CREATE INDEX IF NOT EXISTS idx_audit_log_correlation_id ON audit_log(correlation_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_order_id ON ledger_entries(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);
"""
