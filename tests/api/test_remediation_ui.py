"""The second gate on screen.

`product_design.md` §3.4 asks for one thing this file mostly checks: that the ceiling is
*visible*, not merely enforced. An operator approving a refund without knowing what fraction
of the budget it consumes is approving in the dark, and a limit enforced only server-side is
one they cannot reason about before they click.

The other half is that a blocked refund offers no button. Rendering Approve and then refusing
the POST would be an interface arguing against itself — and would teach the operator that the
ceiling is a suggestion.
"""

import re

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.razorpay.refunds import RefundResult, RefundUnavailable
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
from precedent.api import remediation_ui
from precedent.api.main import create_app
from precedent.config import ConfigError

NOW = "2026-09-05T10:00:00+00:00"


def flat(body: str) -> str:
    """Copy assertions read the sentence, not the line breaks the template wraps at."""
    return re.sub(r"\s+", " ", body)


class StubRefunds:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def create_refund(self, payment_id, amount_paise, idempotency_key, notes=None):
        self.calls.append((payment_id, amount_paise, idempotency_key))
        if self._raises:
            raise self._raises
        return RefundResult("rfnd_STUB01", payment_id, amount_paise, "processed", 200)


def seed(db_path, *, kind="duplicate_payment_rejected", human_action="confirmed",
         amount_paise=150_00):
    conn = connect(db_path)
    init_db(conn)
    payments = PaymentsRepository(conn)
    for i, at in enumerate(("2026-09-01T10:00:00+00:00", "2026-09-02T10:00:00+00:00")):
        payments.insert(PaymentRecord(
            payment_id=f"pay_dup_{i}", order_id="order_dup", amount_paise=amount_paise,
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
def app_with(tmp_path, monkeypatch):
    def build(stub=None, **seed_kwargs):
        db_path = str(tmp_path / "ui.db")
        seed(db_path, **seed_kwargs)
        # The client is built inside the handler rather than injected, so that a missing
        # credential renders a page instead of a 500 from dependency resolution. That makes
        # monkeypatch the seam rather than dependency_overrides.
        monkeypatch.setattr(remediation_ui, "_refund_client", lambda: stub or StubRefunds())
        return TestClient(create_app(db_path)), (stub or StubRefunds()), db_path

    return build


class TestTheCeilingIsVisible:
    """§3.4 — 'bounded' has to be something the operator can see, not just something the
    server enforces."""

    def test_the_screen_shows_the_budget_and_what_is_left(self, app_with):
        client, _, _ = app_with()
        body = client.get("/refunds").text
        assert "What it may spend" in body
        assert "Budget" in body and "Left to spend" in body
        assert "500.00" in body          # max_total_paise
        assert "250.00" in body          # max_single_paise

    def test_the_case_screen_shows_it_before_the_button(self, app_with):
        client, _, _ = app_with()
        body = client.get("/exceptions/res_dup").text
        assert body.index("Left to spend") < body.index("Approve and send")

    def test_an_untouched_ledger_says_so_rather_than_showing_an_empty_table(self, app_with):
        client, _, _ = app_with()
        assert "No refund has been proposed yet" in client.get("/refunds").text


class TestABlockedRefundOffersNoButton:
    def test_over_the_per_call_cap_the_approve_control_is_absent(self, app_with):
        client, _, _ = app_with(amount_paise=400_00)
        body = client.get("/exceptions/res_dup").text
        assert 'value="approved"' not in body
        assert "Blocked by the ceiling" in body
        assert "per-refund cap" in body

    def test_it_says_why_rather_than_just_disabling(self, app_with):
        client, _, _ = app_with(amount_paise=400_00)
        assert "a limit that can be clicked past is not a limit" in \
            flat(client.get("/exceptions/res_dup").text)


class TestWhenNoMoneyNeedsToMove:
    def test_it_says_so_rather_than_rendering_nothing(self, app_with):
        # The commonest answer, and a real one. Rendering nothing would leave the reviewer
        # unable to tell it from a screen that forgot to ask.
        client, _, _ = app_with(kind="tds_short_payment")
        body = flat(client.get("/exceptions/res_dup").text)
        assert "Does money need to move?" in body
        assert "not a payment" in body

    def test_an_unreviewed_case_shows_no_money_gate_at_all(self, app_with):
        # The first gate has not been passed. Asking about a refund would put the second
        # authorisation in front of the first.
        client, _, _ = app_with(human_action=None)
        assert "Does money need to move?" not in client.get("/exceptions/res_dup").text


class TestTheGateItself:
    def test_refusing_records_it_and_sends_nothing(self, app_with):
        stub = StubRefunds()
        client, _, _ = app_with(stub=stub)
        assert client.post("/exceptions/res_dup/remediate", data={"gate_action": "refused"},
                           follow_redirects=False).status_code == 303
        assert stub.calls == []
        assert "refused" in client.get("/refunds").text

    def test_approving_sends_exactly_one_refund(self, app_with):
        stub = StubRefunds()
        client, _, _ = app_with(stub=stub)
        client.post("/exceptions/res_dup/remediate", data={"gate_action": "approved"})
        assert len(stub.calls) == 1
        assert stub.calls[0][:2] == ("pay_dup_1", 150_00)

    def test_an_unknown_action_is_refused(self, app_with):
        client, _, _ = app_with()
        assert "was not recorded" in client.post(
            "/exceptions/res_dup/remediate", data={"gate_action": "maybe"}
        ).text

    def test_a_missing_credential_explains_itself_instead_of_500ing(self, app_with,
                                                                   monkeypatch):
        client, _, _ = app_with()

        def unconfigured():
            raise ConfigError("Missing required environment variable(s): RAZORPAY_KEY_ID")

        monkeypatch.setattr(remediation_ui, "_refund_client", unconfigured)
        response = client.post("/exceptions/res_dup/remediate",
                               data={"gate_action": "approved"})
        assert response.status_code == 200
        assert "No Razorpay credentials" in response.text
        assert "deployment problem, not a decision about the case" in flat(response.text)


class TestAnUnknownOutcomeIsTheLoudestState:
    """A refund whose call did not come back may have moved money. It is not the same as one
    that was refused, and the copy must not let a reader treat them alike."""

    def test_it_says_money_may_have_moved(self, app_with):
        client, _, _ = app_with(stub=StubRefunds(raises=RefundUnavailable("timed out")))
        body = flat(client.post("/exceptions/res_dup/remediate",
                                data={"gate_action": "approved"}).text)
        assert "did not come back" in body
        assert "Money may have moved" in body
        assert "Check the Razorpay dashboard" in body

    def test_the_reservation_still_holds_against_the_ceiling(self, app_with):
        client, _, _ = app_with(stub=StubRefunds(raises=RefundUnavailable("timed out")))
        client.post("/exceptions/res_dup/remediate", data={"gate_action": "approved"})
        assert "unknown outcome" in flat(client.get("/refunds").text)
