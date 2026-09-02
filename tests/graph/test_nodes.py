"""Node-level tests. Every node is a plain function of state, so none of this builds a graph."""

import json

import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.graph.nodes import (
    classify_kind,
    escalate,
    make_finalize,
    make_investigate,
    make_propose_resolution,
    make_retrieve_precedents,
    route_after_verify,
    verify,
)
from precedent.graph.state import MAX_REVISIONS, MAX_TOOL_CALLS, initial_state
from tests.domain.test_case import credit, ledger, payment


def state_for(case, **overrides):
    state = initial_state(case, "corr_1")
    state.update(overrides)
    return state


def proposal_json(**overrides):
    body = {
        "reason_code": "exact_match", "confidence": 0.95,
        "rationale": "the credit equals the payment net of fee and tax",
        "cited_precedent_ids": [],
    }
    body.update(overrides)
    return json.dumps(body)


CLEAN = ReconciliationCase("c1", [payment()], [credit()], [ledger()])


class TestClassifyKind:
    """A retrieval hint, computed rather than asked of a model — naming the class before
    the evidence is gathered would anchor every node downstream."""

    def test_a_credit_with_no_payment_behind_it(self):
        case = ReconciliationCase("c1", [], [credit(amount=100_000)], [ledger()])
        assert classify_kind(state_for(case))["kind"] == "credit_without_payment"

    def test_two_payments_on_one_order(self):
        case = ReconciliationCase("c1", [payment(), payment()], [credit()], [ledger()])
        assert classify_kind(state_for(case))["kind"] == "repeated_payment_on_one_order"

    def test_many_payments_one_credit(self):
        payments = [payment("o1"), payment("o2"), payment("o3")]
        settled = sum(p.amount_paise - p.fee_paise - p.tax_paise for p in payments)
        case = ReconciliationCase("c1", payments, [credit(amount=settled)],
                                  [ledger("o1"), ledger("o2"), ledger("o3")])
        assert classify_kind(state_for(case))["kind"] == "many_payments_one_credit"

    def test_a_proportional_shortfall(self):
        paid = payment(amount=90_000, fee=1_800, tax=324)
        case = ReconciliationCase("c1", [paid], [credit(amount=90_000 - 2_124)],
                                  [ledger(expects=100_000)])
        assert classify_kind(state_for(case))["kind"] == "proportional_shortfall"

    def test_it_records_the_hint_in_the_trace(self):
        result = classify_kind(state_for(CLEAN))
        assert result["trace"][0]["node"] == "classify_kind"

    def test_it_never_returns_a_reason_code(self):
        # It is a hint, not an answer. Returning a ReasonCode would make it the decision.
        result = classify_kind(state_for(CLEAN))
        assert "reason_code" not in result
        assert result["kind"] not in {c.value for c in ReasonCode}


class TestRetrievePrecedents:
    def test_retrieves_k_precedents(self):
        from precedent.adapters.retrieval.bm25 import BM25Retriever

        node = make_retrieve_precedents(BM25Retriever(seed_precedent_records()), k=3)
        assert len(node(state_for(CLEAN))["precedents"]) == 3

    def test_no_retriever_is_the_zero_shot_arm_not_an_error(self):
        node = make_retrieve_precedents(None, k=5)
        assert node(state_for(CLEAN))["precedents"] == []


