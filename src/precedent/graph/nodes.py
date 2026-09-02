"""One function per node in spec §7's investigation graph.

Nodes are plain functions of state, so each is testable without building a graph. The wiring
lives in `investigation.py`.

**Two nodes deliberately do not call a model**, and both deviate from a naive reading of the
spec diagram:

* `classify_kind` is deterministic. An LLM call here would name the exception class *before*
  any evidence is gathered, and everything downstream would be anchored on that guess. The
  classification is only a retrieval hint, and the observations already encode it.
* `verify` is deterministic. A check performed by the model being checked samples the same
  distribution that produced the error — see `prompts/verify/v1.md` for the full argument.

Every model call site escalates on failure with a reason code (spec §7). No node raises.
"""

import json
from pathlib import Path

from pydantic import ValidationError

from precedent.adapters.llm.base import LLMClient, LLMUnavailable
from precedent.adapters.retrieval.base import Retriever
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD
from precedent.domain.money import ROUNDING_TOLERANCE_PAISE
from precedent.domain.reasons import ReasonCode
from precedent.graph.state import (
    MAX_REVISIONS,
    MAX_TOOL_CALLS,
    InvestigationState,
    traced,
)
from precedent.graph.tools import CaseWorkspace
from precedent.usecases.resolve import (
    ProposedResolution,
    extract_json,
    render_precedents,
)

PROMPTS = Path(__file__).resolve().parents[3] / "prompts"


def _load(name: str) -> tuple[str, str]:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    system_part = text.split("\n## System\n", 1)[1]
    system, user = system_part.split("\n## User\n", 1)
    return system.strip(), user.strip()


# --------------------------------------------------------------------------------------
# classify_kind — deterministic


def classify_kind(state: InvestigationState) -> dict:
    """A coarse retrieval hint from the case's own computed observations.

    Not the answer, and deliberately not a model call: naming the class before the evidence
    is gathered anchors every node after it. The hint is recorded in the trace so a reader
    can see what the retriever was steered by.
    """
    case = state["case"]
    notes = " ".join(case.observations()).lower()
    if "no captured payment record exists" in notes:
        kind = "credit_without_payment"
    elif "share the same order reference" in notes:
        kind = "repeated_payment_on_one_order"
    elif "distinct order references settle against a single bank credit" in notes:
        kind = "many_payments_one_credit"
    elif "share one order reference" in notes:
        kind = "one_credit_many_invoices"
    elif "round proportion of the invoice" in notes:
        kind = "proportional_shortfall"
    elif "no fee rate accounts for it" in notes:
        kind = "unexplained_shortfall"
    elif "no bank statement line is present" in notes or "no open ledger entry" in notes:
        kind = "missing_counterpart"
    else:
        kind = "unclassified"
    return {"kind": kind, "trace": traced("classify_kind", kind)}


# --------------------------------------------------------------------------------------
# retrieve_precedents


def make_retrieve_precedents(retriever: Retriever | None, k: int):
    def retrieve_precedents(state: InvestigationState) -> dict:
        if retriever is None:
            return {
                "precedents": [],
                "trace": traced("retrieve_precedents", "no retriever configured"),
            }
        hits = retriever.retrieve(state["case"].retrieval_query(), k)
        detail = ", ".join(h.record.precedent_id for h in hits) or "none"
        return {"precedents": hits, "trace": traced("retrieve_precedents", detail)}

    return retrieve_precedents


# --------------------------------------------------------------------------------------
# investigate — the tool loop


def _render_tool_history(state: InvestigationState) -> str:
    calls = state.get("tool_calls") or []
    if not calls:
        return "(none yet)"
    return "\n\n".join(
        f"[{i}] {c['tool']}({json.dumps(c['args'])}) ->\n{json.dumps(c['result'], indent=2)[:1200]}"
        for i, c in enumerate(calls, start=1)
    )


