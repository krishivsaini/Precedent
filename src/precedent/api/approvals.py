"""The endpoints behind the approval screen (spec §7's gate, Ring 3.2).

The gate is durable and tested, but until now had no human-facing surface: a paused
resolution could only be resumed from Python. These are the three operations an operator
needs — see what is waiting, see one case in full, decide it — and nothing more.

**Why this is thin.** Everything that matters already lives behind it. The decision is
validated by `resume_gate`, the deposit rules by `usecases.deposit`, the transaction
boundary by `api.deps`. An endpoint that re-implemented any of those would be a second place
for the rules to drift, and the rule that matters most — a corpus must never deposit
unreviewed output — is one this layer must not be able to weaken.

**No auth, single operator**, as `product_design.md` §3.2 scopes it. That is a deliberate
omission rather than an oversight, and it is the first thing that would have to change before
this ran anywhere real: these endpoints let a caller confirm a state-changing financial
resolution.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from precedent.adapters.storage.repositories import (
    ExceptionsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.api.deps import get_connection
from precedent.graph.investigation import HUMAN_ACTIONS

router = APIRouter(prefix="/approvals", tags=["approvals"])


@dataclass(frozen=True)
class Decision:
    """What an operator sends back. Mirrors the gate's resume payload exactly."""

    human_action: str
    corrected_reason_code: str | None = None
    correction_note: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("")
def list_pending(conn=Depends(get_connection)) -> dict:
    """Exceptions awaiting a decision, oldest first.

    Oldest first because an approval queue worked newest-first quietly starves its hardest
    items, and the hardest items are the ones a precedent corpus most needs resolved.
    """
    rows = conn.execute(
        """
        SELECT e.exception_id, e.kind, e.detected_at, e.correlation_id,
               r.resolution_id, r.confidence, r.rationale, r.verified
        FROM exceptions e
        JOIN resolutions r ON r.exception_id = e.exception_id
        WHERE r.human_action IS NULL
        ORDER BY e.detected_at ASC
        """
    ).fetchall()
    return {
        "pending": [dict(row) for row in rows],
        "count": len(rows),
    }


@router.get("/{resolution_id}")
def get_one(resolution_id: str, conn=Depends(get_connection)) -> dict:
    """One case in full: the proposal, its evidence, and what it cites.

    The cited precedents are returned in full rather than as ids. A reviewer asked to confirm
    a resolution *because three precedents support it* cannot do that without reading them,
    and a screen that shows only ids turns the gate into a rubber stamp — which is precisely
    the failure mode the whole deposit rule exists to prevent.
    """
    resolution = ResolutionsRepository(conn).get(resolution_id)
    if resolution is None:
        raise HTTPException(status_code=404, detail=f"no resolution {resolution_id}")

    exception = ExceptionsRepository(conn).get(resolution.exception_id)
    precedents = PrecedentsRepository(conn)
    cited = [
        record for record in (precedents.get(pid) for pid in resolution.cited_precedents)
        if record is not None
    ]

    return {
        "resolution": {
            "resolution_id": resolution.resolution_id,
            "confidence": resolution.confidence,
            "rationale": resolution.rationale,
            "verified": resolution.verified,
            "human_action": resolution.human_action,
        },
        "exception": asdict(exception) if exception else None,
        "cited_precedents": [
            {
                "precedent_id": r.precedent_id,
                "situation": r.situation,
                "resolution": r.resolution,
                "reason_code": r.reason_code,
                "confidence_at_deposit": r.confidence_at_deposit,
                # Shown because a precedent the system wrote about itself should be visible
                # as such to whoever is deciding whether to trust it.
                "derived_from_resolution": r.derived_from_resolution,
            }
            for r in cited
        ],
        "missing_cited_precedents": [
            pid for pid in resolution.cited_precedents
            if precedents.get(pid) is None
        ],
    }


@router.post("/{resolution_id}")
def decide(resolution_id: str, decision: dict, conn=Depends(get_connection)) -> dict:
    """Record an operator's decision.

    Validation is deliberately strict and deliberately duplicated from the gate: this is the
    boundary where a state-changing financial action is authorised, and an endpoint that
    accepted a malformed decision would be relying on a layer below it to notice.
    """
    action = (decision or {}).get("human_action")
    if action not in HUMAN_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"human_action must be one of {sorted(HUMAN_ACTIONS)}; got {action!r}",
        )
    corrected = (decision or {}).get("corrected_reason_code")
    if action == "corrected" and not corrected:
        raise HTTPException(
            status_code=422,
            detail="a corrected decision must carry corrected_reason_code — otherwise the "
                   "agent's original answer is deposited under the label of a correction",
        )

    resolutions = ResolutionsRepository(conn)
    existing = resolutions.get(resolution_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no resolution {resolution_id}")
    if existing.human_action is not None:
        # Not an error the operator can fix by retrying, and not something to silently
        # overwrite: a second decision on a resolution that already deposited would create a
        # precedent with no matching review.
        raise HTTPException(
            status_code=409,
            detail=f"{resolution_id} was already {existing.human_action}",
        )

    resolutions.record_human_action(
        resolution_id=resolution_id,
        human_action=action,
        corrected_payload=(
            {"reason_code": corrected, "note": (decision or {}).get("correction_note", "")}
            if action == "corrected" else None
        ),
        resolved_at=_now(),
    )
    return {
        "resolution_id": resolution_id,
        "human_action": action,
        "deposits": action in {"confirmed", "corrected"},
    }
