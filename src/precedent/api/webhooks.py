"""`payment.captured` / `refund.processed` webhook receipt (spec §4, FR-1.2/1.3).

Always acks with 200 once the delivery is durably recorded — Razorpay retries on
anything but 2xx, and retry-storming ourselves over a signature failure or an unknown
event type would be worse than just recording the problem and moving on. A signature
failure is stored (`signature_valid=False`), never silently dropped, and never trusted
downstream: nothing reads from this table and treats it as ground truth without checking
that flag first. "Process from storage" (turning a valid, unprocessed event into a
`payments`/`refunds` row) is a separate step, not implemented in this module.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request

from precedent.adapters.razorpay.webhook_signature import verify_webhook_signature
from precedent.adapters.storage.records import WebhookEventRecord
from precedent.adapters.storage.repositories import WebhookEventsRepository
from precedent.api.deps import get_connection
from precedent.config import razorpay_config

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/razorpay")
async def receive_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
    conn=Depends(get_connection),
):
    raw_body = await request.body()
    signature_valid = bool(x_razorpay_signature) and verify_webhook_signature(
        raw_body, x_razorpay_signature, razorpay_config().webhook_secret
    )

    if not x_razorpay_event_id:
        # Without the event id there is no dedupe key, so storing it would risk
        # reprocessing on Razorpay's retry. Nothing to record; still ack.
        logger.warning("Webhook delivery with no x-razorpay-event-id header; ignoring.")
        return {"status": "ok"}

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # UnicodeDecodeError is not a JSONDecodeError: `json.loads` on bytes sniffs the
        # encoding first, so a body starting with a UTF-16-looking BOM blows up during
        # decode, before any JSON parsing happens.
        payload = {}
    event_type = payload.get("event", "") if isinstance(payload, dict) else ""

    record = WebhookEventRecord(
        event_id=x_razorpay_event_id,
        event_type=event_type,
        # `errors="replace"` so a body that isn't valid UTF-8 is recorded rather than
        # crashing the handler into a 500 (which Razorpay would retry-storm). Such a body
        # cannot have come from Razorpay, and its signature check above has already
        # failed, so the lossy decode only ever affects forensics on junk we distrust.
        raw_body=raw_body.decode("utf-8", errors="replace"),
        signature_valid=signature_valid,
        received_at=datetime.now(timezone.utc).isoformat(),
    )

    repo = WebhookEventsRepository(conn)
    try:
        repo.insert_if_new(record)
    except ValueError:
        logger.warning(
            "Ignoring webhook with unsupported event_type=%r (event_id=%r)",
            event_type, x_razorpay_event_id,
        )

    return {"status": "ok"}
