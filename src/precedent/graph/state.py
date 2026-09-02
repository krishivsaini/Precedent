"""The state carried through the investigation graph (spec §7).

A LangGraph `StateGraph` state, so it is a `TypedDict` with reducers rather than a dataclass:
nodes return partial updates and the graph merges them.

Two fields are load-bearing beyond the flow itself:

* **`trace`** — every node appends what it did. Ring 2's exit checklist requires an
  end-to-end run with a *visible* trace, and a graph whose reasoning cannot be inspected is
  not meaningfully better than the single prompt it replaced.
* **`revisions`** — counted in the state rather than in a node's local scope, because the
  `verify → revise → verify` cycle is a real cycle in the graph. A counter that reset each
  time through would loop forever on a case the model cannot fix.
"""

import operator
from typing import Annotated, Any, TypedDict

from precedent.adapters.retrieval.base import RetrievedPrecedent
from precedent.domain.case import ReconciliationCase
from precedent.domain.reasons import ReasonCode
from precedent.usecases.resolve import ProposedResolution

#: Spec §7: "investigate (tool loop, max 5 calls)". A cap rather than a budget the model can
#: negotiate — an unbounded loop is the failure mode where one hard case consumes a batch.
MAX_TOOL_CALLS = 5

#: Spec §7: "fail → revise (max 2) → verify". After two failed revisions the case escalates
#: with `escalated_verify_failed` rather than being revised indefinitely.
MAX_REVISIONS = 2


class ToolCall(TypedDict):
    """One investigation step, recorded whether it succeeded or not.

    Failed calls stay in the trace: a model that asked for a tool that does not exist, or
    asked with bad arguments, has told you something about the prompt.
    """

    tool: str
    args: dict[str, Any]
    result: Any
    ok: bool


class TraceEntry(TypedDict):
    node: str
    detail: str


class InvestigationState(TypedDict, total=False):
    """Everything the graph knows about one case.

    `total=False` because nodes populate it progressively; only `case` is present at entry.
    """

    case: ReconciliationCase
    correlation_id: str

    # classify_kind
    kind: str

    # retrieve_precedents
    precedents: list[RetrievedPrecedent]

    # investigate — appended to, so it needs a reducer
    tool_calls: Annotated[list[ToolCall], operator.add]

    # propose_resolution / revise
    proposal: ProposedResolution | None
    revisions: int

    # verify
    verified: bool
    verification_notes: list[str]

    # gate — the human decision (Ring 3.1)
    human_action: str        # "confirmed" | "corrected" | "rejected"
    correction_note: str
    corrected_reason_code: str

    # route → terminal
    reason_code: ReasonCode
    confidence: float
    escalated: bool
    rationale: str

    # Appended by every node.
    trace: Annotated[list[TraceEntry], operator.add]


def initial_state(case: ReconciliationCase, correlation_id: str) -> InvestigationState:
    """The entry state. Accumulating fields start empty rather than absent, so a node that
    reads one before another has written it gets an empty list, not a `KeyError`."""
    return {
        "case": case,
        "correlation_id": correlation_id,
        "tool_calls": [],
        "trace": [],
        "revisions": 0,
        "proposal": None,
        "verified": False,
        "verification_notes": [],
        "escalated": False,
    }


def traced(node: str, detail: str) -> list[TraceEntry]:
    """Helper so every node records itself the same way."""
    return [{"node": node, "detail": detail}]
