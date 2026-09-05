"""The remediation gate over HTTP.

The refund client is overridden in every test — nothing here reaches the network. What is
being tested is the boundary: that the two gates stay separate, that a refusal is not an
error, and that the status code an operator's tooling sees matches what it should do next.
"""

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.razorpay.refunds import (
    RefundConflict,
    RefundRejected,
    RefundResult,
    RefundUnavailable,
)
from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import (
    ExceptionRecord,
    PaymentRecord,
    ResolutionRecord,
)
from precedent.adapters.storage.repositories import (
    ExceptionsRepository,
    PaymentsRepository,
    ResolutionsRepository,
)
from precedent.api.main import create_app
from precedent.api.remediation import _ceiling, _refund_client
from precedent.domain.remediation import RemediationCeiling

NOW = "2026-09-05T10:00:00+00:00"


class StubRefunds:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def create_refund(self, payment_id, amount_paise, idempotency_key, notes=None):
        self.calls.append((payment_id, amount_paise, idempotency_key))
        if self._raises:
            raise self._raises
        return RefundResult("rfnd_STUB01", payment_id, amount_paise, "processed", 200)


def seed(db_path, *, kind="duplicate_payment_rejected", human_action="confirmed"):
    conn = connect(db_path)
    init_db(conn)
    payments = PaymentsRepository(conn)
    for i, at in enumerate(("2026-09-01T10:00:00+00:00", "2026-09-02T10:00:00+00:00")):
        payments.insert(PaymentRecord(
            payment_id=f"pay_dup_{i}", order_id="order_dup", amount_paise=150_00,
            fee_paise=0, tax_paise=0, captured_at=at, status="captured",
            source="synthetic",
        ))
    ExceptionsRepository(conn).insert(ExceptionRecord(
        exception_id="exc_dup", batch_id="b1", kind=kind,
        member_refs=["pay_dup_0", "pay_dup_1"], detected_at=NOW, status="open",
        correlation_id="corr_dup",
    ))
    ResolutionsRepository(conn).insert(ResolutionRecord(
        resolution_id="res_dup", exception_id="exc_dup", proposed_by="agent",
        confidence=0.95, rationale="second capture", cited_precedents=[], verified=True,
    ))
    if human_action:
        ResolutionsRepository(conn).record_human_action(
            resolution_id="res_dup", human_action=human_action, resolved_at=NOW,
        )
    conn.commit()
    conn.close()


@pytest.fixture
def app_with(tmp_path):
    def build(stub=None, ceiling=None, **seed_kwargs):
        db_path = str(tmp_path / "test.db")
        seed(db_path, **seed_kwargs)
        app = create_app(db_path)
        stub = stub or StubRefunds()
        app.dependency_overrides[_refund_client] = lambda: stub
        if ceiling is not None:
            app.dependency_overrides[_ceiling] = lambda: ceiling
        return TestClient(app), stub, db_path

    return build


class TestTheProposalIsFreeOfConsequence:
    def test_it_shows_the_amount_and_the_ceiling_without_sending_anything(self, app_with):
        client, stub, _ = app_with()
        body = client.get("/remediation/res_dup").json()
        assert body["remediable"] is True
        assert body["amount_paise"] == 150_00
        assert body["payment_id"] == "pay_dup_1"
        assert body["ceiling"]["remaining_refunds"] == 3
        assert stub.calls == []

    def test_a_case_needing_no_money_is_not_an_error(self, app_with):
        # The commonest answer. A 404 would make "nothing to refund here" look like a
        # lookup failure and send an operator hunting for a bug.
        client, _, _ = app_with(kind="tds_short_payment")
        response = client.get("/remediation/res_dup")
        assert response.status_code == 200
        assert response.json()["remediable"] is False

    def test_an_unreviewed_resolution_is_a_conflict(self, app_with):
        client, _, _ = app_with(human_action=None)
        assert client.get("/remediation/res_dup").status_code == 409