def make_investigate(llm: LLMClient, workspace_factory=CaseWorkspace):
    """The tool loop, capped at `MAX_TOOL_CALLS` (spec §7).

    The cap is enforced here rather than being asked of the model, because a limit the model
    can talk itself past is not a limit — and an unbounded loop is the failure mode where one
    hard case consumes an entire batch's quota.
    """

    def investigate(state: InvestigationState) -> dict:
        system, user_template = _load("investigate/v1.md")
        new_calls: list[dict] = []
        trace: list[dict] = []

        with workspace_factory(state["case"]) as workspace:
            registry = workspace.registry()
            for _ in range(MAX_TOOL_CALLS):
                merged = dict(state)
                merged["tool_calls"] = list(state.get("tool_calls") or []) + new_calls
                user = (
                    user_template.replace("{case_summary}", state["case"].summarize())
                    .replace("{precedents}", render_precedents(state.get("precedents") or []))
                    .replace("{tool_history}", _render_tool_history(merged))
                )
                try:
                    response = llm.complete(system, user)
                except LLMUnavailable as error:
                    # Not fatal to the case: investigation is evidence gathering, and
                    # `propose_resolution` can still run on what was gathered. Recorded so
                    # a degraded run is visible rather than looking like a short one.
                    trace += traced("investigate", f"model unavailable: {error}")
                    break

                try:
                    decision = json.loads(extract_json(response.text))
                except (json.JSONDecodeError, ValueError):
                    trace += traced("investigate", "unparseable tool decision; stopping")
                    break

                if decision.get("done") or not decision.get("tool"):
                    trace += traced(
                        "investigate", f"finished: {decision.get('why', 'no reason given')}"
                    )
                    break

                name = decision["tool"]
                args = decision.get("args") or {}
                tool = registry.get(name)
                if tool is None:
                    # Fed back rather than dropped: the model can correct itself, and a
                    # request for a tool that does not exist is a fact about the prompt.
                    new_calls.append({
                        "tool": name, "args": args, "ok": False,
                        "result": {"error": f"no such tool; available: {sorted(registry)}"},
                    })
                    trace += traced("investigate", f"unknown tool {name!r}")
                    continue

                try:
                    result = tool(**args)
                    ok = True
                except TypeError as error:
                    result = {"error": f"bad arguments: {error}"}
                    ok = False
                except Exception as error:  # noqa: BLE001 - a tool must never kill the run
                    result = {"error": f"{type(error).__name__}: {error}"}
                    ok = False
                new_calls.append({"tool": name, "args": args, "ok": ok, "result": result})
                trace += traced("investigate", f"{name}({json.dumps(args)}) ok={ok}")
            else:
                trace += traced("investigate", f"reached the {MAX_TOOL_CALLS}-call cap")

        return {"tool_calls": new_calls, "trace": trace}

    return investigate


# --------------------------------------------------------------------------------------
# propose_resolution / revise


def _propose_user_prompt(state: InvestigationState, feedback: str | None) -> str:
    _, user_template = _load("resolve/v1.md")
    user = user_template.replace("{case_summary}", state["case"].summarize()).replace(
        "{precedents}", render_precedents(state.get("precedents") or [])
    )
    evidence = _render_tool_history(state)
    user += f"\n\n**Evidence gathered during investigation**\n\n{evidence}"
    if feedback:
        user += (
            "\n\n**Your previous answer failed verification.** Correct it. Do not restate "
            f"the same reasoning:\n{feedback}"
        )
    return user


def make_propose_resolution(llm: LLMClient, revising: bool = False):
    node_name = "revise" if revising else "propose_resolution"

    def propose_resolution(state: InvestigationState) -> dict:
        system, _ = _load("resolve/v1.md")
        feedback = "\n".join(state.get("verification_notes") or []) if revising else None
        user = _propose_user_prompt(state, feedback)

        try:
            response = llm.complete(system, user)
        except LLMUnavailable as error:
            return {
                "proposal": None,
                "reason_code": ReasonCode.ESCALATED_MODEL_UNAVAILABLE,
                "escalated": True,
                "confidence": 0.0,
                "rationale": str(error),
                "revisions": state.get("revisions", 0) + (1 if revising else 0),
                "trace": traced(node_name, f"model unavailable: {error}"),
            }

        try:
            payload = json.loads(extract_json(response.text))
            proposal = ProposedResolution.model_validate(payload)
        except (json.JSONDecodeError, ValueError, ValidationError) as error:
            return {
                "proposal": None,
                "reason_code": ReasonCode.ESCALATED_PARSE_FAILURE,
                "escalated": True,
                "confidence": 0.0,
                "rationale": f"could not parse a resolution: {error}",
                "revisions": state.get("revisions", 0) + (1 if revising else 0),
                "trace": traced(node_name, f"parse failure: {type(error).__name__}"),
            }

        return {
            "proposal": proposal,
            "revisions": state.get("revisions", 0) + (1 if revising else 0),
            "trace": traced(
                node_name,
                f"{proposal.reason_code.value} @ {proposal.confidence:.2f}, "
                f"cites {proposal.cited_precedent_ids or 'nothing'}",
            ),
        }

    return propose_resolution


# --------------------------------------------------------------------------------------
# verify — deterministic


