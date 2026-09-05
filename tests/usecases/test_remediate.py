"""Bounded remediation. No network — the refund client is a stub with a scripted outcome.

The properties under test are the ones that decide whether money leaves: that confirming an
explanation does not authorise a payment, that the ceiling is read from storage rather than
memory, that a reservation is written before the call and held when the outcome is unknown,
and that the stopping rule stops when driven to exhaustion rather than merely when read.
"""

import pytest

from precedent.adapters.razorpay.refunds import (
    RefundConflict,
    RefundRejected,
    RefundResult,
    RefundUnavailable,
)
from precedent.adapters.storage.records import (
    ExceptionRecord,
    PaymentRecord,
    ResolutionRecord,
)
from precedent.adapters.storage.repositories import (
    AuditLogRepository,
    ExceptionsRepository,
    PaymentsRepository,
    RemediationsRepository,
    ResolutionsRepository,
)
from precedent.domain.remediation import RemediationCeiling
from precedent.usecases.remediate import (
    RemediationRefused,
    ceiling_status,
    execute_remediation,
    propose_remediation,
)

NOW = "2026-09-05T10:00:00+00:00"
DUPLICATE = "duplicate_payment_rejected"


class StubRefunds:
    """Records what it was asked to do; raises whatever it was told to."""

    def __init__(self, raises=None, refund_id="rfnd_STUB01"):
        self.calls = []
        self._raises = raises
        self._refund_id = refund_id

    def create_refund(self, payment_id, amount_paise, idempotency_key, notes=None):
        self.calls.append((payment_id, amount_paise, idempotency_key))
        if self._raises:
            raise self._raises
        return RefundResult(
            refund_id=self._refund_id, payment_id=payment_id,
            amount_paise=amount_paise, status="processed", http_status=200,
        )


def seed(conn, *, human_action="confirmed", kind=DUPLICATE, amounts=(150_00, 150_00),
         corrected_to=None):
    """`corrected_to` populates `corrected_payload`, which the schema sets only on
    corrections — so a confirmed resolution here genuinely exercises the fallback to the
    exception's own kind, as it would in production."""
    payments = PaymentsRepository(conn)
    refs = []
    for i, amount in enumerate(amounts):
        pid = f"pay_dup_{i}"
        payments.insert(PaymentRecord(
            payment_id=pid, order_id="order_dup", amount_paise=amount,
            fee_paise=0, tax_paise=0, captured_at=f"2026-09-0{i + 1}T10:00:00+00:00",
            status="captured", source="synthetic",
        ))
        refs.append(pid)
    ExceptionsRepository(conn).insert(ExceptionRecord(
        exception_id="exc_dup", batch_id="batch_1", kind=kind, member_refs=refs,
        detected_at=NOW, status="open", correlation_id="corr_dup",
    ))
    ResolutionsRepository(conn).insert(ResolutionRecord(
        resolution_id="res_dup", exception_id="exc_dup", proposed_by="agent",
        confidence=0.95, rationale="second capture of the same order",
        cited_precedents=[], verified=True,
    ))
    if human_action:
        ResolutionsRepository(conn).record_human_action(
            resolution_id="res_dup", human_action=human_action,
            corrected_payload=(
                {"reason_code": corrected_to, "note": ""} if corrected_to else None
            ),
            resolved_at=NOW,
        )
    return refs


