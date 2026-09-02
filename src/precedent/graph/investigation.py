"""The investigation graph (spec §7), wired with LangGraph.

```
classify_kind → retrieve_precedents → investigate → propose_resolution → verify
                                                          ↑                 │
                                                        revise ←────────────┤ fail (max 2)
                                                                            │
                          gate [interrupt] ← finalize ←──────────────────── ┘ pass
                                            escalate ←──── verify failed twice
```

`gate` is optional at build time: the ablation runs without it, because an eval that stopped
for a human on every case would measure nothing. Ring 3 turns it on.

**Why a graph rather than the Ring 1 chain.** The honest answer is that the cycle is the only
part a chain cannot express: `verify → revise → verify` needs to route back to a node it has
already visited, with a counter that survives the loop. Everything else here — classify,
retrieve, propose — is linear and a chain would do it.

That is worth stating plainly because Ring 2's gate is that the graph must not regress against
Ring 1's grounded arm, which scored 100%. There is no headroom to improve on, so the graph
has to justify itself on structure rather than accuracy: a durable, inspectable, resumable
state machine, into which Ring 3's human gate can be dropped as an `interrupt` between
`finalize` and the deposit. A chain gives none of that.

The checkpointer is what makes that gate possible: a paused resolution survives a process
restart. Ring 2 wires it; Ring 3 uses it.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from precedent.adapters.llm.base import LLMClient
from precedent.adapters.llm.metered import MeteredLLM
from precedent.adapters.retrieval.base import Retriever
from precedent.domain.case import ReconciliationCase
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD
from precedent.graph.nodes import (
    classify_kind,
    escalate,
    gate,
    make_finalize,
    make_investigate,
    make_propose_resolution,
    make_retrieve_precedents,
    route_after_verify,
    verify,
)
from precedent.graph.state import InvestigationState, initial_state
from precedent.graph.tools import CaseWorkspace
from precedent.usecases.resolve import ResolutionOutcome


def build_investigation_graph(
    llm: LLMClient,
    retriever: Retriever | None = None,
    k: int = 5,
    threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD,
    workspace_factory=CaseWorkspace,
    checkpointer=None,
    with_gate: bool = False,
):
    """Compile the graph.

    `with_gate=True` inserts the human `interrupt` between `finalize` and the end. It is off
    by default because the ablation must run unattended — a graph that stops for a human on
    every case measures nothing.

    `checkpointer` defaults to an in-memory saver, which is fine for an eval and **useless
    for the gate**: a paused resolution held only in memory does not survive the restart that
    spec §7 claims it survives. Pass a `SqliteSaver` when the gate is on; `durable_graph()`
    does exactly that.
    """
    graph = StateGraph(InvestigationState)

    graph.add_node("classify_kind", classify_kind)
    graph.add_node("retrieve_precedents", make_retrieve_precedents(retriever, k))
    graph.add_node("investigate", make_investigate(llm, workspace_factory))
    graph.add_node("propose_resolution", make_propose_resolution(llm))
    graph.add_node("verify", verify)
    graph.add_node("revise", make_propose_resolution(llm, revising=True))
    graph.add_node("finalize", make_finalize(threshold))
    graph.add_node("escalate", escalate)
    if with_gate:
        graph.add_node("gate", gate)

    graph.add_edge(START, "classify_kind")
    graph.add_edge("classify_kind", "retrieve_precedents")
    graph.add_edge("retrieve_precedents", "investigate")
    graph.add_edge("investigate", "propose_resolution")
    graph.add_edge("propose_resolution", "verify")

    # The cycle, and the reason this is a graph at all.
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"finalize": "finalize", "revise": "revise", "escalate": "escalate"},
    )
    graph.add_edge("revise", "verify")

    if with_gate:
        # Only an auto-resolved proposal reaches the gate. `finalize` also emits the
        # low-confidence escalation, and that case has already been routed to a human by
        # definition — sending it through the gate as well would ask the same question
        # twice, and would block on an approval nobody was waiting to give.
        graph.add_conditional_edges(
            "finalize",
            lambda state: "gate" if not state.get("escalated") else "done",
            {"gate": "gate", "done": END},
        )
        graph.add_edge("gate", END)
    else:
        graph.add_edge("finalize", END)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


def run_investigation(
    case: ReconciliationCase,
    llm: LLMClient,
    retriever: Retriever | None = None,
    k: int = 5,
    threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD,
    graph=None,
    workspace_factory=CaseWorkspace,
) -> tuple[ResolutionOutcome, list[dict]]:
    """Run one case to a terminal state.

    Returns the outcome and the trace. Returning the same `ResolutionOutcome` the Ring 1
    chain produces is deliberate: the ablation scores both through identical code, so a
    difference between them is a difference in the system rather than in how it was measured.

    Passing a prebuilt `graph` skips metering — that graph is already bound to whatever
    client it was compiled with — so the cost fields come back zero. Compiling is cheap;
    omit `graph` whenever the numbers matter.
    """
    # Metered per case so the outcome carries what this case cost. The graph makes between
    # two and eight model calls, so unlike the Ring 1 chain there is no single response to
    # read the totals off — and a cost metric that silently reports zero when the system
    # gets more expensive is worse than not reporting one.
    metered = MeteredLLM(llm)
    compiled = graph or build_investigation_graph(
        metered, retriever, k, threshold, workspace_factory
    )
    config = {"configurable": {"thread_id": case.case_id}}
    final = compiled.invoke(initial_state(case, case.case_id), config)

    proposal = final.get("proposal")
    return (
        ResolutionOutcome(
            case_id=case.case_id,
            reason_code=final["reason_code"],
            confidence=final.get("confidence", 0.0),
            rationale=final.get("rationale", ""),
            cited_precedent_ids=list(proposal.cited_precedent_ids) if proposal else [],
            retrieved_precedent_ids=[
                h.record.precedent_id for h in (final.get("precedents") or [])
            ],
            escalated=final.get("escalated", False),
            model=llm.model,
            prompt_tokens=metered.prompt_tokens,
            completion_tokens=metered.completion_tokens,
            thinking_tokens=metered.thinking_tokens,
            latency_ms=metered.latency_ms or None,
            attempts=max(1, metered.attempts),
            cached=metered.cached_calls == metered.calls and metered.calls > 0,
            model_calls=metered.calls,
            tool_calls_made=len(
                [c for c in (final.get("tool_calls") or []) if c.get("ok")]
            ),
        ),
        final.get("trace", []),
    )


def durable_graph(
    llm: LLMClient,
    checkpoint_path: str,
    retriever: Retriever | None = None,
    k: int = 5,
    threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD,
):
    """A gated graph whose paused state is on disk, not in memory.

    Returns `(compiled_graph, checkpointer_context)`. The caller must keep the context open
    for the lifetime of the graph — `SqliteSaver.from_conn_string` is a context manager, and
    closing it closes the connection the checkpoints live in.

    This is the only configuration in which spec §7's durability claim is true. Anything
    using the default `MemorySaver` loses a paused resolution the moment the process ends.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    context = SqliteSaver.from_conn_string(checkpoint_path)
    checkpointer = context.__enter__()
    checkpointer.setup()
    compiled = build_investigation_graph(
        llm, retriever, k, threshold, checkpointer=checkpointer, with_gate=True
    )
    return compiled, context


