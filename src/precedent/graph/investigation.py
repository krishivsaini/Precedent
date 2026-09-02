"""The investigation graph (spec §7), wired with LangGraph.

```
classify_kind → retrieve_precedents → investigate → propose_resolution → verify
                                                          ↑                 │
                                                        revise ←────────────┤ fail (max 2)
                                                                            │
                                            finalize ←───────────────────── ┘ pass
                                            escalate ←──── verify failed twice
```

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
):
    """Compile the graph. `checkpointer` defaults to an in-memory saver."""
    graph = StateGraph(InvestigationState)

    graph.add_node("classify_kind", classify_kind)
    graph.add_node("retrieve_precedents", make_retrieve_precedents(retriever, k))
    graph.add_node("investigate", make_investigate(llm, workspace_factory))
    graph.add_node("propose_resolution", make_propose_resolution(llm))
    graph.add_node("verify", verify)
    graph.add_node("revise", make_propose_resolution(llm, revising=True))
    graph.add_node("finalize", make_finalize(threshold))
    graph.add_node("escalate", escalate)

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


def format_trace(trace: list[dict]) -> str:
    """The trace as readable text. Ring 2's exit checklist requires a *visible* trace — a
    graph whose reasoning cannot be inspected is not better than the prompt it replaced."""
    return "\n".join(f"  {entry['node']:22} {entry['detail']}" for entry in trace)
