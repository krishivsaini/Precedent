"""The read-only screens: what the corpus holds, and whether it helped.

The assertion these mostly make is about provenance. A corpus of hand-written entries is a
knowledge base like any other; the claim this project makes is that it *grows from its own
operation*. A screen that showed 43 precedents without saying which kind they were would let
that claim pass unchecked, so the split is the thing under test.
"""

import re

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import (
    ExceptionRecord,
    PrecedentRecord,
    ResolutionRecord,
)
from precedent.adapters.storage.repositories import (
    ExceptionsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.api.main import create_app

NOW = "2026-09-05T09:00:00+00:00"


def flat(body: str) -> str:
    return re.sub(r"\s+", " ", body)


def precedent(pid, *, derived=None, version=0, code="tds_short_payment"):
    return PrecedentRecord(
        precedent_id=pid,
        situation="Payments from this counterparty arrive short of the invoice by a "
                  "proportion matching no statutory band.",
        resolution="Close it under the counterparty's negotiated rebate.",
        reason_code=code, entities=["Acme"], amount_signature="sig",
        confidence_at_deposit=0.93, deposited_at=NOW, corpus_version=version,
        derived_from_resolution=derived,
    )


def build(db, records=()):
    conn = connect(str(db))
    init_db(conn)
    # `precedents.derived_from_resolution` is a real foreign key — the provenance a deposited
    # precedent claims has to point at a resolution that exists. So the row it points to is
    # created here rather than the constraint being worked around.
    ExceptionsRepository(conn).insert(ExceptionRecord(
        exception_id="exc_1", batch_id="b1", kind="negotiated_rebate",
        member_refs=[], detected_at=NOW, status="open", correlation_id="corr_1",
    ))
    ResolutionsRepository(conn).insert(ResolutionRecord(
        resolution_id="res_1", exception_id="exc_1", proposed_by="agent",
        confidence=0.94, rationale="a standing rebate", cited_precedents=[], verified=True,
    ))
    repo = PrecedentsRepository(conn)
    for record in records:
        repo.insert(record)
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=str(db)))


@pytest.fixture
def mixed(tmp_path):
    with build(tmp_path / "k.db", [
        precedent("prec_seed_0001"),
        precedent("prec_seed_0002", code="negotiated_rebate"),
        precedent("prec_0001", derived="res_1", version=1, code="negotiated_rebate"),
    ]) as c:
        yield c


class TestTheCorpusSeparatesItsTwoOrigins:
    def test_it_counts_hand_written_and_authored_apart(self, mixed):
        body = flat(mixed.get("/corpus").text)
        assert "All (3)" in body
        assert "Written by hand (2)" in body
        assert "Authored by operation (1)" in body

    def test_each_entry_says_where_it_came_from(self, mixed):
        body = mixed.get("/corpus").text
        assert "written by hand at set-up" in body
        assert "written from res_1" in body

    def test_an_authored_entry_links_back_to_the_decision_behind_it(self, mixed):
        # Provenance a reader can follow, not just assert.
        assert 'href="/exceptions/res_1"' in mixed.get("/corpus").text

    def test_the_filter_narrows_it(self, mixed):
        assert "written from res_1" not in mixed.get("/corpus?show=seeded").text
        assert "written by hand at set-up" not in mixed.get("/corpus?show=authored").text

    def test_an_unknown_filter_falls_back_to_everything(self, mixed):
        # A bad query string should not produce an empty screen that reads as "no corpus".
        assert "written from res_1" in mixed.get("/corpus?show=nonsense").text


class TestTheCorpusIsHonestBeforeItHasGrown:
    def test_a_corpus_that_has_never_been_deposited_into_says_so(self, tmp_path):
        with build(tmp_path / "s.db", [precedent("prec_seed_0001")]) as client:
            body = flat(client.get("/corpus").text)
            assert "Nothing has been deposited from operation yet" in body

    def test_an_empty_corpus_directs_rather_than_apologises(self, tmp_path):
        with build(tmp_path / "e.db") as client:
            body = flat(client.get("/corpus").text)
            assert "The corpus is empty" in body
            assert "reviewers confirm and correct resolutions" in body
            assert "sorry" not in body.lower()


class TestTheLearningScreen:
    def test_it_is_built_on_the_classes_that_cannot_be_derived(self, mixed):
        body = mixed.get("/learns").text
        assert "cannot be derived" in body
        # negotiated_rebate is one of the two counterparty codes; tds_short_payment is not.
        assert "negotiated rebate" in body

    def test_it_shows_the_before_and_the_after(self, mixed):
        body = mixed.get("/learns").text
        assert "first time it meets a counterparty" in body.lower()
        assert "After a reviewer has resolved one" in body


class TestTheResultScreenNeverComputesAFigureOfItsOwn:
    def test_it_reports_the_committed_measurement(self, mixed):
        body = mixed.get("/result").text
        assert "Does it work" in body
        # Read from evals/results/*.json at request time, not typed in.
        assert "evals/results/" in body
        assert re.search(r"\d+\.\d%", body)

    def test_it_shows_the_control_beside_the_result(self, mixed):
        # The control is the line that decides whether the other one means anything.
        assert "random control" in mixed.get("/result").text

    def test_it_carries_its_own_caveats(self, mixed):
        assert "What this does not show" in mixed.get("/result").text

    def test_it_names_the_model_behind_the_number(self, mixed):
        assert "Model:" in mixed.get("/result").text