def verify(state: InvestigationState) -> dict:
    """Spec §7's two checks, both in code. See `prompts/verify/v1.md`."""
    proposal = state.get("proposal")
    if proposal is None:
        return {
            "verified": False,
            "verification_notes": ["no proposal to verify"],
            "trace": traced("verify", "nothing to verify"),
        }

    case = state["case"]
    notes: list[str] = []

    # 1. Does the arithmetic close to the paise? Reuses domain.money's tolerance rather than
    #    inventing a second one, so the verifier and the matcher cannot disagree.
    settled = case.net_settlement_paise()
    credited = case.credited_paise()
    expected = case.expected_paise()
    code = proposal.reason_code

    if code in (ReasonCode.EXACT_MATCH, ReasonCode.NETTED_SETTLEMENT,
                ReasonCode.SPLIT_PAYMENT):
        if abs(settled - credited) > ROUNDING_TOLERANCE_PAISE:
            notes.append(
                f"{code.value} claims the credit is accounted for, but the payments settle "
                f"to {settled} paise against {credited} credited — a gap of "
                f"{settled - credited} paise that no fee explains."
            )

    if code is ReasonCode.EXACT_MATCH and len(case.ledger_entries) > 1:
        # Arithmetic closure is not sufficient here, which is why this check is separate.
        # In a split payment the single payment settles to the credit *exactly*, so the
        # gap test above passes and `exact_match` sails through — while leaving the second
        # invoice open. The customer has paid in full and is then chased for the remainder.
        #
        # Found from the zero-shot arm's errors in the Ring 2 run: five of its six false
        # resolutions were split payments called `exact_match` at 0.85-0.95 confidence.
        # This is a definitional rule (an exact match is one payment against one invoice),
        # not a threshold fitted to those cases.
        notes.append(
            f"{code.value} matches one payment to one invoice, but this order has "
            f"{len(case.ledger_entries)} open ledger entries. Closing it as an exact match "
            f"would leave {len(case.ledger_entries) - 1} of them outstanding against a "
            f"customer who has paid in full."
        )
    if code is ReasonCode.TDS_SHORT_PAYMENT:
        gross_paid = sum(p.amount_paise for p in case.payments)
        if expected <= gross_paid:
            notes.append(
                f"{code.value} requires the invoice to exceed what the customer paid, but "
                f"the ledger expects {expected} paise against {gross_paid} paid."
            )
    if code is ReasonCode.REFUND_NETTED and settled - credited <= 0:
        notes.append(
            f"{code.value} requires the credit to fall short of what the payments settle "
            f"to, but {credited} paise arrived against {settled} expected."
        )
    if code is ReasonCode.DUPLICATE_PAYMENT_REJECTED:
        orders = {p.order_id for p in case.payments}
        if len(case.payments) < 2 or len(orders) != 1:
            notes.append(
                f"{code.value} requires two or more payments on one order; this case has "
                f"{len(case.payments)} payment(s) across {len(orders)} order(s)."
            )
    if code is ReasonCode.DIRECT_NEFT_BYPASS and case.payments:
        notes.append(
            f"{code.value} requires no processor payment behind the credit, but "
            f"{len(case.payments)} payment(s) exist."
        )

    # 2. Do the cited precedents actually apply? Structural, and honest about its limits:
    #    this catches invention and contradiction, not subtle inapplicability.
    retrieved = {h.record.precedent_id: h.record for h in (state.get("precedents") or [])}
    for pid in proposal.cited_precedent_ids:
        record = retrieved.get(pid)
        if record is None:
            notes.append(f"cited precedent {pid} was never retrieved — it was invented.")
        elif record.reason_code != code.value:
            notes.append(
                f"cited precedent {pid} concludes {record.reason_code!r}, which does not "
                f"support the proposed {code.value!r}."
            )

    verified = not notes
    return {
        "verified": verified,
        "verification_notes": notes,
        "trace": traced(
            "verify", "passed" if verified else f"{len(notes)} problem(s): {notes[0]}"
        ),
    }


# --------------------------------------------------------------------------------------
# route — the three-way decision


def route_after_verify(state: InvestigationState) -> str:
    """Spec §7's branch. Returns the name of the next node."""
    if state.get("escalated"):
        return "escalate"
    if state.get("verified"):
        return "finalize"
    if state.get("revisions", 0) >= MAX_REVISIONS:
        return "escalate"
    return "revise"


def make_finalize(threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD):
    def finalize(state: InvestigationState) -> dict:
        """Verified, so the only remaining question is confidence (spec §7's `route`)."""
        proposal = state["proposal"]
        if proposal.confidence < threshold:
            return {
                "reason_code": ReasonCode.ESCALATED_LOW_CONFIDENCE,
                "confidence": proposal.confidence,
                "escalated": True,
                "rationale": proposal.rationale,
                "trace": traced(
                    "route", f"below threshold ({proposal.confidence:.2f} < {threshold})"
                ),
            }
        return {
            "reason_code": proposal.reason_code,
            "confidence": proposal.confidence,
            "escalated": False,
            "rationale": proposal.rationale,
            "trace": traced("route", f"auto-resolve at {proposal.confidence:.2f}"),
        }

    return finalize


def escalate(state: InvestigationState) -> dict:
    """Terminal for everything that could not be resolved.

    Preserves a reason code already set by a failing model call; otherwise the case got here
    by failing verification twice, which is `escalated_verify_failed`.
    """
    if state.get("escalated") and state.get("reason_code"):
        return {"trace": traced("escalate", state["reason_code"].value)}
    proposal = state.get("proposal")
    return {
        "reason_code": ReasonCode.ESCALATED_VERIFY_FAILED,
        "confidence": proposal.confidence if proposal else 0.0,
        "escalated": True,
        "rationale": "; ".join(state.get("verification_notes") or ["verification failed"]),
        "trace": traced("escalate", "verify failed after the revision limit"),
    }
