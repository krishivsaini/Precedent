"""The compiled graph, end to end against a scripted model. No network.

The cycle is what justifies this being a graph rather than the Ring 1 chain, so it is what
these tests are mostly about: that `verify → revise → verify` actually routes back, that the
revision counter survives the loop, and that the loop terminates.
"""

import json

import pytest

from precedent.adapters.llm.base import LLMUnavailable
from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import (
    build_investigation_graph,
    format_trace,
    run_investigation,
)
from precedent.graph.state import MAX_REVISIONS
from tests.domain.test_case import credit, ledger, payment

CLEAN = ReconciliationCase("case_clean", [payment()], [credit()], [ledger()])
DONE = json.dumps({"done": True, "why": "nothing further would change the answer"})


def proposal(code="exact_match", confidence=0.95, cited=()):
    return json.dumps({
        "reason_code": code, "confidence": confidence,
        "rationale": "the credit equals the payment net of fee and tax",
        "cited_precedent_ids": list(cited),
    })


def run(responses, case=CLEAN, retriever=None, threshold=0.8):
    return run_investigation(
        case, ScriptedLLM(responses), retriever=retriever, threshold=threshold
    )


class TestHappyPath:
    def test_resolves_a_clean_case_end_to_end(self):
        outcome, _ = run([DONE, proposal()])
        assert outcome.reason_code is ReasonCode.EXACT_MATCH
        assert not outcome.escalated
        assert outcome.confidence == 0.95

    def test_visits_every_node_on_the_way(self):
        # Ring 2's exit checklist requires a visible trace: a graph whose reasoning cannot
        # be inspected is not better than the prompt it replaced.
        _, trace = run([DONE, proposal()])
        visited = [entry["node"] for entry in trace]
        assert visited[:4] == [
            "classify_kind", "retrieve_precedents", "investigate", "propose_resolution",
        ]
        assert "verify" in visited and "route" in visited

    def test_the_trace_renders_as_readable_text(self):
        _, trace = run([DONE, proposal()])
        rendered = format_trace(trace)
        assert "classify_kind" in rendered
        assert len(rendered.splitlines()) == len(trace)

    def test_retrieved_precedents_reach_the_outcome(self):
        outcome, _ = run(
            [DONE, proposal()], retriever=BM25Retriever(seed_precedent_records())
        )
        assert len(outcome.retrieved_precedent_ids) == 5


class TestTheCycle:
    """`verify → revise → verify` is the only part of spec §7 a chain cannot express."""

    def test_a_failed_verification_routes_back_through_revise(self):
        # First proposal claims a withholding shortfall on a case that has none; the second
        # corrects it.
        outcome, trace = run([DONE, proposal("tds_short_payment"), proposal("exact_match")])
        nodes = [entry["node"] for entry in trace]
        assert "revise" in nodes
        assert nodes.count("verify") == 2
        assert outcome.reason_code is ReasonCode.EXACT_MATCH
        assert not outcome.escalated

    def test_the_revision_limit_terminates_the_loop(self):
        # A model that never corrects itself must not spin forever.
        wrong = proposal("tds_short_payment")
        outcome, trace = run([DONE] + [wrong] * 10)
        assert outcome.escalated
        assert outcome.reason_code is ReasonCode.ESCALATED_VERIFY_FAILED
        assert [e["node"] for e in trace].count("revise") == MAX_REVISIONS

    def test_the_escalation_explains_what_failed_verification(self):
        outcome, _ = run([DONE] + [proposal("tds_short_payment")] * 10)
        assert "ledger expects" in outcome.rationale

    def test_a_case_that_never_fails_verification_never_revises(self):
        _, trace = run([DONE, proposal()])
        assert "revise" not in [entry["node"] for entry in trace]


class TestEscalationPaths:
    def test_an_unavailable_model_at_proposal_escalates_without_looping(self):
        outcome, trace = run([DONE, LLMUnavailable("down")])
        assert outcome.reason_code is ReasonCode.ESCALATED_MODEL_UNAVAILABLE
        assert "revise" not in [entry["node"] for entry in trace]

    def test_unparseable_output_escalates_as_a_parse_failure(self):
        outcome, _ = run([DONE, "I think this is fine honestly"])
        assert outcome.reason_code is ReasonCode.ESCALATED_PARSE_FAILURE

    def test_low_confidence_escalates_even_when_verified(self):
        outcome, _ = run([DONE, proposal(confidence=0.4)], threshold=0.8)
        assert outcome.escalated
        assert outcome.reason_code is ReasonCode.ESCALATED_LOW_CONFIDENCE
        assert outcome.confidence == 0.4

    def test_an_invented_citation_fails_verification(self):
        outcome, _ = run([DONE] + [proposal(cited=["prec_seed_9999"])] * 10)
        assert outcome.escalated
        assert "invented" in outcome.rationale

    def test_the_graph_always_reaches_a_terminal_reason_code(self):
        # Every path out of the graph must carry a reason code; a case that ends without
        # one would vanish from the exception list rather than being reported.
        for responses in (
            [DONE, proposal()],
            [DONE, LLMUnavailable("x")],
            [DONE, "garbage"],
            [DONE] + [proposal("tds_short_payment")] * 10,
            [LLMUnavailable("x"), proposal()],
        ):
            outcome, _ = run(responses)
            assert isinstance(outcome.reason_code, ReasonCode)


class TestToolLoopIntegration:
    def test_tool_results_are_gathered_before_the_proposal(self):
        llm = ScriptedLLM([
            json.dumps({"tool": "search_prior_resolutions", "args": {}}),
            DONE,
            proposal(),
        ])
        outcome, trace = run_investigation(CLEAN, llm)
        details = " ".join(e["detail"] for e in trace)
        assert "search_prior_resolutions" in details
        assert not outcome.escalated

    def test_an_investigation_outage_does_not_prevent_a_resolution(self):
        # Investigation is evidence gathering. Losing it degrades the answer; it must not
        # abort the case.
        outcome, _ = run([LLMUnavailable("blip"), proposal()])
        assert not outcome.escalated
        assert outcome.reason_code is ReasonCode.EXACT_MATCH


class TestIsolationAndCheckpointing:
    def test_each_case_runs_under_its_own_thread(self):
        # The checkpointer keys on the case id, so two cases cannot share state — and a
        # paused resolution will survive a restart when Ring 3 adds the human gate.
        graph = build_investigation_graph(ScriptedLLM([DONE, proposal()] * 2))
        a, _ = run_investigation(CLEAN, ScriptedLLM([DONE, proposal()]), graph=graph)
        other = ReconciliationCase("case_other", [payment("o9")], [credit()], [ledger("o9")])
        b, _ = run_investigation(other, ScriptedLLM([DONE, proposal()]), graph=graph)
        assert a.case_id == "case_clean" and b.case_id == "case_other"

    def test_the_compiled_graph_is_reusable_across_cases(self):
        graph = build_investigation_graph(ScriptedLLM([DONE, proposal()] * 4))
        for index in range(2):
            case = ReconciliationCase(f"c{index}", [payment()], [credit()], [ledger()])
            outcome, _ = run_investigation(
                case, ScriptedLLM([DONE, proposal()]), graph=graph
            )
            assert not outcome.escalated