class TestInvestigate:
    def test_stops_when_the_model_says_it_is_done(self):
        llm = ScriptedLLM([json.dumps({"done": True, "why": "nothing further"})])
        result = make_investigate(llm)(state_for(CLEAN))
        assert result["tool_calls"] == []
        assert "finished" in result["trace"][-1]["detail"]

    def test_executes_a_requested_tool_and_records_the_result(self):
        llm = ScriptedLLM([
            json.dumps({"tool": "fetch_payment", "args": {}, "why": "look"}),
            json.dumps({"done": True, "why": "seen enough"}),
        ])
        calls = make_investigate(llm)(state_for(CLEAN))["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["tool"] == "fetch_payment" and calls[0]["ok"]
        assert calls[0]["result"]["count"] == 1

    def test_enforces_the_five_call_cap_in_code(self):
        # A limit the model can talk past is not a limit, and an unbounded loop is how one
        # hard case consumes a whole batch's quota.
        llm = ScriptedLLM([json.dumps({"tool": "fetch_payment", "args": {}})] * 50)
        result = make_investigate(llm)(state_for(CLEAN))
        assert len(result["tool_calls"]) == MAX_TOOL_CALLS
        assert "cap" in result["trace"][-1]["detail"]

    def test_an_unknown_tool_is_fed_back_rather_than_dropped(self):
        llm = ScriptedLLM([
            json.dumps({"tool": "fetch_moon_phase", "args": {}}),
            json.dumps({"done": True}),
        ])
        calls = make_investigate(llm)(state_for(CLEAN))["tool_calls"]
        assert calls[0]["ok"] is False
        assert "no such tool" in calls[0]["result"]["error"]

    def test_bad_arguments_are_reported_not_raised(self):
        llm = ScriptedLLM([
            json.dumps({"tool": "fetch_payment", "args": {"nonsense": 1}}),
            json.dumps({"done": True}),
        ])
        calls = make_investigate(llm)(state_for(CLEAN))["tool_calls"]
        assert calls[0]["ok"] is False
        assert "bad arguments" in calls[0]["result"]["error"]

    def test_an_unparseable_decision_ends_the_loop_without_raising(self):
        llm = ScriptedLLM(["I'll look at the payment I think"])
        result = make_investigate(llm)(state_for(CLEAN))
        assert result["tool_calls"] == []
        assert "unparseable" in result["trace"][-1]["detail"]

    def test_an_unavailable_model_ends_the_loop_without_killing_the_case(self):
        # Investigation is evidence gathering; propose_resolution can still run on what
        # was gathered, so an outage here must not be terminal.
        llm = ScriptedLLM([LLMUnavailable("down")])
        result = make_investigate(llm)(state_for(CLEAN))
        assert "unavailable" in result["trace"][-1]["detail"]


class TestProposeResolution:
    def test_parses_a_valid_proposal(self):
        result = make_propose_resolution(ScriptedLLM([proposal_json()]))(state_for(CLEAN))
        assert result["proposal"].reason_code is ReasonCode.EXACT_MATCH
        assert result["revisions"] == 0

    def test_an_unavailable_model_escalates_with_its_own_reason_code(self):
        result = make_propose_resolution(ScriptedLLM([LLMUnavailable("down")]))(state_for(CLEAN))
        assert result["reason_code"] is ReasonCode.ESCALATED_MODEL_UNAVAILABLE
        assert result["escalated"] and result["proposal"] is None

    def test_unparseable_output_escalates_as_a_parse_failure(self):
        result = make_propose_resolution(ScriptedLLM(["no json here"]))(state_for(CLEAN))
        assert result["reason_code"] is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_a_schema_violation_escalates_as_a_parse_failure(self):
        result = make_propose_resolution(ScriptedLLM(['{"reason_code": "made_up"}']))(
            state_for(CLEAN)
        )
        assert result["reason_code"] is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_revising_increments_the_counter_in_state(self):
        # Counted in state rather than a node's local scope: the cycle revisits this node,
        # and a counter that reset would loop forever on a case the model cannot fix.
        node = make_propose_resolution(ScriptedLLM([proposal_json()]), revising=True)
        assert node(state_for(CLEAN, revisions=1))["revisions"] == 2

    def test_revising_feeds_the_verification_failure_back(self):
        llm = ScriptedLLM([proposal_json()])
        node = make_propose_resolution(llm, revising=True)
        node(state_for(CLEAN, verification_notes=["the arithmetic does not close"]))
        assert "the arithmetic does not close" in llm.last_user_prompt
        assert "failed verification" in llm.last_user_prompt

    def test_the_evidence_gathered_reaches_the_prompt(self):
        llm = ScriptedLLM([proposal_json()])
        calls = [{"tool": "fetch_payment", "args": {}, "ok": True, "result": {"count": 1}}]
        make_propose_resolution(llm)(state_for(CLEAN, tool_calls=calls))
        assert "fetch_payment" in llm.last_user_prompt


class TestVerify:
    """Spec §7's two checks, both in code. A check performed by the model being checked
    samples the same distribution that produced the error."""

    def make_state(self, case, code, cited=(), precedents=()):
        from precedent.usecases.resolve import ProposedResolution

        return state_for(
            case,
            proposal=ProposedResolution(
                reason_code=code, confidence=0.9, rationale="x",
                cited_precedent_ids=list(cited),
            ),
            precedents=list(precedents),
        )

    def test_passes_a_proposal_whose_arithmetic_closes(self):
        result = verify(self.make_state(CLEAN, ReasonCode.EXACT_MATCH))
        assert result["verified"] and result["verification_notes"] == []

    def test_rejects_an_exact_match_whose_arithmetic_does_not_close(self):
        case = ReconciliationCase("c1", [payment()], [credit(amount=1)], [ledger()])
        result = verify(self.make_state(case, ReasonCode.EXACT_MATCH))
        assert not result["verified"]
        assert "no fee explains" in result["verification_notes"][0]

    def test_rejects_a_withholding_claim_when_the_invoice_does_not_exceed_what_was_paid(self):
        result = verify(self.make_state(CLEAN, ReasonCode.TDS_SHORT_PAYMENT))
        assert not result["verified"]

    def test_accepts_a_withholding_claim_when_it_does(self):
        paid = payment(amount=90_000, fee=1_800, tax=324)
        case = ReconciliationCase("c1", [paid], [credit(amount=87_876)],
                                  [ledger(expects=100_000)])
        assert verify(self.make_state(case, ReasonCode.TDS_SHORT_PAYMENT))["verified"]

    def test_rejects_a_refund_claim_with_no_shortfall_to_explain(self):
        assert not verify(self.make_state(CLEAN, ReasonCode.REFUND_NETTED))["verified"]

    def test_rejects_a_duplicate_claim_on_a_single_payment(self):
        result = verify(self.make_state(CLEAN, ReasonCode.DUPLICATE_PAYMENT_REJECTED))
        assert not result["verified"]

    def test_rejects_a_direct_transfer_claim_when_a_payment_exists(self):
        result = verify(self.make_state(CLEAN, ReasonCode.DIRECT_NEFT_BYPASS))
        assert not result["verified"]

    def test_catches_an_invented_citation(self):
        result = verify(self.make_state(CLEAN, ReasonCode.EXACT_MATCH, cited=["prec_nope"]))
        assert not result["verified"]
        assert "invented" in result["verification_notes"][0]

    def test_catches_a_citation_that_contradicts_the_conclusion(self):
        tds = next(r for r in seed_precedent_records() if r.reason_code == "tds_short_payment")
        result = verify(self.make_state(
            CLEAN, ReasonCode.EXACT_MATCH,
            cited=[tds.precedent_id], precedents=[RetrievedPrecedent(tds, 1.0)],
        ))
        assert not result["verified"]
        assert "does not support" in result["verification_notes"][0]

    def test_accepts_a_citation_that_agrees_with_the_conclusion(self):
        exact = next(r for r in seed_precedent_records() if r.reason_code == "exact_match")
        result = verify(self.make_state(
            CLEAN, ReasonCode.EXACT_MATCH,
            cited=[exact.precedent_id], precedents=[RetrievedPrecedent(exact, 1.0)],
        ))
        assert result["verified"]

    def test_nothing_to_verify_is_a_failure_not_a_pass(self):
        result = verify(state_for(CLEAN))
        assert not result["verified"]


class TestRouting:
    def test_a_verified_proposal_goes_to_finalize(self):
        assert route_after_verify(state_for(CLEAN, verified=True)) == "finalize"

    def test_a_failure_goes_to_revise_while_revisions_remain(self):
        assert route_after_verify(state_for(CLEAN, verified=False, revisions=0)) == "revise"

    def test_the_revision_limit_sends_it_to_escalate(self):
        state = state_for(CLEAN, verified=False, revisions=MAX_REVISIONS)
        assert route_after_verify(state) == "escalate"

    def test_an_already_escalated_case_never_loops(self):
        # A model outage during propose must not be sent round the revise cycle.
        state = state_for(CLEAN, escalated=True, verified=False)
        assert route_after_verify(state) == "escalate"


class TestFinalize:
    def make_state(self, confidence):
        from precedent.usecases.resolve import ProposedResolution

        return state_for(CLEAN, proposal=ProposedResolution(
            reason_code=ReasonCode.EXACT_MATCH, confidence=confidence, rationale="x",
        ))

    def test_auto_resolves_at_or_above_the_threshold(self):
        result = make_finalize(0.8)(self.make_state(0.8))
        assert not result["escalated"]
        assert result["reason_code"] is ReasonCode.EXACT_MATCH

    def test_escalates_below_the_threshold(self):
        result = make_finalize(0.8)(self.make_state(0.79))
        assert result["escalated"]
        assert result["reason_code"] is ReasonCode.ESCALATED_LOW_CONFIDENCE


class TestEscalate:
    def test_verify_failure_gets_its_own_reason_code(self):
        result = escalate(state_for(CLEAN, verification_notes=["arithmetic does not close"]))
        assert result["reason_code"] is ReasonCode.ESCALATED_VERIFY_FAILED
        assert "arithmetic" in result["rationale"]

    def test_a_reason_code_already_set_by_a_failed_call_is_preserved(self):
        # An outage escalation must not be relabelled as a verification failure.
        state = state_for(
            CLEAN, escalated=True, reason_code=ReasonCode.ESCALATED_MODEL_UNAVAILABLE
        )
        result = escalate(state)
        assert "reason_code" not in result or result.get("reason_code") is None
        assert result["trace"][0]["detail"] == "escalated_model_unavailable"


class TestVerifyRejectsExactMatchOnMultipleInvoices:
    """Arithmetic closure alone is not sufficient for `exact_match`.

    In a split payment the single payment settles to the credit exactly, so the gap check
    passes and `exact_match` sails through — leaving the second invoice open against a
    customer who has paid in full. Five of six zero-shot false resolutions in the Ring 2
    run were exactly this, at 0.85-0.95 confidence.
    """

    def state_with(self, entries, code=ReasonCode.EXACT_MATCH):
        from precedent.usecases.resolve import ProposedResolution

        case = ReconciliationCase("c1", [payment()], [credit()], entries)
        return state_for(case, proposal=ProposedResolution(
            reason_code=code, confidence=0.95, rationale="x",
        ))

    def test_rejects_exact_match_when_two_invoices_are_open(self):
        result = verify(self.state_with([ledger(expects=60_000), ledger(expects=40_000)]))
        assert not result["verified"]
        assert "open ledger entries" in result["verification_notes"][0]

    def test_still_accepts_exact_match_on_a_single_invoice(self):
        assert verify(self.state_with([ledger()]))["verified"]

    def test_does_not_penalise_split_payment_for_the_same_shape(self):
        # The very shape that makes `exact_match` wrong is what makes `split_payment` right.
        result = verify(self.state_with(
            [ledger(expects=60_000), ledger(expects=40_000)], ReasonCode.SPLIT_PAYMENT
        ))
        assert result["verified"]
