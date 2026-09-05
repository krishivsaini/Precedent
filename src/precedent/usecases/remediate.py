"""Bounded remediation — the only thing in this project that moves money (spec §9 Ring 5).

Everything else here writes explanations. A wrong explanation is caught by the next
reviewer; a wrong refund has already left. So this module is built around refusing, and the
happy path is the short branch.

**Two gates, deliberately not one.** The resolution gate (Ring 3) asks *is this the right
explanation?* This one asks *should we move this money?* They are different questions with
different blast radii, and collapsing them would mean that confirming an explanation
silently authorised a payment. So a confirmed resolution grants exactly nothing here: a
remediation needs its own approval, recorded against its own row, and a reviewer who
confirms the diagnosis is free to refuse the refund that follows from it.

**Narrow by construction.** Only `duplicate_payment_rejected` is remediable. Every other
reason code in the vocabulary describes a bookkeeping outcome — a shortfall explained, a
settlement netted — where the correct action is to record what happened, not to send money.
A duplicate is the one class where the customer is genuinely holding money that is not
theirs. Widening this set is a decision someone should have to make on purpose, so it is a
constant rather than a heuristic.

**The stopping rule is enforced against storage, not memory** — see `domain.remediation`
and the `remediations` table. A ceiling that a process restart clears is not a ceiling.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from precedent.adapters.razorpay.refunds import (
    RefundClient,
    RefundConflict,
    RefundRejected,
    RefundUnavailable,
)
from precedent.adapters.storage.records import AuditLogRecord, RemediationRecord
from precedent.adapters.storage.repositories import (
    AuditLogRepository,
    ExceptionsRepository,
    PaymentsRepository,
    RemediationsRepository,
    ResolutionsRepository,
)
from precedent.domain.reasons import ReasonCode
from precedent.domain.reasons import reason_code_for
from precedent.domain.remediation import (
    CeilingUsage,
    RemediationCeiling,
    check_ceiling,
    refund_idempotency_key,
)

#: The one reason code that warrants moving money. See the module docstring — this is a
#: policy decision, written as a constant so that widening it is a visible edit.
REMEDIABLE_REASON_CODES = frozenset({ReasonCode.DUPLICATE_PAYMENT_REJECTED.value})

#: The human actions on the *resolution* gate that make a remediation eligible to be
#: proposed. Note that eligibility is not authorisation: the remediation gate still has to
#: approve separately.
RESOLVED_ACTIONS = frozenset({"confirmed", "corrected"})


class RemediationRefused(RuntimeError):
    """The refund was not attempted, because policy says it must not be.

    Deliberately distinct from `RefundUnavailable`: a refusal is a correct outcome and must
    never be retried, while an unavailable API is a bad day and must be. Conflating them
    would let a caller's retry loop grind against a policy decision — or, worse, let a
    policy refusal look like a transient blip that might clear on its own.
    """


@dataclass(frozen=True)
class RemediationProposal:
    resolution_id: str
    exception_id: str
    payment_id: str
    amount_paise: int
    reason_code: str
    correlation_id: str
    idempotency_key: str


@dataclass(frozen=True)
class RemediationOutcome:
    remediation_id: str | None
    refund_id: str | None
    amount_paise: int
    executed: bool
    replayed: bool
    reason: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _reason_code_of(resolution) -> str:
    """The corrected code when a human corrected it, empty otherwise.

    Same rule as the deposit path, and for the same reason: if a reviewer said the
    diagnosis was wrong, the diagnosis they replaced it with is the one that governs — most
    of all when the consequence is a refund. A correction can therefore both *create* a
    refund (a case relabelled as a duplicate) and *cancel* one (a duplicate relabelled as
    something else), which is the whole point of letting the human overrule.

    Empty for a confirmation, because `corrected_payload` is only populated on corrections
    (schema §resolutions); the caller falls back to the exception's own kind.
    """
    if resolution.human_action == "corrected" and resolution.corrected_payload:
        return resolution.corrected_payload.get("reason_code") or ""
    return ""


def propose_remediation(conn, resolution_id: str, reason_code: str | None = None):
    """What a refund for this resolution *would* be, or `None` if none is warranted.

    Proposing is free of consequence and deliberately separate from executing: the gate has
    to render the amount and the ceiling before anyone approves, and building that view must
    not be able to send a request by accident.

    `reason_code` is a parameter because the resolutions table stores the code inside
    `corrected_payload` only for corrections — an agent's own code lives on the exception's
    `kind`. The caller passes it when it knows better.
    """
    resolution = ResolutionsRepository(conn).get(resolution_id)
    if resolution is None:
        raise RemediationRefused(f"no resolution {resolution_id}")
    if resolution.human_action not in RESOLVED_ACTIONS:
        raise RemediationRefused(
            f"{resolution_id} is {resolution.human_action or 'unreviewed'}; a refund needs a "
            "confirmed or corrected resolution behind it before it can even be proposed"
        )

    exception = ExceptionsRepository(conn).get(resolution.exception_id)
    if exception is None:
        raise RemediationRefused(f"resolution {resolution_id} has no exception behind it")

    # Resolved rather than compared raw. The three sources disagree on vocabulary: a
    # caller's override and a reviewer's correction are `ReasonCode` values, while an
    # agent's own classification is the exception's `kind` — and `duplicate_payment` is the
    # kind whose code is `duplicate_payment_rejected`. Comparing the kind directly meant an
    # agent-classified duplicate charge could never be proposed for a refund, which made
    # this entire use case unreachable without a reviewer correcting the code by hand.
    resolved = reason_code_for(reason_code or _reason_code_of(resolution) or exception.kind)
    if resolved is None or resolved.value not in REMEDIABLE_REASON_CODES:
        return None
    code = resolved.value

    payment_id, amount_paise = _duplicate_payment_to_refund(conn, exception)
    if payment_id is None:
        raise RemediationRefused(
            f"{exception.exception_id} is a {code} but no duplicate payment could be "
            "identified from its members; refusing to guess which one to refund"
        )

    return RemediationProposal(
        resolution_id=resolution_id,
        exception_id=exception.exception_id,
        payment_id=payment_id,
        amount_paise=amount_paise,
        reason_code=code,
        correlation_id=exception.correlation_id,
        idempotency_key=refund_idempotency_key(resolution_id, payment_id, amount_paise),
    )


def _duplicate_payment_to_refund(conn, exception) -> tuple[str | None, int]:
    """Which of a duplicate's payments to send back: the later capture.

    The earlier one is the payment that settled the invoice. Refunding it instead would
    leave the invoice open and the customer paid — arithmetically identical, operationally
    the wrong way round. Ordered by capture time, and by payment id as a tiebreak so the
    choice is deterministic when two captures share a timestamp.
    """
    payments = PaymentsRepository(conn)
    members = [
        record
        for record in (payments.get(ref) for ref in exception.member_refs)
        if record is not None
    ]
    if len(members) < 2:
        return None, 0
    members.sort(key=lambda p: (p.captured_at, p.payment_id))
    later = members[-1]
    return later.payment_id, later.amount_paise


def execute_remediation(
    conn,
    proposal: RemediationProposal,
    approval: str,
    refund_client: RefundClient,
    ceiling: RemediationCeiling | None = None,
    approved_by: str = "operator",
    now=_now,
) -> RemediationOutcome:
    """Fire the refund, or refuse and say why.

    The reservation row is written *before* the network call, so that a crash between the
    two leaves evidence: an 'approved' row with no refund id is the trace of a refund whose
    outcome nobody knows, and it keeps holding its amount against the ceiling until a human
    looks.

    **This is the one place in the codebase that commits for itself**, against the
    repository contract that the caller owns the transaction. It has to. `api.deps`
    scopes a transaction to the HTTP request and rolls it back on any exception — so under
    the ordinary convention, a refund that timed out would take its own reservation down
    with it, leaving money possibly moved and no record that it was ever attempted. That is
    precisely the failure the reservation exists to prevent, and it was a live bug here
    until an API-level test caught it: four rows that should have survived came back zero.

    A reservation that is not durable is not a reservation, so the commit is part of the
    mechanism rather than a convenience.
    """
    ceiling = ceiling or RemediationCeiling()
    remediations = RemediationsRepository(conn)
    audit = AuditLogRepository(conn)

    def record(stage: str, reason: str) -> None:
        audit.append(AuditLogRecord(
            correlation_id=proposal.correlation_id, stage=stage, actor=approved_by,
            reason=reason, created_at=now(),
            input_digest=proposal.idempotency_key,
        ))

    # 1. The second gate. A confirmed resolution buys nothing here.
    if approval != "approved":
        remediations.insert(_row(proposal, "refused", approved_by,
                                 f"operator {approval}", now()))
        record("gated", f"remediation refused by operator: {approval}")
        return RemediationOutcome(None, None, proposal.amount_paise, False, False,
                                  f"refused at the remediation gate: {approval}")

    # 2. Local idempotency, before anything leaves the process.
    existing = remediations.get_by_idempotency_key(proposal.idempotency_key)
    if existing is not None:
        if existing.status == "executed":
            return RemediationOutcome(
                existing.remediation_id, existing.refund_id, existing.amount_paise,
                True, True, "already refunded; returning the original refund",
            )
        raise RemediationRefused(
            f"a remediation for this intent already exists as {existing.remediation_id} "
            f"with status {existing.status!r}; resolve it before trying again"
        )

    # 3. The ceiling, computed from storage so a restart cannot widen it.
    refunds_made, total_paise = remediations.usage()
    decision = check_ceiling(
        ceiling, CeilingUsage(refunds_made, total_paise), proposal.amount_paise
    )
    if not decision.allowed:
        remediations.insert(_row(proposal, "refused", approved_by, decision.reason, now()))
        record("gated", f"remediation refused by ceiling: {decision.reason}")
        return RemediationOutcome(None, None, proposal.amount_paise, False, False,
                                  decision.reason)

    # 4. Reserve, then call. Never the other way round.
    remediation_id = f"rem_{uuid.uuid4().hex[:12]}"
    remediations.insert(RemediationRecord(
        remediation_id=remediation_id, resolution_id=proposal.resolution_id,
        payment_id=proposal.payment_id, amount_paise=proposal.amount_paise,
        idempotency_key=proposal.idempotency_key, status="approved",
        approved_by=approved_by, reason=decision.reason,
        correlation_id=proposal.correlation_id, created_at=now(),
    ))
    # Durable before the request leaves the process — see the docstring. Everything after
    # this point is recovering from an outcome we may not learn.
    conn.commit()

    try:
        result = refund_client.create_refund(
            payment_id=proposal.payment_id,
            amount_paise=proposal.amount_paise,
            idempotency_key=proposal.idempotency_key,
            notes={
                "resolution_id": proposal.resolution_id,
                "exception_id": proposal.exception_id,
                "reason_code": proposal.reason_code,
            },
        )
    except RefundConflict as exc:
        # The row stays 'approved'. A 409 means a request under this key already reached
        # Razorpay with a different body, so a refund may exist that this row does not
        # describe — releasing the reservation would let the same budget be spent again.
        record("acted", f"refund conflict, reservation held: {exc}")
        conn.commit()
        raise
    except RefundUnavailable as exc:
        # Outcome unknown. Same reasoning: hold the reservation.
        record("acted", f"refund outcome unknown, reservation held: {exc}")
        conn.commit()
        raise
    except RefundRejected as exc:
        # The API said no, in so many words. Nothing moved, so the budget goes back — but
        # the attempt is still recorded, committed here because the caller's transaction is
        # about to be rolled back by the exception on its way out.
        remediations.mark_failed(remediation_id, str(exc))
        record("acted", f"refund rejected, reservation released: {exc}")
        conn.commit()
        raise

    remediations.mark_executed(remediation_id, result.refund_id, now())
    record("acted", json.dumps({
        "refund_id": result.refund_id, "amount_paise": result.amount_paise,
        "payment_id": result.payment_id, "status": result.status,
    }))
    return RemediationOutcome(
        remediation_id, result.refund_id, result.amount_paise, True, False,
        f"refunded {result.amount_paise} paise as {result.refund_id}",
    )


def _row(proposal: RemediationProposal, status: str, approved_by: str,
         reason: str, created_at: str) -> RemediationRecord:
    return RemediationRecord(
        remediation_id=f"rem_{uuid.uuid4().hex[:12]}",
        resolution_id=proposal.resolution_id, payment_id=proposal.payment_id,
        amount_paise=proposal.amount_paise, idempotency_key=proposal.idempotency_key,
        status=status, approved_by=approved_by, reason=reason,
        correlation_id=proposal.correlation_id, created_at=created_at,
    )


def ceiling_status(conn, ceiling: RemediationCeiling | None = None) -> dict:
    """What the gate shows above the Approve button.

    An operator approving a refund needs to know how much of the budget it consumes; a
    screen that shows only the amount makes every refund look like the first one.
    """
    ceiling = ceiling or RemediationCeiling()
    refunds_made, total_paise = RemediationsRepository(conn).usage()
    return {
        "max_refunds": ceiling.max_refunds,
        "max_total_paise": ceiling.max_total_paise,
        "max_single_paise": ceiling.max_single_paise,
        "refunds_made": refunds_made,
        "total_paise": total_paise,
        "remaining_refunds": max(0, ceiling.max_refunds - refunds_made),
        "remaining_paise": max(0, ceiling.max_total_paise - total_paise),
        "exhausted": refunds_made >= ceiling.max_refunds
        or total_paise >= ceiling.max_total_paise,
    }
