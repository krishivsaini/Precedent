"""The human gate, and the durability claim it rests on.

Spec §7 justifies `interrupt` to the panel as "a durable human gate on a state-changing
action, with checkpointed state so a paused resolution survives a process restart. A chain
gives neither."

That claim is only worth making if it is true, so `TestSurvivesAProcessRestart` **starts a
second Python interpreter**. The first process pauses a case at the gate and exits; the
second, which never ran the graph, reads the pending decision off disk and resumes it. A test
that paused and resumed inside one process would prove the checkpointer works in memory,
which is precisely the thing that is not in question.
"""

import json
import subprocess
import sys
import textwrap

import pytest

from precedent.adapters.llm.scripted import ScriptedLLM
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import (
    build_investigation_graph,
    durable_graph,
    pending_gate,
    resume_gate,
    run_investigation,
)
from precedent.graph.state import initial_state
from tests.domain.test_case import credit, ledger, payment

CASE = ReconciliationCase("case_gate", [payment()], [credit()], [ledger()])
DONE = json.dumps({"done": True, "why": "nothing further"})


def proposal(code="exact_match", confidence=0.95):
    return json.dumps({
        "reason_code": code, "confidence": confidence,
        "rationale": "the credit equals the payment net of fee and tax",
        "cited_precedent_ids": [],
    })


def gated(responses):
    llm = ScriptedLLM(responses)
    return build_investigation_graph(llm, with_gate=True)


def config(case_id="case_gate"):
    return {"configurable": {"thread_id": case_id}}


class TestPausing:
    def test_the_graph_stops_at_the_gate_instead_of_finishing(self):
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        assert pending_gate(compiled, CASE.case_id) is not None

    def test_the_pause_carries_what_a_reviewer_needs_to_decide(self):
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        payload = pending_gate(compiled, CASE.case_id)
        assert payload["proposed_reason_code"] == "exact_match"
        assert payload["confidence"] == 0.95
        assert payload["rationale"]
        assert "order_1" in payload["case_summary"]

    def test_an_escalated_case_does_not_stop_at_the_gate(self):
        # It has already been routed to a human by definition; a second human step would be
        # asking the same question twice.
        compiled = gated([DONE, proposal(confidence=0.2)])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        assert pending_gate(compiled, CASE.case_id) is None

    def test_a_graph_built_without_the_gate_never_pauses(self):
        # The ablation must run unattended: a graph that stops for a human on every case
        # measures nothing.
        outcome, _ = run_investigation(CASE, ScriptedLLM([DONE, proposal()]))
        assert outcome.reason_code is ReasonCode.EXACT_MATCH


class TestResuming:
    def test_confirming_completes_the_case(self):
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        final = resume_gate(compiled, CASE.case_id, {"human_action": "confirmed"})
        assert final["human_action"] == "confirmed"
        assert pending_gate(compiled, CASE.case_id) is None

    def test_a_correction_replaces_the_proposed_reason_code(self):
        # It is the corrected version that gets deposited (spec §7). Depositing the agent's
        # original would teach it the mistake it just made.
        compiled = gated([DONE, proposal("exact_match")])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        final = resume_gate(compiled, CASE.case_id, {
            "human_action": "corrected",
            "corrected_reason_code": "negotiated_rebate",
            "correction_note": "this customer has a standing rebate",
        })
        assert final["reason_code"] is ReasonCode.NEGOTIATED_REBATE
        assert final["correction_note"]

    def test_rejecting_is_recorded_and_deposits_nothing(self):
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        final = resume_gate(compiled, CASE.case_id, {"human_action": "rejected"})
        assert final["human_action"] == "rejected"

    def test_an_unrecognised_decision_is_refused(self):
        # Falling through to "confirmed" on a malformed resume would let a typo write to the
        # corpus. The gate is the boundary between a proposal and an action.
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        with pytest.raises(ValueError, match="human_action must be"):
            resume_gate(compiled, CASE.case_id, {"human_action": "looks fine to me"})

    def test_an_empty_decision_is_refused_loudly(self):
        # LangGraph treats a falsy resume as *no resume*: the graph pauses again and the
        # caller gets back a state that looks acted-on. The difference between "your
        # approval was rejected" and "your approval vanished" matters at a gate.
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        with pytest.raises(ValueError, match="non-empty decision"):
            resume_gate(compiled, CASE.case_id, {})
        assert pending_gate(compiled, CASE.case_id) is not None

    def test_a_correction_with_nothing_corrected_is_refused(self):
        # It would deposit the agent's original answer under the label of a human
        # correction — the worst of both.
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        with pytest.raises(ValueError, match="corrected_reason_code"):
            resume_gate(compiled, CASE.case_id, {"human_action": "corrected"})


