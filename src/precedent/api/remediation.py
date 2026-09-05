"""The remediation gate — the second gate, and the only one behind which money moves.

Kept in its own router rather than added to `approvals.py` on purpose. The two gates answer
different questions (*is this the right explanation?* / *should we move this money?*) and a
single endpoint that did both would make confirming a diagnosis into an authorisation to
pay. Separate routes make that separation something a reader can see, and something a
reverse proxy could enforce differently if this ever ran anywhere real.

`GET /remediation` is the ceiling: how much budget is left before the agent stops. It is the
first thing this screen shows, because an operator approving a refund without knowing what
fraction of the day's budget it consumes is approving in the dark.

**No auth, single operator**, exactly as `product_design.md` §3.2 scopes it — and the
omission is more serious here than on the approval screen, because these endpoints send
money. It is the first thing that would have to change before this ran anywhere real.
"""

from fastapi import APIRouter, Depends, HTTPException

from precedent.adapters.razorpay.refunds import (
    RefundClient,
    RefundConflict,
    RefundRejected,
    RefundUnavailable,
)
from precedent.adapters.storage.repositories import RemediationsRepository
from precedent.api.deps import get_connection
from precedent.config import razorpay_config
from precedent.domain.remediation import RemediationCeiling
from precedent.usecases.remediate import (
    RemediationRefused,
    ceiling_status,
    execute_remediation,
    propose_remediation,
)

router = APIRouter(prefix="/remediation", tags=["remediation"])

#: Approvals are spelled out rather than boolean. `{"approve": false}` and a missing key
#: look identical to a JSON parser after a typo; `"approved"` / `"refused"` do not.
GATE_ACTIONS = frozenset({"approved", "refused"})


def _refund_client() -> RefundClient:
    """Overridable via `app.dependency_overrides` so tests never reach the network."""
    return RefundClient(razorpay_config())


def _ceiling() -> RemediationCeiling:
    return RemediationCeiling()


@router.get("")
def get_ceiling(
    conn=Depends(get_connection), ceiling: RemediationCeiling = Depends(_ceiling)
) -> dict:
    """What is left of the budget, and everything already spent against it."""
    status = ceiling_status(conn, ceiling)
    status["history"] = [
        {
            "remediation_id": r.remediation_id, "resolution_id": r.resolution_id,
            "payment_id": r.payment_id, "amount_paise": r.amount_paise,
            "status": r.status, "refund_id": r.refund_id, "reason": r.reason,
            "created_at": r.created_at,
        }
        for r in _all_remediations(conn)
    ]
    return status


def _all_remediations(conn):
    rows = conn.execute(
        "SELECT * FROM remediations ORDER BY created_at DESC, remediation_id DESC"
    ).fetchall()
    repo = RemediationsRepository(conn)
    return [repo.get(row["remediation_id"]) for row in rows]


@router.get("/{resolution_id}")
def get_proposal(
    resolution_id: str,
    conn=Depends(get_connection),
    ceiling: RemediationCeiling = Depends(_ceiling),
) -> dict:
    """What a refund for this resolution would be — without sending one.

    Returns `remediable: false` rather than 404 when the reason code does not warrant a
    refund. "This case needs no money moved" is a real answer and the commonest one; a 404
    would make it look like a lookup failure.
    """
    try:
        proposal = propose_remediation(conn, resolution_id)
    except RemediationRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if proposal is None:
        return {
            "resolution_id": resolution_id,
            "remediable": False,
            "reason": "this resolution's reason code does not warrant moving money",
            "ceiling": ceiling_status(conn, ceiling),
        }
    return {
        "resolution_id": resolution_id,
        "remediable": True,
        "payment_id": proposal.payment_id,
        "amount_paise": proposal.amount_paise,
        "reason_code": proposal.reason_code,
        "idempotency_key": proposal.idempotency_key,
        "ceiling": ceiling_status(conn, ceiling),
    }


@router.post("/{resolution_id}")
def decide(
    resolution_id: str,
    decision: dict,
    conn=Depends(get_connection),
    refund_client: RefundClient = Depends(_refund_client),
    ceiling: RemediationCeiling = Depends(_ceiling),
) -> dict:
    """Approve or refuse the refund. This is the endpoint that spends money."""
    action = (decision or {}).get("gate_action")
    if action not in GATE_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"gate_action must be one of {sorted(GATE_ACTIONS)}; got {action!r}",
        )

    try:
        proposal = propose_remediation(conn, resolution_id)
    except RemediationRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if proposal is None:
        raise HTTPException(
            status_code=422,
            detail=f"{resolution_id} does not warrant a refund; nothing to approve",
        )

    try:
        outcome = execute_remediation(
            conn, proposal, approval=action, refund_client=refund_client,
            ceiling=ceiling, approved_by=(decision or {}).get("approved_by", "operator"),
        )
    except RefundConflict as exc:
        # 409 out, mirroring the API's own answer: a request under this key already
        # reached Razorpay with a different body, so a refund may exist. Stop and look.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundUnavailable as exc:
        # 503, because the client may retry — and must, under the same derived key, which
        # is the only reason retrying is safe.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RefundRejected as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RemediationRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "resolution_id": resolution_id,
        "remediation_id": outcome.remediation_id,
        "refund_id": outcome.refund_id,
        "amount_paise": outcome.amount_paise,
        "executed": outcome.executed,
        "replayed": outcome.replayed,
        "reason": outcome.reason,
        "ceiling": ceiling_status(conn, ceiling),
    }
