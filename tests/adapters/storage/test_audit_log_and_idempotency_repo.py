from precedent.adapters.storage.records import AuditLogRecord, IdempotencyRecord
from precedent.adapters.storage.repositories import AuditLogRepository, IdempotencyRepository


class TestAuditLogRepository:
    def test_append_assigns_an_id(self, conn):
        repo = AuditLogRepository(conn)
        row_id = repo.append(
            AuditLogRecord(
                correlation_id="corr_1", stage="detected", actor="matcher",
                created_at="2026-01-01T00:00:00Z",
            )
        )
        assert isinstance(row_id, int)

    def test_list_by_correlation_id_preserves_stage_order(self, conn):
        repo = AuditLogRepository(conn)
        for stage in ("detected", "retrieved", "decided", "verified", "gated", "acted"):
            repo.append(
                AuditLogRecord(
                    correlation_id="corr_1", stage=stage, actor="graph",
                    created_at="2026-01-01T00:00:00Z",
                )
            )
        repo.append(
            AuditLogRecord(
                correlation_id="corr_other", stage="detected", actor="matcher",
                created_at="2026-01-01T00:00:00Z",
            )
        )

        rows = repo.list_by_correlation_id("corr_1")

        assert [r.stage for r in rows] == [
            "detected", "retrieved", "decided", "verified", "gated", "acted",
        ]


class TestIdempotencyRepository:
    def test_put_and_get_round_trip(self, conn):
        repo = IdempotencyRepository(conn)
        record = IdempotencyRecord(
            key="receipt_order_1", request_digest="abc123",
            response={"payment_id": "pay_1"}, created_at="2026-01-01T00:00:00Z",
        )
        repo.put(record)
        assert repo.get("receipt_order_1") == record

    def test_get_missing_key_returns_none(self, conn):
        repo = IdempotencyRepository(conn)
        assert repo.get("does_not_exist") is None
