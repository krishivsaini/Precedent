"""Ring 5's exit criterion: a real test-mode refund, fired end-to-end through the gate.

Everything below talks to the live Razorpay test-mode API. No mock, no stub, no recorded
response — the refund ids this prints can be looked up in the dashboard.

It runs the three cases that matter, in order:

1. **The default ceiling refuses a full-value refund.** Nothing is sent. This is the case
   worth showing first: the interesting behaviour of a spending limit is the refusal.
2. **A widened ceiling lets the same refund through.** Widening is explicit and printed,
   because a limit that some code path can quietly raise is not a limit.
3. **The same intent, replayed.** Returns the original refund id without a second call —
   the property that makes a crashed-and-retried remediation safe.

The exception it acts on is constructed: these two payments are real captures made for this
project, but they were not a real duplicate. The refund is real; the story around it is
staged, and saying so is cheaper than having someone discover it.

    uv run python scripts/fire_remediation.py [--db precedent.db] [--dry-run]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from precedent.adapters.razorpay.refunds import RefundClient, RefundConflict, RefundUnavailable
from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import ExceptionRecord, ResolutionRecord
from precedent.adapters.storage.repositories import (
    ExceptionsRepository,
    PaymentsRepository,
    RemediationsRepository,
    ResolutionsRepository,
)
from precedent.config import razorpay_config
from precedent.domain.remediation import RemediationCeiling
from precedent.usecases.remediate import (
    ceiling_status,
    execute_remediation,
    propose_remediation,
)

RESULTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "results"

EXCEPTION_ID = "exc_ring5_demo"
RESOLUTION_ID = "res_ring5_demo"


def rupees(paise: int) -> str:
    return f"INR {paise / 100:,.2f}"


def seed_exception(conn) -> tuple[str, int]:
    """Stage a duplicate-payment exception over the real captured payments."""
    payments = PaymentsRepository(conn)
    real = [p for p in _all_payments(conn) if p.source == "razorpay"]
    if len(real) < 2:
        raise SystemExit(
            f"need two real captured payments to stage a duplicate; found {len(real)}. "
            "Run scripts/ingest_test_payment.py first."
        )
    real.sort(key=lambda p: (p.captured_at, p.payment_id))
    later = real[-1]

    exceptions = ExceptionsRepository(conn)
    if exceptions.get(EXCEPTION_ID) is None:
        exceptions.insert(ExceptionRecord(
            exception_id=EXCEPTION_ID, batch_id="ring5", kind="duplicate_payment_rejected",
            member_refs=[p.payment_id for p in real], detected_at=_now(), status="open",
            correlation_id="corr_ring5_demo",
        ))
    resolutions = ResolutionsRepository(conn)
    if resolutions.get(RESOLUTION_ID) is None:
        resolutions.insert(ResolutionRecord(
            resolution_id=RESOLUTION_ID, exception_id=EXCEPTION_ID, proposed_by="agent",
            confidence=0.97,
            rationale="Two captures against the same customer for one invoice; the later "
                      "one is unmatched and should go back.",
            cited_precedents=[], verified=True,
        ))
        resolutions.record_human_action(
            resolution_id=RESOLUTION_ID, human_action="confirmed", resolved_at=_now(),
        )
    conn.commit()
    return later.payment_id, later.amount_paise


def _all_payments(conn):
    repo = PaymentsRepository(conn)
    rows = conn.execute("SELECT payment_id FROM payments").fetchall()
    return [repo.get(row["payment_id"]) for row in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="precedent.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="run case 1 only — the refusal, which sends nothing")
    args = parser.parse_args()

    conn = connect(args.db)
    init_db(conn)
    payment_id, amount_paise = seed_exception(conn)

    proposal = propose_remediation(conn, RESOLUTION_ID)
    if proposal is None:
        raise SystemExit("the staged exception did not propose a refund; nothing to fire")

    print(f"proposal: refund {rupees(proposal.amount_paise)} on {proposal.payment_id}")
    print(f"          idempotency key {proposal.idempotency_key}\n")

    client = RefundClient(razorpay_config())
    record = {
        "run_id": _now(),
        "what_this_demonstrates": (
            "A real Razorpay test-mode refund fired through the remediation gate, the "
            "default ceiling refusing the same refund, and an idempotent replay. The "
            "duplicate-payment exception is staged over two real captures."
        ),
        "payment_id": proposal.payment_id,
        "amount_paise": proposal.amount_paise,
        "idempotency_key": proposal.idempotency_key,
        "cases": [],
    }

    # 1. The default ceiling refuses it. Nothing is sent.
    default_ceiling = RemediationCeiling()
    refused = execute_remediation(conn, proposal, "approved", client, default_ceiling)
    print(f"[1] default ceiling  -> executed={refused.executed}  {refused.reason}")
    record["cases"].append({
        "case": "default_ceiling_refuses", "ceiling": _ceiling_dict(default_ceiling),
        "executed": refused.executed, "reason": refused.reason, "refund_id": None,
    })
    if refused.executed:
        raise SystemExit("the default ceiling did not refuse a full-value refund")

    if args.dry_run:
        print("\n--dry-run: stopping before anything is sent.")
        conn.commit()
        return 0

    # 2. Widened deliberately, and said out loud.
    widened = RemediationCeiling(
        max_refunds=3,
        max_total_paise=amount_paise * 2,
        max_single_paise=amount_paise,
    )
    print(f"[2] widening the ceiling to {rupees(widened.max_single_paise)} per refund, "
          f"{rupees(widened.max_total_paise)} total — deliberately, for this demonstration")
    try:
        fired = execute_remediation(conn, proposal, "approved", client, widened)
    except (RefundConflict, RefundUnavailable) as exc:
        print(f"    refund did not complete: {exc}")
        conn.commit()
        record["cases"].append({"case": "live_refund", "error": str(exc)})
        _write(record)
        return 1
    print(f"    executed={fired.executed}  refund_id={fired.refund_id}  {fired.reason}")
    record["cases"].append({
        "case": "live_refund", "ceiling": _ceiling_dict(widened),
        "executed": fired.executed, "refund_id": fired.refund_id,
        "replayed": fired.replayed, "reason": fired.reason,
    })

    # 3. The same intent again. No second refund.
    replayed = execute_remediation(conn, proposal, "approved", client, widened)
    print(f"[3] replay           -> refund_id={replayed.refund_id}  "
          f"replayed={replayed.replayed}  (no second call)")
    record["cases"].append({
        "case": "idempotent_replay", "executed": replayed.executed,
        "replayed": replayed.replayed, "refund_id": replayed.refund_id,
        "same_refund_as_first": replayed.refund_id == fired.refund_id,
    })

    status = ceiling_status(conn, widened)
    rows = RemediationsRepository(conn).list_by_resolution(RESOLUTION_ID)
    print(f"\nceiling now: {status['refunds_made']}/{status['max_refunds']} refunds, "
          f"{rupees(status['total_paise'])} of {rupees(status['max_total_paise'])} spent")
    print(f"remediation rows: {[(r.status, r.refund_id) for r in rows]}")
    record["ceiling_after"] = status
    conn.commit()
    _write(record)
    return 0


def _ceiling_dict(ceiling: RemediationCeiling) -> dict:
    return {
        "max_refunds": ceiling.max_refunds,
        "max_total_paise": ceiling.max_total_paise,
        "max_single_paise": ceiling.max_single_paise,
    }


def _write(record: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = RESULTS_DIR / f"remediation-{stamp}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {path}")


if __name__ == "__main__":
    sys.exit(main())
