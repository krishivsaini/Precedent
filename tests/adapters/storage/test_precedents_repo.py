import sqlite3

import pytest

from precedent.adapters.storage.records import PrecedentRecord
from precedent.adapters.storage.repositories import PrecedentsRepository


def make_precedent(precedent_id="prec_1", corpus_version=0, derived_from_resolution=None):
    return PrecedentRecord(
        precedent_id=precedent_id,
        situation="Payment short by exactly 2% of order value, narration mentions TDS",
        resolution="Match against ledger net of 2% TDS deduction",
        reason_code="tds_short_payment",
        entities=["Acme Co"],
        amount_signature="short_by_2pct_tds",
        deposited_at="2026-01-01T00:00:00Z",
        corpus_version=corpus_version,
        derived_from_resolution=derived_from_resolution,
    )


class TestPrecedentsRepository:
    def test_insert_and_get_round_trip(self, conn):
        repo = PrecedentsRepository(conn)
        record = make_precedent()
        repo.insert(record)
        assert repo.get("prec_1") == record

    def test_seed_precedents_may_have_no_derived_resolution(self, conn):
        # ~40 hand-written seed precedents (Ring 1) are authored directly, not derived
        # from a system resolution — this must not require a fake FK target.
        repo = PrecedentsRepository(conn)
        record = make_precedent(derived_from_resolution=None)
        repo.insert(record)
        assert repo.get("prec_1").derived_from_resolution is None

    def test_rejects_a_reason_code_outside_the_closed_vocabulary(self, conn):
        repo = PrecedentsRepository(conn)
        record = make_precedent()
        object.__setattr__(record, "reason_code", "made_up_reason")
        with pytest.raises(sqlite3.IntegrityError):
            repo.insert(record)

    def test_list_as_of_corpus_version_returns_only_deposits_up_to_that_point(self, conn):
        repo = PrecedentsRepository(conn)
        repo.insert(make_precedent("prec_v0", corpus_version=0))
        repo.insert(make_precedent("prec_v50", corpus_version=50))
        repo.insert(make_precedent("prec_v100", corpus_version=100))

        snapshot = repo.list_as_of_corpus_version(50)

        assert {r.precedent_id for r in snapshot} == {"prec_v0", "prec_v50"}