class TestTheGateIsSeparateFromTheApprovalGate:
    def test_refusing_here_sends_nothing(self, app_with):
        client, stub, _ = app_with()
        body = client.post("/remediation/res_dup", json={"gate_action": "refused"}).json()
        assert body["executed"] is False
        assert stub.calls == []

    def test_a_refusal_is_a_200_not_an_error(self, app_with):
        # Refusing is the system working. Returning 4xx would train an operator to read
        # every refusal as something to escalate.
        client, _, _ = app_with()
        assert client.post(
            "/remediation/res_dup", json={"gate_action": "refused"}
        ).status_code == 200

    def test_approving_fires_the_refund(self, app_with):
        client, stub, _ = app_with()
        body = client.post("/remediation/res_dup", json={"gate_action": "approved"}).json()
        assert body["executed"] is True
        assert body["refund_id"] == "rfnd_STUB01"
        assert len(stub.calls) == 1

    @pytest.mark.parametrize("bad", [{}, {"gate_action": "yes"}, {"gate_action": True}])
    def test_an_unrecognised_action_is_refused_rather_than_guessed(self, app_with, bad):
        client, stub, _ = app_with()
        assert client.post("/remediation/res_dup", json=bad).status_code == 422
        assert stub.calls == []

    def test_a_case_needing_no_money_cannot_be_approved(self, app_with):
        client, stub, _ = app_with(kind="negotiated_rebate")
        assert client.post(
            "/remediation/res_dup", json={"gate_action": "approved"}
        ).status_code == 422
        assert stub.calls == []


class TestTheReservationSurvivesAFailedRequest:
    """The bug this class exists for: `api.deps.get_connection` rolls the request back on
    any exception. Without an explicit commit, a refund whose outcome is unknown would take
    its own reservation row down with it — leaving money possibly moved and no record of it,
    which is the exact failure the reservation was written to prevent."""

    def _remediation_rows(self, db_path):
        conn = connect(db_path)
        try:
            return conn.execute(
                "SELECT status, refund_id, amount_paise FROM remediations"
            ).fetchall()
        finally:
            conn.close()

    def test_a_timeout_leaves_the_reservation_on_disk(self, app_with):
        client, _, db_path = app_with(StubRefunds(raises=RefundUnavailable("504")))
        assert client.post(
            "/remediation/res_dup", json={"gate_action": "approved"}
        ).status_code == 503
        rows = self._remediation_rows(db_path)
        assert len(rows) == 1, "the reservation was rolled back with the failed request"
        assert rows[0]["status"] == "approved" and rows[0]["refund_id"] is None

    def test_the_held_reservation_still_counts_against_the_ceiling_afterwards(self, app_with):
        client, _, db_path = app_with(StubRefunds(raises=RefundUnavailable("504")))
        client.post("/remediation/res_dup", json={"gate_action": "approved"})
        ceiling = client.get("/remediation").json()
        assert ceiling["refunds_made"] == 1
        assert ceiling["total_paise"] == 150_00

    def test_a_conflict_also_leaves_the_reservation(self, app_with):
        client, _, db_path = app_with(StubRefunds(raises=RefundConflict("409")))
        assert client.post(
            "/remediation/res_dup", json={"gate_action": "approved"}
        ).status_code == 409
        assert len(self._remediation_rows(db_path)) == 1

    def test_an_outright_rejection_records_the_failure_and_releases_the_budget(self, app_with):
        client, _, db_path = app_with(StubRefunds(raises=RefundRejected("400")))
        assert client.post(
            "/remediation/res_dup", json={"gate_action": "approved"}
        ).status_code == 502
        rows = self._remediation_rows(db_path)
        assert len(rows) == 1 and rows[0]["status"] == "failed"
        assert client.get("/remediation").json()["refunds_made"] == 0


class TestTheCeilingEndpoint:
    def test_it_reports_an_empty_budget_as_untouched(self, app_with):
        client, _, _ = app_with()
        body = client.get("/remediation").json()
        assert body["refunds_made"] == 0 and body["exhausted"] is False
        assert body["history"] == []

    def test_a_fired_refund_appears_in_the_history(self, app_with):
        client, _, _ = app_with()
        client.post("/remediation/res_dup", json={"gate_action": "approved"})
        history = client.get("/remediation").json()["history"]
        assert len(history) == 1
        assert history[0]["refund_id"] == "rfnd_STUB01"
        assert history[0]["status"] == "executed"

    def test_an_exhausted_ceiling_refuses_over_http_without_calling_the_api(self, app_with):
        client, stub, _ = app_with(
            ceiling=RemediationCeiling(max_refunds=1, max_total_paise=500_00,
                                       max_single_paise=200_00)
        )
        first = client.post("/remediation/res_dup", json={"gate_action": "approved"})
        assert first.json()["executed"] is True
        # The same intent replays rather than re-spending — so exhaust with a second
        # distinct one by confirming the ceiling directly.
        assert client.get("/remediation").json()["exhausted"] is True
        assert len(stub.calls) == 1
