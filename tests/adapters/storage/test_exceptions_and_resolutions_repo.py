import sqlite3

import pytest

from precedent.adapters.storage.records import ExceptionRecord, ResolutionRecord
from precedent.adapters.storage.repositories import ExceptionsRepository, ResolutionsRepository


def make_exception(exception_id="exc_1", batch_id="batch_1"):
    return ExceptionRecord(
        exception_id=exception_id, batch_id=batch_id, kind="tds_short_payment",
        member_refs=["pay_1", "led_1"], detected_at="2026-01-01T00:00:00Z",
        status="open", correlation_id="corr_1",
    )


class TestExceptionsRepository:
    def test_insert_and_get_round_trip_json_member_refs(self, conn):
        repo = ExceptionsRepository(conn)
        record = make_exception()
        repo.insert(record)
        assert repo.get("exc_1") == record

    def test_list_by_batch(self, conn):
        repo = ExceptionsRepository(conn)
        repo.insert(make_exception("exc_1", "batch_1"))
        repo.insert(make_exception("exc_2", "batch_1"))
        repo.insert(make_exception("exc_3", "batch_2"))

        results = repo.list_by_batch("batch_1")

        assert {r.exception_id for r in results} == {"exc_1", "exc_2"}


class TestResolutionsRepository:
    def test_insert_and_get_round_trip(self, conn):
        exceptions_repo = ExceptionsRepository(conn)
        exceptions_repo.insert(make_exception())

        repo = ResolutionsRepository(conn)
        record = ResolutionRecord(
            resolution_id="res_1", exception_id="exc_1", proposed_by="rule",
            confidence=0.95, rationale="exact amount + order match",
            cited_precedents=[], verified=True,
        )
        repo.insert(record)

        assert repo.get("res_1") == record

    def test_rejects_a_resolution_for_a_nonexistent_exception(self, conn):
        repo = ResolutionsRepository(conn)
        record = ResolutionRecord(
            resolution_id="res_1", exception_id="does_not_exist", proposed_by="rule",
            confidence=0.5, rationale="", cited_precedents=[], verified=False,
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert(record)

    def test_rejects_confidence_outside_the_unit_interval(self, conn):
        ExceptionsRepository(conn).insert(make_exception())
        repo = ResolutionsRepository(conn)
        record = ResolutionRecord(
            resolution_id="res_1", exception_id="exc_1", proposed_by="rule",
            confidence=1.5, rationale="", cited_precedents=[], verified=False,
        )
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert(record)

    def test_record_human_action_stores_corrected_payload(self, conn):
        ExceptionsRepository(conn).insert(make_exception())
        repo = ResolutionsRepository(conn)
        repo.insert(
            ResolutionRecord(
                resolution_id="res_1", exception_id="exc_1", proposed_by="agent",
                confidence=0.6, rationale="draft", cited_precedents=["prec_1"], verified=True,
            )
        )

        repo.record_human_action(
            "res_1", human_action="corrected",
            resolved_at="2026-01-01T00:05:00Z",
            corrected_payload={"amount_paise": 9_800},
        )

        result = repo.get("res_1")
        assert result.human_action == "corrected"
        assert result.corrected_payload == {"amount_paise": 9_800}
        assert result.resolved_at == "2026-01-01T00:05:00Z"