class TestWhatMayBeProposed:
    def test_a_duplicate_payment_proposes_a_refund_of_the_later_capture(self, conn):
        # The earlier capture is the one that settled the invoice. Refunding it instead
        # leaves the invoice open and the customer paid — same arithmetic, wrong operation.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        assert proposal.payment_id == "pay_dup_1"
        assert proposal.amount_paise == 150_00

    def test_an_unreviewed_resolution_cannot_even_be_proposed(self, conn):
        seed(conn, human_action=None)
        with pytest.raises(RemediationRefused, match="unreviewed"):
            propose_remediation(conn, "res_dup")

    def test_a_rejected_resolution_cannot_be_proposed(self, conn):
        seed(conn, human_action="rejected")
        with pytest.raises(RemediationRefused):
            propose_remediation(conn, "res_dup")

    def test_every_other_reason_code_proposes_nothing(self, conn):
        # Narrow by construction: the rest of the vocabulary describes bookkeeping outcomes
        # where the right action is to record what happened, not to send money.
        for kind in ("tds_short_payment", "negotiated_rebate", "split_payment",
                     "advance_adjusted", "refund_netted"):
            conn.execute("DELETE FROM resolutions")
            conn.execute("DELETE FROM exceptions")
            conn.execute("DELETE FROM payments")
            seed(conn, kind=kind)
            assert propose_remediation(conn, "res_dup") is None, kind

    def test_a_duplicate_with_only_one_member_refuses_rather_than_guesses(self, conn):
        seed(conn, amounts=(150_00,))
        with pytest.raises(RemediationRefused, match="refusing to guess"):
            propose_remediation(conn, "res_dup")

    def test_a_confirmation_falls_back_to_the_exceptions_own_kind(self, conn):
        # `corrected_payload` is null on a confirmation, so the code that governs has to
        # come from the exception. Asserted because an earlier version of this test seeded
        # a payload on confirmations too, which made this path unreachable.
        seed(conn, human_action="confirmed")
        assert conn.execute(
            "SELECT corrected_payload FROM resolutions WHERE resolution_id = 'res_dup'"
        ).fetchone()[0] is None
        assert propose_remediation(conn, "res_dup").payment_id == "pay_dup_1"

    def test_a_correction_into_duplicate_creates_a_refund_that_was_not_there(self, conn):
        # The reviewer overruled the agent. The refund follows their answer, not its.
        seed(conn, human_action="corrected", kind="tds_short_payment",
             corrected_to=DUPLICATE)
        assert propose_remediation(conn, "res_dup") is not None

    def test_a_correction_out_of_duplicate_cancels_the_refund(self, conn):
        # The direction that matters more: the agent wanted to send money and the human
        # said no. Their code has to win, or the override is decorative.
        seed(conn, human_action="corrected", kind=DUPLICATE,
             corrected_to="tds_short_payment")
        assert propose_remediation(conn, "res_dup") is None

    def test_proposing_sends_nothing_and_writes_nothing(self, conn):
        # Building the screen must not be able to move money by accident.
        seed(conn)
        propose_remediation(conn, "res_dup")
        assert RemediationsRepository(conn).usage() == (0, 0)