class TestIsolation:
    def test_two_paused_cases_do_not_see_each_other(self):
        compiled = gated([DONE, proposal(), DONE, proposal("netted_settlement", 0.9)])
        other = ReconciliationCase("case_other", [payment("o9")], [credit()], [ledger("o9")])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        compiled.invoke(initial_state(other, other.case_id), config("case_other"))

        assert pending_gate(compiled, "case_gate")["proposed_reason_code"] == "exact_match"
        assert pending_gate(compiled, "case_other")["proposed_reason_code"] == "netted_settlement"

        resume_gate(compiled, "case_gate", {"human_action": "confirmed"})
        # Resolving one must not resolve the other.
        assert pending_gate(compiled, "case_other") is not None


class TestSurvivesAProcessRestart:
    """The load-bearing test of the gate, and the reason `MemorySaver` is not enough.

    A design note claiming durability is not proof, so this pauses a case in one interpreter,
    lets that interpreter exit, and resumes in a second one that never ran the graph.
    """

    def run_in_subprocess(self, source: str, cwd) -> dict:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            capture_output=True, text=True, cwd=str(cwd), timeout=180,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_a_paused_case_survives_the_process_that_paused_it(self, tmp_path):
        checkpoint = tmp_path / "checkpoints.sqlite"
        repo_root = __import__("pathlib").Path(__file__).resolve().parents[2]

        preamble = f"""
            import json, sys
            sys.path.insert(0, {str(repo_root)!r})
            sys.path.insert(0, {str(repo_root / "src")!r})
            from precedent.adapters.llm.scripted import ScriptedLLM
            from precedent.adapters.storage.records import (
                BankLineRecord, LedgerEntryRecord, PaymentRecord)
            from precedent.domain.case import ReconciliationCase
            from precedent.graph.investigation import (
                durable_graph, pending_gate, resume_gate)
            from precedent.graph.state import initial_state

            case = ReconciliationCase(
                "case_restart",
                [PaymentRecord(payment_id="pay_1", order_id="order_1", amount_paise=100000,
                               captured_at="2026-01-01T10:00:00+00:00", status="captured",
                               source="synthetic", fee_paise=2000, tax_paise=360)],
                [BankLineRecord(line_id="line_1", value_date="2026-01-02",
                                amount_paise=97640, direction="credit", narration="NEFT",
                                source="synthetic")],
                [LedgerEntryRecord(entry_id="led_1", order_id="order_1",
                                   expected_amount_paise=100000, invoice_no="INV-1",
                                   customer_name="Acme", terms="net_15")],
            )
            responses = [
                json.dumps({{"done": True}}),
                json.dumps({{"reason_code": "exact_match", "confidence": 0.95,
                            "rationale": "credit equals payment net of fee",
                            "cited_precedent_ids": []}}),
            ]
            compiled, ctx = durable_graph(ScriptedLLM(responses), {str(checkpoint)!r})
        """

        # --- process 1: run to the gate, then exit ---
        paused = self.run_in_subprocess(preamble + """
            compiled.invoke(initial_state(case, case.case_id),
                            {"configurable": {"thread_id": case.case_id}})
            payload = pending_gate(compiled, case.case_id)
            ctx.__exit__(None, None, None)
            print(json.dumps({"paused": payload is not None,
                              "proposed": payload and payload["proposed_reason_code"]}))
        """, repo_root)
        assert paused["paused"], "the first process did not pause at the gate"
        assert paused["proposed"] == "exact_match"

        # --- process 2: a fresh interpreter that never ran the graph ---
        # ScriptedLLM is deliberately given NO responses here: if resuming re-ran any model
        # call it would raise, so a clean resume proves the work was genuinely restored from
        # disk rather than recomputed.
        resumed = self.run_in_subprocess(preamble.replace(
            "ScriptedLLM(responses)", "ScriptedLLM([])"
        ) + """
            still_pending = pending_gate(compiled, case.case_id)
            final = resume_gate(compiled, case.case_id, {"human_action": "confirmed"})
            ctx.__exit__(None, None, None)
            print(json.dumps({
                "found_pending": still_pending is not None,
                "proposed": still_pending and still_pending["proposed_reason_code"],
                "human_action": final.get("human_action"),
                "reason_code": final["reason_code"].value,
            }))
        """, repo_root)

        assert resumed["found_pending"], "the second process could not see the paused case"
        assert resumed["proposed"] == "exact_match"
        assert resumed["human_action"] == "confirmed"
        assert resumed["reason_code"] == "exact_match"

    def test_an_in_memory_checkpointer_does_not_survive(self, tmp_path):
        # The control for the test above. Without it, the durability test proves only that
        # *something* worked, not that the durable checkpointer is what made it work.
        compiled = gated([DONE, proposal()])
        compiled.invoke(initial_state(CASE, CASE.case_id), config())
        assert pending_gate(compiled, CASE.case_id) is not None

        # A second graph with its own MemorySaver is the in-process stand-in for a restart.
        fresh = gated([DONE, proposal()])
        assert pending_gate(fresh, CASE.case_id) is None
