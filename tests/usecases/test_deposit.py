"""The deposit loop. No network — an authored precedent comes from a scripted model.

The rules under test are policy, not plumbing: what may deposit, what the deposited answer is
allowed to be, and what happens when authoring fails. Each is a way the corpus could be
poisoned, which is the failure mode `docs/ARCHITECTURE.md` names as the most serious.
"""

import json

import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.adapters.storage.records import ExceptionRecord, ResolutionRecord
from precedent.adapters.storage.repositories import (
    AuditLogRepository,
    ExceptionsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.usecases.deposit import (
    DepositRefused,
    author_precedent,
    deposit_precedent,
    load_deposit_prompt,
    next_corpus_version,
    record_and_deposit,
)
from tests.domain.test_case import credit, ledger, payment

CASE = ReconciliationCase("case_1", [payment()], [credit()], [ledger()])
NOW = "2026-09-02T10:00:00+00:00"


def authored(**overrides):
    body = {
        "situation": (
            "Payments from Konark Logistics arrive short of the invoice by a proportion "
            "matching no statutory withholding band, with no refund or fee explaining it."
        ),
        "resolution": (
            "Konark Logistics settles under a negotiated rebate agreed in their supply "
            "contract. Reconstruct the invoice from the receipt, confirm it reproduces the "
            "invoiced figure, and close the invoice in full."
        ),
        "reason_code": "negotiated_rebate",
        "entities": ["Konark Logistics"],
        "amount_signature": "rebate_konark",
        "confidence_at_deposit": 0.93,
    }
    body.update(overrides)
    return json.dumps(body)


@pytest.fixture
def seeded(conn):
    # The resolution's parent exception, because the FK is real and enforced.
    ExceptionsRepository(conn).insert(
        ExceptionRecord(
            exception_id="exc_0001", batch_id="batch_1", kind="negotiated_rebate",
            member_refs=["case_1"], detected_at=NOW, status="open",
            correlation_id="corr_0001",
        )
    )
    ResolutionsRepository(conn).insert(
        ResolutionRecord(
            resolution_id="res_0001", exception_id="exc_0001",
            proposed_by="agent", confidence=0.9,
            rationale="matched against the customer's negotiated terms",
            cited_precedents=[], verified=True,
        )
    )
    conn.commit()
    return conn


class TestPrompt:
    def test_the_deposit_prompt_splits_into_system_and_user(self):
        system, user = load_deposit_prompt()
        assert "precedent" in system.lower()
        for field in ("{case_summary}", "{resolution_narrative}", "{reason_code}",
                      "{human_action}", "{correction_note}"):
            assert field in user, field

    def test_it_tells_the_author_to_name_a_counterparty(self):
        # Learned the hard way: written generically the same knowledge resolved 1 case in 5;
        # naming the customer, 4 in 5. The query carries a name, so the precedent must too.
        system, _ = load_deposit_prompt()
        assert "Name the counterparty" in system

    def test_it_forbids_depositing_an_escalation_code(self):
        system, _ = load_deposit_prompt()
        assert "Never deposit an escalation code" in system


class TestAuthorPrecedent:
    def test_parses_an_authored_precedent(self):
        precedent = author_precedent(
            ScriptedLLM([authored()]), CASE, ReasonCode.NEGOTIATED_REBATE,
            "rebate applied", "confirmed",
        )
        assert precedent.reason_code is ReasonCode.NEGOTIATED_REBATE
        assert precedent.entities == ["Konark Logistics"]

    def test_the_reason_code_comes_from_the_resolution_not_the_model(self):
        # The human already decided what this case was. Letting the deposit re-decide would
        # let a precedent disagree with the resolution it claims to derive from.
        precedent = author_precedent(
            ScriptedLLM([authored(reason_code="exact_match")]), CASE,
            ReasonCode.NEGOTIATED_REBATE, "rebate applied", "confirmed",
        )
        assert precedent.reason_code is ReasonCode.NEGOTIATED_REBATE

    def test_the_case_and_the_resolution_both_reach_the_prompt(self):
        llm = ScriptedLLM([authored()])
        author_precedent(llm, CASE, ReasonCode.NEGOTIATED_REBATE,
                         "the customer applied their agreed rebate", "corrected",
                         correction_note="was called TDS; it is a rebate")
        sent = llm.last_user_prompt
        assert "the customer applied their agreed rebate" in sent
        assert "was called TDS; it is a rebate" in sent
        assert "corrected" in sent
        assert "{case_summary}" not in sent

    def test_an_unusable_precedent_raises_rather_than_being_stored(self):
        # Precedent's own validators are the gate; a situation naming a payment id can never
        # retrieve again, so it must not reach the corpus.
        with pytest.raises(ValueError):
            author_precedent(
                ScriptedLLM([authored(situation="Payment pay_TWTMWlUBiCYWtU was short by 2%.")]),
                CASE, ReasonCode.NEGOTIATED_REBATE, "x", "confirmed",
            )


class TestWhatMayDeposit:
    def test_a_confirmed_resolution_deposits(self, seeded):
        outcome = deposit_precedent(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "rebate applied", "confirmed", now=NOW,
        )
        assert outcome.deposited and outcome.precedent_id

    def test_a_corrected_resolution_deposits_too(self, seeded):
        # Spec §7: a corrected resolution is the *higher*-value precedent, because it
        # encodes a case the system got wrong.
        outcome = deposit_precedent(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "rebate applied", "corrected", now=NOW,
        )
        assert outcome.deposited

    def test_a_rejected_resolution_is_refused(self, seeded):
        with pytest.raises(DepositRefused, match="does not deposit"):
            deposit_precedent(
                seeded, ScriptedLLM([authored()]), CASE, "res_0001",
                ReasonCode.NEGOTIATED_REBATE, "x", "rejected", now=NOW,
            )

    def test_an_unreviewed_resolution_is_refused(self, seeded):
        # A corpus that deposits its own unreviewed output amplifies its own errors.
        with pytest.raises(DepositRefused):
            deposit_precedent(
                seeded, ScriptedLLM([authored()]), CASE, "res_0001",
                ReasonCode.NEGOTIATED_REBATE, "x", "", now=NOW,
            )

    def test_refusal_is_not_confused_with_failure(self, seeded):
        # Refusing to deposit a rejected resolution is correct behaviour; returning it as a
        # failure would hide a policy violation inside a retryable-looking error.
        with pytest.raises(DepositRefused):
            deposit_precedent(
                seeded, ScriptedLLM([LLMUnavailable("down")]), CASE, "res_0001",
                ReasonCode.NEGOTIATED_REBATE, "x", "rejected", now=NOW,
            )


class TestProvenanceAndVersioning:
    def test_the_deposit_records_which_resolution_it_came_from(self, seeded):
        outcome = deposit_precedent(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
        )
        stored = PrecedentsRepository(seeded).get(outcome.precedent_id)
        assert stored.derived_from_resolution == "res_0001"
        assert stored.deposited_at == NOW

    def test_seeds_sit_at_version_zero_so_the_first_deposit_is_version_one(self, seeded):
        from precedent.corpus.seed import seed_precedent_records

        repo = PrecedentsRepository(seeded)
        for record in seed_precedent_records():
            repo.insert(record)
        assert next_corpus_version(seeded) == 1

    def test_versions_increment_so_a_snapshot_means_after_n_deposits(self, seeded):
        versions = []
        for _ in range(3):
            outcome = deposit_precedent(
                seeded, ScriptedLLM([authored()]), CASE, "res_0001",
                ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
            )
            versions.append(outcome.corpus_version)
        assert versions == [1, 2, 3]

    def test_a_snapshot_returns_the_corpus_as_it_was(self, seeded):
        from precedent.corpus.seed import seed_precedent_records

        repo = PrecedentsRepository(seeded)
        for record in seed_precedent_records():
            repo.insert(record)
        for _ in range(3):
            deposit_precedent(
                seeded, ScriptedLLM([authored()]), CASE, "res_0001",
                ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
            )
        assert len(repo.list_as_of_corpus_version(0)) == 42
        assert len(repo.list_as_of_corpus_version(2)) == 44


class TestFailureHandling:
    def test_an_unavailable_model_does_not_lose_the_human_decision(self, seeded):
        # The human's decision stands whether or not a precedent could be authored from it.
        outcome = deposit_precedent(
            seeded, ScriptedLLM([LLMUnavailable("down")]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
        )
        assert not outcome.deposited
        assert "could not author" in outcome.reason

    def test_a_failed_deposit_is_still_audited(self, seeded):
        deposit_precedent(
            seeded, ScriptedLLM(["not json at all"]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
        )
        rows = AuditLogRepository(seeded).list_by_correlation_id("res_0001")
        assert any("deposit failed" in (r.reason or "") for r in rows)

    def test_an_unusable_precedent_is_not_stored(self, seeded):
        outcome = deposit_precedent(
            seeded, ScriptedLLM([authored(confidence_at_deposit=5.0)]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "confirmed", now=NOW,
        )
        assert not outcome.deposited
        assert PrecedentsRepository(seeded).list_as_of_corpus_version(99) == []


class TestAtomicity:
    def test_the_human_action_and_the_precedent_land_together(self, seeded):
        outcome = record_and_deposit(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "rebate applied", "confirmed", now=NOW,
        )
        assert outcome.deposited
        resolution = ResolutionsRepository(seeded).get("res_0001")
        assert resolution.human_action == "confirmed"
        assert PrecedentsRepository(seeded).get(outcome.precedent_id) is not None

    def test_a_rejection_records_the_action_and_deposits_nothing(self, seeded):
        outcome = record_and_deposit(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "rejected", now=NOW,
        )
        assert not outcome.deposited
        assert ResolutionsRepository(seeded).get("res_0001").human_action == "rejected"
        assert PrecedentsRepository(seeded).list_as_of_corpus_version(99) == []

    def test_a_correction_stores_the_corrected_payload(self, seeded):
        record_and_deposit(
            seeded, ScriptedLLM([authored()]), CASE, "res_0001",
            ReasonCode.NEGOTIATED_REBATE, "x", "corrected",
            corrected_payload={"reason_code": "negotiated_rebate"}, now=NOW,
        )
        assert ResolutionsRepository(seeded).get("res_0001").corrected_payload is not None