class TestTheSecondGate:
    """A confirmed resolution buys nothing here. That separation is the point of Ring 5.2."""

    def test_a_confirmed_resolution_alone_does_not_fire_a_refund(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        stub = StubRefunds()
        outcome = execute_remediation(conn, proposal, approval="refused",
                                      refund_client=stub, now=lambda: NOW)
        assert not outcome.executed
        assert stub.calls == [], "a refused remediation must not reach the API at all"
        assert "refused at the remediation gate" in outcome.reason

    def test_a_refusal_is_recorded_rather_than_dropped(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        execute_remediation(conn, proposal, "refused", StubRefunds(), now=lambda: NOW)
        rows = RemediationsRepository(conn).list_by_resolution("res_dup")
        assert [r.status for r in rows] == ["refused"]

    def test_a_refusal_does_not_consume_ceiling(self, conn):
        # It would be a strange budget where saying no cost as much as saying yes.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        execute_remediation(conn, proposal, "refused", StubRefunds(), now=lambda: NOW)
        assert RemediationsRepository(conn).usage() == (0, 0)

    def test_approval_fires_the_refund_and_records_it(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        stub = StubRefunds()
        outcome = execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        assert outcome.executed
        assert outcome.refund_id == "rfnd_STUB01"
        assert stub.calls == [("pay_dup_1", 150_00, proposal.idempotency_key)]
        row = RemediationsRepository(conn).get(outcome.remediation_id)
        assert row.status == "executed" and row.refund_id == "rfnd_STUB01"


class TestIdempotency:
    def test_a_second_attempt_at_the_same_intent_returns_the_original_refund(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        stub = StubRefunds()
        first = execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        second = execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        assert second.replayed
        assert second.refund_id == first.refund_id
        assert len(stub.calls) == 1, "the second attempt must not reach the API"

    def test_a_replay_does_not_spend_the_ceiling_twice(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        stub = StubRefunds()
        execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        assert RemediationsRepository(conn).usage() == (1, 150_00)

    def test_a_refusal_does_not_burn_the_key_for_a_later_approval(self, conn):
        # An operator declines, then reconsiders — or widens the ceiling and retries. A
        # plain UNIQUE column on idempotency_key blocked that forever, which running
        # `scripts/fire_remediation.py` found and reading the schema had not.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        execute_remediation(conn, proposal, "refused", StubRefunds(), now=lambda: NOW)
        stub = StubRefunds()
        outcome = execute_remediation(conn, proposal, "approved", stub, now=lambda: NOW)
        assert outcome.executed
        assert len(stub.calls) == 1

    def test_a_rejected_attempt_does_not_burn_the_key_either(self, conn):
        # The API said the request was invalid, so nothing moved and nothing is claimed.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        with pytest.raises(RefundRejected):
            execute_remediation(conn, proposal, "approved",
                                StubRefunds(raises=RefundRejected("400")), now=lambda: NOW)
        outcome = execute_remediation(conn, proposal, "approved", StubRefunds(),
                                      now=lambda: NOW)
        assert outcome.executed

    def test_an_unresolved_earlier_attempt_blocks_rather_than_retries(self, conn):
        # An 'approved' row with no refund id is a refund whose outcome nobody knows.
        # Sending another under the same key would be the one thing guaranteed to be wrong.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        with pytest.raises(RefundUnavailable):
            execute_remediation(conn, proposal, "approved",
                                StubRefunds(raises=RefundUnavailable("timeout")),
                                now=lambda: NOW)
        with pytest.raises(RemediationRefused, match="already exists"):
            execute_remediation(conn, proposal, "approved", StubRefunds(), now=lambda: NOW)


class TestReservationSurvivesTheCall:
    def test_an_unknown_outcome_keeps_holding_its_amount_against_the_ceiling(self, conn):
        # Releasing budget on a maybe is how the same money gets spent twice.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        with pytest.raises(RefundUnavailable):
            execute_remediation(conn, proposal, "approved",
                                StubRefunds(raises=RefundUnavailable("504")),
                                now=lambda: NOW)
        assert RemediationsRepository(conn).usage() == (1, 150_00)
        rows = RemediationsRepository(conn).list_by_resolution("res_dup")
        assert rows[0].status == "approved" and rows[0].refund_id is None

    def test_a_conflict_also_holds_the_reservation(self, conn):
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        with pytest.raises(RefundConflict):
            execute_remediation(conn, proposal, "approved",
                                StubRefunds(raises=RefundConflict("409")), now=lambda: NOW)
        assert RemediationsRepository(conn).usage() == (1, 150_00)

    def test_an_outright_rejection_releases_the_reservation(self, conn):
        # Here the API said no in so many words: nothing moved, so the budget goes back.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        with pytest.raises(RefundRejected):
            execute_remediation(conn, proposal, "approved",
                                StubRefunds(raises=RefundRejected("400")), now=lambda: NOW)
        assert RemediationsRepository(conn).usage() == (0, 0)

    def test_every_terminal_path_leaves_an_audit_row(self, conn):
        # NFR-5: every state change reconstructable from audit_log alone. A refund with no
        # audit row is money that moved for no recorded reason.
        seed(conn)
        proposal = propose_remediation(conn, "res_dup")
        execute_remediation(conn, proposal, "approved", StubRefunds(), now=lambda: NOW)
        stages = [r.stage for r in AuditLogRepository(conn).list_by_correlation_id("corr_dup")]
        assert "acted" in stages


class TestTheStoppingRuleDrivenToExhaustion:
    """Ring 5.2 asks for the ceiling to be *driven* to exhaustion, not reviewed."""

    def _proposal_for(self, conn, index, amount):
        pid = f"pay_x_{index}"
        PaymentsRepository(conn).insert(PaymentRecord(
            payment_id=pid, order_id=f"order_x_{index}", amount_paise=amount,
            fee_paise=0, tax_paise=0, captured_at=NOW, status="captured",
            source="synthetic",
        ))
        PaymentsRepository(conn).insert(PaymentRecord(
            payment_id=f"{pid}_b", order_id=f"order_x_{index}", amount_paise=amount,
            fee_paise=0, tax_paise=0, captured_at="2026-09-06T10:00:00+00:00",
            status="captured", source="synthetic",
        ))
        ExceptionsRepository(conn).insert(ExceptionRecord(
            exception_id=f"exc_x_{index}", batch_id="batch_x", kind=DUPLICATE,
            member_refs=[pid, f"{pid}_b"], detected_at=NOW, status="open",
            correlation_id=f"corr_x_{index}",
        ))
        ResolutionsRepository(conn).insert(ResolutionRecord(
            resolution_id=f"res_x_{index}", exception_id=f"exc_x_{index}",
            proposed_by="agent", confidence=0.95, rationale="dup",
            cited_precedents=[], verified=True,
        ))
        ResolutionsRepository(conn).record_human_action(
            resolution_id=f"res_x_{index}", human_action="confirmed",
            corrected_payload={"reason_code": DUPLICATE, "note": ""}, resolved_at=NOW,
        )
        return propose_remediation(conn, f"res_x_{index}")

    def test_the_ceiling_stops_the_run_and_nothing_goes_over(self, conn):
        ceiling = RemediationCeiling(max_refunds=5, max_total_paise=500_00,
                                     max_single_paise=200_00)
        stub = StubRefunds()
        executed = []
        for i in range(10):
            proposal = self._proposal_for(conn, i, 150_00)
            outcome = execute_remediation(conn, proposal, "approved", stub, ceiling,
                                          now=lambda: NOW)
            if not outcome.executed:
                break
            executed.append(outcome)
        else:  # pragma: no cover - only reached if the rule never stops
            pytest.fail("the ceiling never refused")

        assert len(executed) == 3, "₹150 x 3 = ₹450; a fourth would cross ₹500"
        assert len(stub.calls) == 3, "the refused attempt must not reach the API"
        count, total = RemediationsRepository(conn).usage()
        assert total <= ceiling.max_total_paise and count <= ceiling.max_refunds

    def test_the_count_limit_can_bind_before_the_total(self, conn):
        ceiling = RemediationCeiling(max_refunds=2, max_total_paise=500_00,
                                     max_single_paise=200_00)
        stub = StubRefunds()
        results = [
            execute_remediation(conn, self._proposal_for(conn, i, 100_00), "approved",
                                stub, ceiling, now=lambda: NOW)
            for i in range(3)
        ]
        assert [r.executed for r in results] == [True, True, False]
        assert "2 of 2 used" in results[-1].reason

    def test_the_per_call_cap_stops_a_single_oversized_refund(self, conn):
        ceiling = RemediationCeiling(max_refunds=5, max_total_paise=500_00,
                                     max_single_paise=200_00)
        stub = StubRefunds()
        outcome = execute_remediation(conn, self._proposal_for(conn, 0, 400_00),
                                      "approved", stub, ceiling, now=lambda: NOW)
        assert not outcome.executed
        assert "single-refund cap" in outcome.reason
        assert stub.calls == []

    def test_the_ceiling_is_recomputed_from_storage_not_carried_in_memory(self, conn):
        # The property that makes the limit survive a restart: a fresh call with no shared
        # state still sees what earlier ones spent, because it reads the table.
        ceiling = RemediationCeiling(max_refunds=2, max_total_paise=500_00,
                                     max_single_paise=200_00)
        execute_remediation(conn, self._proposal_for(conn, 0, 100_00), "approved",
                            StubRefunds(), ceiling, now=lambda: NOW)
        execute_remediation(conn, self._proposal_for(conn, 1, 100_00), "approved",
                            StubRefunds(), ceiling, now=lambda: NOW)
        # A brand-new client and a brand-new proposal — nothing carried over but the rows.
        outcome = execute_remediation(conn, self._proposal_for(conn, 2, 100_00),
                                      "approved", StubRefunds(), ceiling, now=lambda: NOW)
        assert not outcome.executed


class TestCeilingStatus:
    def test_it_reports_what_the_gate_has_to_show(self, conn):
        status = ceiling_status(conn, RemediationCeiling(max_refunds=3,
                                                         max_total_paise=500_00,
                                                         max_single_paise=250_00))
        assert status["remaining_refunds"] == 3
        assert status["remaining_paise"] == 500_00
        assert not status["exhausted"]

    def test_exhaustion_is_reported_as_such(self, conn):
        ceiling = RemediationCeiling(max_refunds=1, max_total_paise=500_00,
                                     max_single_paise=250_00)
        seed(conn)
        execute_remediation(conn, propose_remediation(conn, "res_dup"), "approved",
                            StubRefunds(), ceiling, now=lambda: NOW)
        assert ceiling_status(conn, ceiling)["exhausted"]
