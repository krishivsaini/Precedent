"""The second gate, on screen: the ceiling, the history, and the button that spends money.

`api/remediation.py` is the JSON surface for the same use case. This module is the operator's
one, and it exists separately for the reason `product_design.md` §3.4 gives: *"the UI should
show the remediation ceiling and how much of it has been used, so 'bounded' is visible, not
just enforced server-side."* A limit the operator cannot see is a limit they cannot reason
about, and approving a refund without knowing what fraction of the day's budget it consumes
is approving in the dark.

**Why the ceiling is set as a ledger rather than a progress bar.** A progress bar says "most
of the way through" and nothing else. The three limits in `domain/remediation.py` are three
different protections — a count, a sum, and a per-call cap — and each catches a failure the
other two would let through. Rendering them as one bar would collapse exactly the distinction
that makes the ceiling worth having.

**Failure states are kept apart** (design principle 5). A refund that was refused by policy, a
refund whose outcome is unknown because the call timed out, and a refund the API rejected are
three different situations with three different right responses. The middle one is the
dangerous one, and it gets the strongest language on the screen, because money may have moved.
"""

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse

from precedent.adapters.razorpay.refunds import (
    RefundClient,
    RefundConflict,
    RefundRejected,
    RefundUnavailable,
)
from precedent.adapters.storage.repositories import RemediationsRepository
from precedent.api.deps import get_connection
from precedent.api.ui import esc, page, rupees
from precedent.config import ConfigError, razorpay_config
from precedent.domain.remediation import RemediationCeiling
from precedent.usecases.remediate import (
    RemediationRefused,
    ceiling_status,
    execute_remediation,
    propose_remediation,
)

router = APIRouter(tags=["remediation-ui"])

#: The screen is at `/refunds`, not `/remediation`: `api/remediation.py` already owns
#: `GET /remediation` as its JSON ceiling endpoint, and two routes on one path means the
#: one registered first silently wins. "Refunds" is also the word an operator would use.

GATE_ACTIONS = frozenset({"approved", "refused"})


def _history(conn):
    rows = conn.execute(
        "SELECT * FROM remediations ORDER BY created_at DESC, remediation_id DESC"
    ).fetchall()
    repo = RemediationsRepository(conn)
    return [repo.get(row["remediation_id"]) for row in rows]


def _ceiling_ledger(status: dict) -> str:
    """The budget, set the way the tie-out sets a reconciliation.

    Spent and remaining are shown on a shared axis under a rule, so "how much is left" is
    read rather than calculated.
    """
    return f"""
    <div class="tieout">
      <div>
        <h3>Money</h3>
        <table class="ledger">
          <tr><td>Budget</td>
              <td class="fig">{rupees(status['max_total_paise'])}</td></tr>
          <tr><td class="indent">already spent</td>
              <td class="fig figure-neg">{rupees(-status['total_paise'])}</td></tr>
          <tr class="tie"><td>Left to spend</td>
              <td class="fig">{rupees(status['remaining_paise'])}</td></tr>
        </table>
        <p class="reads">No single refund may exceed
        {rupees(status['max_single_paise'])}, whatever the budget allows — a per-call cap
        catches the misplaced decimal that a total never would.</p>
      </div>
      <div>
        <h3>Calls</h3>
        <table class="ledger">
          <tr><td>Refunds permitted</td>
              <td class="fig">{status['max_refunds']}</td></tr>
          <tr><td class="indent">already made</td>
              <td class="fig figure-neg">({status['refunds_made']})</td></tr>
          <tr class="tie"><td>Left</td>
              <td class="fig">{status['remaining_refunds']}</td></tr>
        </table>
        <p class="reads">{"The ceiling is exhausted. Nothing further will be sent until a "
                         "human widens it."
                         if status['exhausted'] else
                         "A count and a sum are separate limits: four small refunds and one "
                         "large one are different failures."}</p>
      </div>
    </div>"""


