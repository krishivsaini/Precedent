"""What actually arrived from Razorpay, and whether it could be trusted.

The README makes a provenance claim — *real delivery, signature-verified, deduped* — and
until this screen there was nowhere in the running system to check it. Every other surface
reports what the agent concluded; this one reports what came in the door, which is the only
input a reviewer cannot reconstruct from the database afterwards.

**Why a failed signature is set as loudly as it is.** `api/webhooks.py` acks 200 on every
delivery once it is durably recorded, including one whose signature did not verify, because
Razorpay retries on any non-2xx and retry-storming over a bad signature is worse than
recording it. That is right for the wire and wrong for a screen: from Razorpay's side a
mismatched secret looks *identical* to a correct one, so the difference has to be visible
somewhere or the claim is unfalsifiable. Here is where it becomes visible.

Nothing downstream reads an unverified row as ground truth. Neither should a reader of this
page, so the two are never set alike.
"""

from fastapi import APIRouter, Depends

from precedent.api.deps import get_connection
from precedent.api.ui import esc, page

router = APIRouter(tags=["deliveries"])


def _rows(conn):
    return conn.execute(
        """
        SELECT event_id, event_type, signature_valid, received_at, processed_at,
               length(raw_body) AS body_bytes
        FROM webhook_events
        ORDER BY received_at DESC, event_id DESC
        """
    ).fetchall()


@router.get("/deliveries")
def deliveries(conn=Depends(get_connection)):
    rows = _rows(conn)
    verified = [r for r in rows if r["signature_valid"]]
    captured = sum(1 for r in rows if r["event_type"] == "payment.captured")
    refunded = sum(1 for r in rows if r["event_type"] == "refund.processed")
    rejected = [r for r in rows if not r["signature_valid"]]

    if not rows:
        return page("Deliveries", """
        <h1>Nothing has arrived yet</h1>
        <p class="standfirst">No webhook delivery has reached this service.</p>
        <p class="empty">Point a Razorpay webhook at
        <code>/webhooks/razorpay</code>, subscribed to <code>payment.captured</code> and
        <code>refund.processed</code>, and deliveries appear here as they land — verified or
        not.</p>
        <p class="note">This service sleeps when idle. A delivery arriving cold may time out
        on Razorpay&rsquo;s first attempt; the retry lands on a warm instance, and the
        <code>event_id</code> primary key is what stops that retry being processed
        twice.</p>""", here="/deliveries")

    body = []
    for r in rows:
        ok = bool(r["signature_valid"])
        mark = ('<span class="ties">verified</span>' if ok
                else '<span class="figure-neg">did not verify</span>')
        body.append(
            f'<tr><td>{esc(r["event_type"])}<br>'
            f'<span class="who">{esc(r["event_id"])}</span></td>'
            f'<td>{mark}</td>'
            f'<td class="fig">{r["body_bytes"]:,}</td>'
            f'<td class="who">{esc((r["received_at"] or "")[:19].replace("T", " "))}</td>'
            f'</tr>'
        )

    warning = "" if not rejected else f"""
    <div class="flag stop">
      <h3>{len(rejected)} deliver{"y" if len(rejected) == 1 else "ies"} did not verify</h3>
      <p>These were recorded and acknowledged — Razorpay retries on anything but a 2xx — but
      nothing downstream treats them as evidence. The usual cause is a
      <code>RAZORPAY_WEBHOOK_SECRET</code> that does not match the secret configured on the
      webhook, character for character. A probe from anyone who found the URL looks exactly
      like this too.</p>
    </div>"""

    return page("Deliveries", f"""
    <h1>What arrived</h1>
    <p class="standfirst">{len(rows)} deliver{"y" if len(rows) == 1 else "ies"} recorded,
    {len(verified)} signature-verified.</p>
    {warning}

    <div class="tieout">
      <div>
        <h3>Trust</h3>
        <table class="ledger">
          <tr><td>Delivered</td><td class="fig">{len(rows)}</td></tr>
          <tr><td class="indent">signature did not verify</td>
              <td class="fig figure-neg">({len(rejected)})</td></tr>
          <tr class="tie"><td>Usable as evidence</td>
              <td class="fig">{len(verified)}</td></tr>
        </table>
        <p class="reads">Every delivery is stored. Only the verified ones are read as
        having come from Razorpay.</p>
      </div>
      <div>
        <h3>What kind</h3>
        <table class="ledger">
          <tr><td>Payments captured</td><td class="fig">{captured}</td></tr>
          <tr><td>Refunds processed</td><td class="fig">{refunded}</td></tr>
          <tr class="tie"><td>Total</td><td class="fig">{len(rows)}</td></tr>
        </table>
        <p class="reads">A retry never appears twice above: <code>event_id</code> is the
        primary key, so the second delivery of the same event is acknowledged and discarded
        rather than reprocessed. No counter here could show that — a tie-out between rows
        and distinct ids would be equal by construction and prove nothing.</p>
      </div>
    </div>

    <h2>Every delivery</h2>
    <table class="queue">
      <thead><tr><th>Event</th><th>Signature</th><th class="fig">Bytes</th>
      <th>Received</th></tr></thead>
      <tbody>{"".join(body)}</tbody>
    </table>
    <p class="note">Only <code>payment.captured</code> and <code>refund.processed</code> are
    accepted; any other event type is acknowledged and dropped rather than stored, so it
    will not appear here.</p>""", here="/deliveries")