def pending_gate(compiled, case_id: str) -> dict | None:
    """What the graph is waiting for on this case, or None if it is not waiting.

    Reads the checkpoint rather than any in-process state, so it answers correctly in a
    process that never ran the graph — which is what makes an approval API possible.
    """
    snapshot = compiled.get_state({"configurable": {"thread_id": case_id}})
    interrupts = getattr(snapshot, "interrupts", None) or ()
    if not interrupts:
        return None
    return interrupts[0].value


#: The only decisions a human may return from the gate.
HUMAN_ACTIONS = frozenset({"confirmed", "corrected", "rejected"})


def resume_gate(compiled, case_id: str, decision: dict) -> dict:
    """Resume a paused case with the human's decision. Returns the final state.

    Validated here rather than only in the node, because LangGraph treats an empty or falsy
    resume value as *no resume at all*: the graph silently pauses again and the caller gets
    back a state that looks like it was acted on. Failing loudly at the boundary is the
    difference between "your approval was rejected" and "your approval vanished".
    """
    from langgraph.types import Command

    if not isinstance(decision, dict) or not decision:
        raise ValueError(
            "the gate must be resumed with a non-empty decision object; an empty resume is "
            "silently ignored by the graph and would leave the case still pending"
        )
    action = decision.get("human_action")
    if action not in HUMAN_ACTIONS:
        raise ValueError(
            f"human_action must be one of {sorted(HUMAN_ACTIONS)}; got {action!r}"
        )
    if action == "corrected" and not decision.get("corrected_reason_code"):
        # A correction with nothing corrected would deposit the agent's original answer
        # under the label of a human correction — the worst of both.
        raise ValueError("a corrected decision must carry corrected_reason_code")

    return compiled.invoke(
        Command(resume=decision), {"configurable": {"thread_id": case_id}}
    )


def format_trace(trace: list[dict]) -> str:
    """The trace as readable text. Ring 2's exit checklist requires a *visible* trace — a
    graph whose reasoning cannot be inspected is not better than the prompt it replaced."""
    return "\n".join(f"  {entry['node']:22} {entry['detail']}" for entry in trace)