def _history_table(records) -> str:
    if not records:
        return ('<p class="empty">No refund has been proposed yet. Everything the agent has '
                'done so far has been an explanation, not a payment.</p>')
    rows = []
    for r in records:
        tone = {"executed": "ties", "failed": "figure-neg"}.get(r.status, "")
        detail = esc(r.refund_id or r.reason or "—")
        rows.append(
            f'<tr><td><a href="/exceptions/{esc(r.resolution_id)}">'
            f'{esc(r.resolution_id)}</a><br>'
            f'<span class="who">{esc(r.payment_id)}</span></td>'
            f'<td class="fig">{rupees(r.amount_paise)}</td>'
            f'<td class="{tone}">{esc(r.status)}</td>'
            f'<td><span class="who">{detail}</span></td>'
            f'<td class="who">{esc((r.created_at or "")[:16].replace("T", " "))}</td></tr>'
        )
    return f"""
    <table class="queue">
      <thead><tr><th>Resolution</th><th class="fig">Amount</th><th>Status</th>
      <th>Refund / reason</th><th>When</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


@router.get("/refunds")
def remediation_screen(conn=Depends(get_connection)):
    status = ceiling_status(conn, RemediationCeiling())
    records = _history(conn)
    unknown = [r for r in records if r.status == "approved" and not r.refund_id]
    warning = "" if not unknown else f"""
    <div class="flag stop">
      <h3>{len(unknown)} refund{"" if len(unknown) == 1 else "s"} with an unknown outcome</h3>
      <p>A reservation was written and no refund id came back, which means the call may have
      reached Razorpay. These keep holding their amount against the ceiling until a human
      checks the dashboard — erring this way costs coverage, and erring the other way costs
      money.</p>
    </div>"""

    return page("Remediation", f"""
    <h1>What it may spend</h1>
    <p class="standfirst">Everything else in this system produces an explanation. This is the
    only place money moves, so the limit is shown before the history, not after it.</p>
    {warning}
    {_ceiling_ledger(status)}

    <h2>Every refund proposed</h2>
    <p class="lede">Reservations are written before the network call, so a refund whose
    outcome nobody knows still leaves a row. An empty column here is not the same as nothing
    having happened.</p>
    {_history_table(records)}""", here="/refunds")


def second_gate(conn, resolution) -> str:
    """The remediation block rendered on a case, or an honest note that none is warranted.

    Rendered even when no refund is due. "This case needs no money moved" is the commonest
    answer and a real one; showing nothing at all would leave the reviewer unable to tell it
    apart from a screen that forgot to ask.
    """
    if resolution.human_action not in {"confirmed", "corrected"}:
        return ""

    try:
        proposal = propose_remediation(conn, resolution.resolution_id)
    except RemediationRefused as exc:
        return (f'<h2>Does money need to move?</h2>'
                f'<div class="flag warn"><h3>No refund can be proposed</h3>'
                f'<p>{esc(str(exc))}</p></div>')

    existing = RemediationsRepository(conn).list_by_resolution(resolution.resolution_id)
    if existing:
        r = existing[-1]
        tone = {"executed": "done", "failed": "stop"}.get(r.status, "warn")
        headline = {
            "executed": f"Refunded {rupees(r.amount_paise)}",
            "failed": "The refund did not go out",
        }.get(r.status, f"{rupees(r.amount_paise)} reserved, outcome unknown")
        body = (f"Razorpay refund <code>{esc(r.refund_id)}</code>."
                if r.refund_id else esc(r.reason or "No refund id came back."))
        return f"""
        <h2>Does money need to move?</h2>
        <div class="flag {tone}"><h3>{headline}</h3><p>{body}</p></div>"""

    if proposal is None:
        return """
        <h2>Does money need to move?</h2>
        <div class="flag done">
          <h3>No. This was a correction to the record, not a payment.</h3>
          <p>Only a duplicate charge warrants sending money back. Everything else here is
          resolved by explaining where the difference went.</p>
        </div>"""

    status = ceiling_status(conn, RemediationCeiling())
    blocked = status["exhausted"] or proposal.amount_paise > status["max_single_paise"]
    reason = ("The ceiling is exhausted." if status["exhausted"] else
              f"This exceeds the {rupees(status['max_single_paise'])} per-refund cap."
              if proposal.amount_paise > status["max_single_paise"] else "")

    actions = f"""
        <div class="actions">
          <button class="confirm" name="gate_action" value="approved">
            Approve and send {rupees(proposal.amount_paise)}</button>
          <button class="reject" name="gate_action" value="refused">Refuse</button>
        </div>""" if not blocked else f"""
        <div class="flag stop"><h3>Blocked by the ceiling</h3><p>{esc(reason)} Approving is
        not offered — a limit that can be clicked past is not a limit.</p></div>"""

    return f"""
    <h2>Does money need to move?</h2>
    <p class="lede">This is a separate authorisation from the one above. Confirming the
    diagnosis said what happened; this sends money back.</p>
    {_ceiling_ledger(status)}
    <div class="gate">
      <p class="stakes"><strong>This sends a real refund to
      {esc(proposal.payment_id)}.</strong> It consumes
      {rupees(proposal.amount_paise)} of the {rupees(status['remaining_paise'])} left in the
      budget and one of {status['remaining_refunds']} remaining calls. The request carries a
      derived idempotency key, so a retry cannot double-send.</p>
      <form method="post" action="/exceptions/{esc(resolution.resolution_id)}/remediate">
        {actions}
      </form>
    </div>"""


def _refund_client() -> RefundClient:
    """Overridable via `app.dependency_overrides` so tests never reach the network."""
    return RefundClient(razorpay_config())


@router.post("/exceptions/{resolution_id}/remediate")
def remediate(
    resolution_id: str,
    gate_action: str = Form(...),
    conn=Depends(get_connection),
):
    """Approve or refuse. Every failure below gets its own screen, on purpose.

    The client is built here rather than injected as a dependency so that a missing
    credential renders as a page explaining what is unset, instead of a 500 from dependency
    resolution that tells the operator nothing.
    """
    back = f'<p><a class="back" href="/exceptions/{esc(resolution_id)}">Back to the case</a></p>'

    if gate_action not in GATE_ACTIONS:
        return page("Not recorded", f"""
        <h1>That action was not recorded</h1>
        <p class="standfirst">Only approve and refuse are available.</p>{back}""")

    try:
        proposal = propose_remediation(conn, resolution_id)
    except RemediationRefused as exc:
        return page("Refused", f"""
        <h1>No refund can be proposed</h1>
        <p class="standfirst">{esc(str(exc))}</p>{back}""")
    if proposal is None:
        return page("Nothing to send", f"""
        <h1>This case does not warrant a refund</h1>
        <p class="standfirst">Its reason code resolves to an explanation, not a
        payment.</p>{back}""")

    try:
        client = _refund_client()
    except ConfigError as exc:
        return page("Not configured", f"""
        <h1>No Razorpay credentials</h1>
        <p class="standfirst">{esc(str(exc))}</p>
        <p class="note">Nothing was sent and nothing was reserved. This is a deployment
        problem, not a decision about the case.</p>{back}""")

    try:
        execute_remediation(
            conn, proposal, approval=gate_action, refund_client=client,
            ceiling=RemediationCeiling(), approved_by="operator",
        )
    except RefundUnavailable as exc:
        # The dangerous one. A reservation exists and the outcome is unknown, so the copy
        # says so plainly rather than inviting a retry that feels free.
        return page("Outcome unknown", f"""
        <h1>The refund call did not come back</h1>
        <p class="standfirst">{esc(str(exc))}</p>
        <p class="note">A reservation was written before the call, so this amount keeps
        counting against the ceiling. Money may have moved. Check the Razorpay dashboard
        before doing anything else — retrying is safe only under the same derived key, which
        is what the reservation preserves.</p>
        <p><a class="back" href="/refunds">See the ceiling and history</a></p>{back}""")
    except RefundConflict as exc:
        return page("Conflict", f"""
        <h1>That key already reached Razorpay</h1>
        <p class="standfirst">{esc(str(exc))}</p>
        <p class="note">A request under this idempotency key was sent with a different body,
        so a refund may already exist. Stop and look rather than retrying.</p>{back}""")
    except RefundRejected as exc:
        return page("Rejected", f"""
        <h1>Razorpay rejected the refund</h1>
        <p class="standfirst">{esc(str(exc))}</p>
        <p class="note">The request was refused, so no money moved.</p>{back}""")
    except RemediationRefused as exc:
        return page("Refused by policy", f"""
        <h1>The ceiling refused this</h1>
        <p class="standfirst">{esc(str(exc))}</p>
        <p class="note">Nothing was sent. The limit did what it exists to do.</p>{back}""")

    return RedirectResponse(f"/exceptions/{resolution_id}", status_code=303)
