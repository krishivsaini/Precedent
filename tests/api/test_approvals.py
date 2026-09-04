"""The approval endpoints.

Thin by design, so these tests are mostly about the rules this layer must not be able to
weaken: a decision it cannot validate, a case it already decided, a precedent it shows
without its content.
"""

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

NOW = "2026-09-03T10:00:00+00:00"


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "approvals.db"
    conn = connect(str(db))
    init_db(conn)

    exceptions = ExceptionsRepository(conn)
    resolutions = ResolutionsRepository(conn)
    precedents = PrecedentsRepository(conn)

    precedents.insert(PrecedentRecord(
        precedent_id="prec_0001",
        situation="Payments from Konark Logistics arrive short by a non-statutory proportion.",
        resolution="Konark settles under a negotiated rebate; close the invoice in full.",
        reason_code="negotiated_rebate", entities=["Konark Logistics"],
        amount_signature="negotiated_rebate", confidence_at_deposit=0.93,
        deposited_at=NOW, corpus_version=1, derived_from_resolution=None,
    ))
    for n, detected in ((1, "2026-09-01T09:00:00+00:00"), (2, "2026-09-02T09:00:00+00:00")):
        exceptions.insert(ExceptionRecord(
            exception_id=f"exc_{n:04d}", batch_id="batch_1", kind="negotiated_rebate",
            member_refs=[f"rec_{n:04d}"], detected_at=detected, status="open",
            correlation_id=f"corr_{n:04d}",
        ))
        resolutions.insert(ResolutionRecord(
            resolution_id=f"res_{n:04d}", exception_id=f"exc_{n:04d}", proposed_by="agent",
            confidence=0.9, rationale="matched the customer's negotiated terms",
            cited_precedents=["prec_0001"], verified=True,
        ))
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db))
    with TestClient(app) as test_client:
        yield test_client


class TestListPending:
    def test_it_lists_undecided_exceptions(self, client):
        body = client.get("/approvals").json()
        assert body["count"] == 2

    def test_it_lists_oldest_first(self, client):
        # Newest-first quietly starves the hardest items, and those are the ones a precedent
        # corpus most needs resolved.
        ids = [row["resolution_id"] for row in client.get("/approvals").json()["pending"]]
        assert ids == ["res_0001", "res_0002"]

    def test_a_decided_case_leaves_the_queue(self, client):
        client.post("/approvals/res_0001", json={"human_action": "confirmed"})
        body = client.get("/approvals").json()
        assert body["count"] == 1
        assert body["pending"][0]["resolution_id"] == "res_0002"


class TestGetOne:
    def test_it_returns_the_proposal_and_its_exception(self, client):
        body = client.get("/approvals/res_0001").json()
        assert body["resolution"]["confidence"] == 0.9
        assert body["exception"]["kind"] == "negotiated_rebate"

    def test_cited_precedents_come_back_in_full_not_as_ids(self, client):
        # A reviewer asked to confirm a resolution *because precedents support it* cannot do
        # that without reading them. Ids alone turn the gate into a rubber stamp.
        cited = client.get("/approvals/res_0001").json()["cited_precedents"]
        assert len(cited) == 1
        assert "Konark Logistics" in cited[0]["situation"]
        assert cited[0]["resolution"]

    def test_it_shows_whether_a_precedent_was_self_authored(self, client):
        # A precedent the system wrote about itself should be visible as such to whoever is
        # deciding whether to trust it.
        cited = client.get("/approvals/res_0001").json()["cited_precedents"]
        assert "derived_from_resolution" in cited[0]

    def test_a_citation_that_no_longer_exists_is_reported_not_hidden(self, client, tmp_path):
        conn = connect(str(tmp_path / "approvals.db"))
        ResolutionsRepository(conn).insert(ResolutionRecord(
            resolution_id="res_0003", exception_id="exc_0001", proposed_by="agent",
            confidence=0.8, rationale="cites something absent",
            cited_precedents=["prec_0001", "prec_ghost"], verified=True,
        ))
        conn.commit()
        conn.close()
        body = client.get("/approvals/res_0003").json()
        assert body["missing_cited_precedents"] == ["prec_ghost"]
        assert len(body["cited_precedents"]) == 1

    def test_an_unknown_resolution_is_a_404(self, client):
        assert client.get("/approvals/res_nope").status_code == 404


class TestDecide:
    def test_confirming_records_the_action_and_signals_a_deposit(self, client):
        body = client.post("/approvals/res_0001", json={"human_action": "confirmed"}).json()
        assert body["human_action"] == "confirmed"
        assert body["deposits"] is True

    def test_rejecting_records_the_action_and_deposits_nothing(self, client):
        body = client.post("/approvals/res_0001", json={"human_action": "rejected"}).json()
        assert body["deposits"] is False

    def test_a_correction_carries_the_corrected_code(self, client):
        response = client.post("/approvals/res_0001", json={
            "human_action": "corrected",
            "corrected_reason_code": "advance_adjusted",
            "correction_note": "this customer holds an advance",
        })
        assert response.status_code == 200
        assert response.json()["deposits"] is True

    @pytest.mark.parametrize("body", [
        {}, {"human_action": ""}, {"human_action": "looks fine"}, {"human_action": None},
    ])
    def test_a_malformed_decision_is_refused(self, client, body):
        # This is where a state-changing financial action is authorised. An endpoint that
        # accepted a malformed decision would be relying on a lower layer to notice.
        assert client.post("/approvals/res_0001", json=body).status_code == 422

    def test_a_correction_with_nothing_corrected_is_refused(self, client):
        # It would deposit the agent's original answer labelled as a human correction.
        response = client.post("/approvals/res_0001", json={"human_action": "corrected"})
        assert response.status_code == 422
        assert "corrected_reason_code" in response.json()["detail"]

    def test_deciding_twice_is_a_conflict_not_an_overwrite(self, client):
        # A second decision on a resolution that already deposited would leave a precedent
        # with no matching review.
        assert client.post("/approvals/res_0001",
                           json={"human_action": "confirmed"}).status_code == 200
        second = client.post("/approvals/res_0001", json={"human_action": "rejected"})
        assert second.status_code == 409
        assert "already confirmed" in second.json()["detail"]

    def test_an_unknown_resolution_is_a_404(self, client):
        assert client.post("/approvals/res_nope",
                           json={"human_action": "confirmed"}).status_code == 404
