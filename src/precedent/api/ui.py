"""The approval screen (product_design.md §3.2, §3.2a, §4; spec §9 Ring 3.2).

Server-rendered, no build step, no external assets.

**The design idea.** A reconciliation is two columns that should agree and do not, and the
whole craft is finding where the difference went. So the screen is built as a worked
statement rather than a dashboard: the tie-out is the hero, set the way accounting sets it —
labels left, figures on a shared decimal axis, a rule above each subtotal and a double rule
above the figure that has to come out at zero. The palette is columnar-pad paper. Everything
is one serif family, controls included, because a financial statement is a document and
dressing it as an app would misrepresent what the reviewer is being asked to do.

**Five things here are requirements, not taste**, each argued in the design doc:

1. **Retrieved precedents appear above the proposal.** The reviewer should judge what the
   system thought was *similar* before seeing what it *concluded*. Verdict-first anchors them.
2. **The arithmetic is a chain, not a verdict.** The bank credit is net of the processor's
   fee; the ledger's expectation is gross. Two different comparisons, shown side by side. One
   "matches / does not match" line is what makes reconciliation UIs untrustworthy, because the
   reviewer cannot then separate a genuine shortfall from a fee deduction.
3. **Exactly three actions.** No snooze, no "later" — an unresolved exception is routed to the
   exception list, not left ambiguous.
4. **The stakes sit next to the button, not in a banner.** §3.2a: a confirmed wrong resolution
   is deposited and then retrieved to justify future wrong ones. A reviewer who knows their
   click becomes durable knowledge behaves differently from one closing a ticket.
5. **Failure states are distinct.** §3.2b: low confidence, verification failed twice, and
   model-unavailable are three different things. Rendering them identically teaches the
   reviewer to distrust all three.
"""

import html
import json
from pathlib import Path

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from precedent.adapters.storage.records import (
    BankLineRecord,
    LedgerEntryRecord,
    PaymentRecord,
)
from precedent.adapters.storage.repositories import (
    BankLinesRepository,
    ExceptionsRepository,
    PaymentsRepository,
    PrecedentsRepository,
    ResolutionsRepository,
)
from precedent.api.approvals import _now
from precedent.api.deps import get_connection
from precedent.domain.case import ReconciliationCase, format_paise
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD

router = APIRouter(tags=["ui"])

#: Committed eval results, read straight off disk. The screens that report numbers read the
#: same JSON the eval wrote, so a figure on the page cannot drift from the run that produced
#: it — the same rule `evals/report.py` is built on.
RESULTS_DIR = Path(__file__).resolve().parents[3] / "evals" / "results"


def latest_result(pattern: str) -> dict | None:
    matches = sorted(RESULTS_DIR.glob(pattern))
    if not matches:
        return None
    try:
        return json.loads(matches[-1].read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


NAV = (
    ("/", "Queue"),
    ("/learns", "How it learns"),
    ("/corpus", "Corpus"),
    ("/result", "Does it work"),
)

REASON_CHOICES = (
    "exact_match", "tolerance_rounding", "date_window_timing", "netted_settlement",
    "direct_neft_bypass", "tds_short_payment", "split_payment", "refund_netted",
    "negotiated_rebate", "advance_adjusted", "duplicate_payment_rejected",
    "unmatchable_no_counterpart",
)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def rupees(paise: int) -> str:
    """Figures without the unit repeated on every line — the column header carries it, the
    way a statement does. Negatives in parentheses, as accounting sets them."""
    sign = paise < 0
    rupee, paisa = divmod(abs(paise), 100)
    body = f"{rupee:,}.{paisa:02d}"
    return f"({body})" if sign else body


STYLE = """
:root {
  --paper:    #F2F4F0;   /* columnar pad */
  --card:     #FBFCFA;
  --rule:     #C9D2C6;   /* the green bar line */
  --ink:      #1B2019;   /* near-black, green cast */
  --muted:    #67705F;
  --debit:    #8E2B22;   /* ledger red */
  --credit:   #2C5F2D;
  --focus:    #1B4D8F;
  color-scheme: light;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.6 "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua",
        Georgia, serif;
}
.sheet { max-width: 58rem; margin: 0 auto; padding: 0 1.5rem 5rem; }

header.masthead {
  display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
  padding: 1.4rem 0 .9rem; border-bottom: 2px solid var(--ink); margin-bottom: 2rem;
  flex-wrap: wrap;
}
.masthead .name { font-size: 1.05rem; letter-spacing: .01em; font-weight: 600; }
.masthead a { color: var(--ink); text-decoration: none; }
.masthead a:hover { text-decoration: underline; }
.masthead .count { color: var(--muted); font-size: .92rem; }
.masthead nav { display: flex; gap: 1.35rem; flex: 1; margin-left: .6rem; flex-wrap: wrap; }
.masthead nav a { color: var(--muted); font-size: .95rem; padding-bottom: .15rem; }
.masthead nav a.on { color: var(--ink); font-weight: 600;
                     box-shadow: inset 0 -2px 0 var(--ink); }
.masthead nav a:hover { color: var(--ink); text-decoration: none; }

/* --- headline figure: used once, on the result screen --- */
.headline { display: grid; grid-template-columns: auto 1fr; gap: 0 1.6rem;
            align-items: baseline; margin: .2rem 0 .3rem; }
.headline .big { font-size: 4.1rem; line-height: 1; font-weight: 600;
                 font-variant-numeric: tabular-nums lining; letter-spacing: -.02em; }
.headline .from { color: var(--muted); font-size: 1.02rem; max-width: 26rem; }
.claim { font-size: 1.12rem; line-height: 1.5; max-width: 40rem; margin: 1.6rem 0 0;
         padding-left: 1.1rem; border-left: 3px solid var(--ink); }
.figures { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
           gap: 1.6rem 2.2rem; margin: 2rem 0 .5rem; }
.figures div { border-top: 1px solid var(--ink); padding-top: .55rem; }
.figures .n { font-size: 1.5rem; font-variant-numeric: tabular-nums lining;
              font-weight: 600; display: block; }
.figures .l { color: var(--muted); font-size: .89rem; }

/* --- before / after --- */
.beforeafter { display: grid; grid-template-columns: 1fr 1fr; gap: 0 2.4rem;
               align-items: start; margin: .6rem 0 0; }
.beforeafter > div + div { border-left: 1px solid var(--rule); padding-left: 2.4rem; }
.stamp { font-weight: 600; margin: 0 0 .15rem; }
.stamp.no { color: var(--debit); }
.stamp.yes { color: var(--credit); }
.beforeafter .when { color: var(--muted); font-size: .89rem; margin: 0 0 .8rem; }
.arrow { text-align: center; color: var(--muted); margin: 1.6rem 0; font-size: .95rem; }

/* --- corpus --- */
.corpus-filters { display: flex; gap: 1.1rem; flex-wrap: wrap; align-items: baseline;
                  margin: 0 0 1.2rem; font-size: .93rem; }
.corpus-filters a { color: var(--muted); }
.corpus-filters a.on { color: var(--ink); font-weight: 600; }
.pill { font-size: .78rem; color: var(--muted); border: 1px solid var(--rule);
        padding: .06rem .4rem; border-radius: 2px; white-space: nowrap; }
.pill.new { color: var(--credit); border-color: var(--credit); }

h1 { font-size: 2.1rem; line-height: 1.15; margin: 0 0 .25rem; font-weight: 600;
     letter-spacing: -.012em; }
.standfirst { color: var(--muted); margin: 0 0 2.2rem; font-size: 1rem; }
h2 { font-size: 1.12rem; font-weight: 600; margin: 2.6rem 0 .9rem; }
h2 + .lede { color: var(--muted); margin: -.6rem 0 1rem; font-size: .95rem;
             max-width: 46rem; }
h3 { font-size: .98rem; font-weight: 600; margin: 0 0 .5rem; }

/* --- the tie-out: two facing columns, which is what a reconciliation is --- */
/* No outer box. A bordered container leaves the shorter column sitting in dead space and
   reads as an empty card; a single rule between the two reads as a spread, which is what a
   reconciliation statement is. */
.tieout { display: grid; grid-template-columns: 1fr 1fr; gap: 0 2.4rem;
          margin: .4rem 0 .6rem; align-items: start; }
.tieout > div + div { border-left: 1px solid var(--rule); padding-left: 2.4rem; }
.ledger { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums lining; }
.ledger td { padding: .28rem 0; border: 0; }
.ledger td.fig { text-align: right; white-space: nowrap; padding-left: 1rem; }
.ledger tr.sub td { border-top: 1px solid var(--ink); padding-top: .42rem; }
.ledger tr.tie td { border-top: 1px solid var(--ink);
                    box-shadow: inset 0 3px 0 -2px var(--ink); padding-top: .5rem;
                    font-weight: 600; }
.ledger td.indent { padding-left: 1.1rem; color: var(--muted); }
.figure-neg { color: var(--debit); }
.ties { color: var(--credit); }
.reads { color: var(--muted); font-size: .9rem; margin: .5rem 0 0; max-width: 46rem; }

/* --- precedents, set as citations --- */
/* A precedent *is* a citation, so it is set like one: hanging indent, a reference mark in
   the margin, no box. Three identical bordered cards would be the generic treatment and
   would say nothing about what the content is. */
ol.citations { list-style: none; counter-reset: cite; margin: 0; padding: 0; }
ol.citations li { counter-increment: cite; position: relative; padding: 0 0 1.1rem 2.6rem;
                  margin-bottom: 1.1rem; border-bottom: 1px solid var(--rule);
                  max-width: 48rem; }
ol.citations li:last-child { border-bottom: 0; margin-bottom: 0; }
ol.citations li::before { content: "[" counter(cite) "]"; position: absolute; left: 0;
                          top: 0; color: var(--muted);
                          font-variant-numeric: tabular-nums; }
.cite-head { display: flex; justify-content: space-between; gap: 1rem;
             align-items: baseline; flex-wrap: wrap; margin-bottom: .3rem; }
.cite-head .code { font-weight: 600; }
.cite-head .prov { color: var(--muted); font-size: .88rem; }
ol.citations p { margin: .3rem 0 0; }
ol.citations .lbl { color: var(--muted); }

/* --- proposal --- */
.proposal { background: var(--card); border: 1px solid var(--rule);
            border-left: 3px solid var(--ink); padding: 1.1rem 1.25rem; }
.proposal .verdict { font-size: 1.15rem; font-weight: 600; margin: 0 0 .2rem; }
.proposal .conf { color: var(--muted); font-size: .93rem; margin: 0 0 .8rem; }
.proposal p.rationale { margin: 0; max-width: 46rem; }

.flag { border: 1px solid var(--rule); padding: 1rem 1.15rem; margin: 0 0 1rem;
        background: var(--card); }
.flag.warn  { border-left: 3px solid #9A6A00; }
.flag.stop  { border-left: 3px solid var(--debit); }
.flag.done  { border-left: 3px solid var(--credit); }
.flag p { margin: .3rem 0 0; max-width: 46rem; }

/* --- gate --- */
.gate { margin-top: 1.2rem; border-top: 2px solid var(--ink); padding-top: 1.2rem; }
.stakes { max-width: 44rem; margin: 0 0 1.1rem; }
.actions { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
button, .btn { font: inherit; font-size: .97rem; padding: .55rem 1.15rem;
               border: 1px solid var(--ink); background: var(--card); color: var(--ink);
               cursor: pointer; border-radius: 2px; }
button.confirm { background: var(--ink); color: var(--paper); }
button.reject  { border-color: #B79A97; color: var(--debit); }
button:hover { background: var(--ink); color: var(--paper); }
button.reject:hover { background: var(--debit); border-color: var(--debit); color: #fff; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }

details.correct { margin-top: 1rem; border-top: 1px solid var(--rule); padding-top: .9rem; }
details.correct summary { cursor: pointer; font-weight: 600; }
details.correct .why { color: var(--muted); font-size: .93rem; max-width: 44rem;
                       margin: .4rem 0 .8rem; }
.field { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
select, input[type=text] { font: inherit; font-size: .95rem; padding: .5rem .55rem;
                           border: 1px solid var(--rule); background: var(--card);
                           color: var(--ink); border-radius: 2px; }
input[type=text] { min-width: 18rem; flex: 1; }

/* --- queue --- */
table.queue { width: 100%; border-collapse: collapse; margin-top: .5rem; }
table.queue th { text-align: left; font-weight: 600; padding: .5rem 1.6rem .5rem 0;
                 border-bottom: 1px solid var(--ink); }
table.queue td { padding: .62rem 1.6rem .62rem 0; border-bottom: 1px solid var(--rule);
                 vertical-align: baseline; }
/* Figures right-align on a shared axis, headers included — and keep their gutter, or the
   next column runs straight into them. */
table.queue th.fig, table.queue td.fig { text-align: right;
                                         font-variant-numeric: tabular-nums lining; }
table.queue th:last-child, table.queue td:last-child { padding-right: 0; }
table.queue tr:hover td { background: var(--card); }
table.queue a { color: var(--ink); text-decoration: none; font-weight: 600; }
table.queue a:hover { text-decoration: underline; }
.who { color: var(--muted); }
.empty { padding: 3rem 0; color: var(--muted); max-width: 40rem; }
.note { color: var(--muted); font-size: .9rem; max-width: 46rem; }
.back { display: inline-block; margin: 1.4rem 0 0; color: var(--muted); }

@media (max-width: 46rem) {
  .tieout { grid-template-columns: 1fr; }
  .tieout > div + div { border-left: 0; border-top: 1px solid var(--rule); }
  h1 { font-size: 1.7rem; }
}
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def page(title: str, body: str, here: str = "", waiting: int | None = None) -> HTMLResponse:
    """One shell for every screen, so the demo is a single app rather than a set of pages."""
    links = "".join(
        '<a href="%s"%s>%s</a>' % (href, ' class="on"' if href == here else "", esc(label))
        for href, label in NAV
    )
    count = f'<span class="count">{waiting} waiting</span>' if waiting is not None else ""
    return HTMLResponse(
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} — Precedent</title><style>{STYLE}</style>"
        "<body><div class='sheet'>"
        "<header class='masthead'>"
        "<span class='name'><a href='/'>Precedent</a></span>"
        f'<nav>{links}</nav>{count}</header>'
        f"{body}</div>"
    )


def _case_for(conn, member_refs: list[str]) -> ReconciliationCase:
    """Rebuild the case from the exception's member references.

    Each id is looked up across all three stores rather than parsed for a type prefix: ids are
    opaque here, and guessing from a prefix would silently drop a record whose naming
    convention changed.
    """
    payments, lines, entries = [], [], []
    payment_repo, line_repo = PaymentsRepository(conn), BankLinesRepository(conn)
    for ref in member_refs:
        payment = payment_repo.get(ref)
        if isinstance(payment, PaymentRecord):
            payments.append(payment)
            continue
        line = line_repo.get(ref)
        if isinstance(line, BankLineRecord):
            lines.append(line)
            continue
        row = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_id = ?", (ref,)
        ).fetchone()
        if row:
            entries.append(LedgerEntryRecord(
                entry_id=row["entry_id"], order_id=row["order_id"],
                expected_amount_paise=row["expected_amount_paise"],
                invoice_no=row["invoice_no"], customer_name=row["customer_name"],
                terms=row["terms"],
            ))
    return ReconciliationCase("case", payments, lines, entries)


def _row(label: str, paise: int, cls: str = "", indent: bool = False) -> str:
    fig = rupees(paise)
    tone = " figure-neg" if paise < 0 else ""
    return (f'<tr class="{cls}"><td class="{"indent" if indent else ""}">{esc(label)}</td>'
            f'<td class="fig{tone}">{fig}</td></tr>')


def _tieout(case: ReconciliationCase) -> str:
    """The reconciliation, as two facing columns.

    Left: what should have reached the bank, net of the processor's fee. Right: what the
    customer owed against what they actually paid, both gross. Keeping them apart is the
    point — collapsing them hides which of the two is short.
    """
    gross = sum(p.amount_paise for p in case.payments)
    fee = sum(p.fee_paise for p in case.payments)
    tax = sum(p.tax_paise for p in case.payments)
    settles, landed = case.net_settlement_paise(), case.credited_paise()
    expected = case.expected_paise()
    bank_gap, customer_gap = settles - landed, expected - gross

    def verdict(gap: int, whole: str, short: str) -> str:
        return (f'<span class="ties">{whole}</span>' if gap == 0
                else f'<span class="figure-neg">{short}</span>')

    return f"""
    <div class="tieout">
      <div>
        <h3>At the bank</h3>
        <table class="ledger">
          {_row("Payments captured, gross", gross)}
          {_row("processor fee", -fee, indent=True)}
          {_row("tax on the fee", -tax, indent=True)}
          {_row("Should have landed", settles, cls="sub")}
          {_row("Actually landed", landed)}
          {_row("Unexplained", bank_gap, cls="tie")}
        </table>
        <p class="reads">{verdict(bank_gap, "The credit ties out.",
                                  "The credit is short by more than the fee explains.")}</p>
      </div>
      <div>
        <h3>Against the invoice</h3>
        <table class="ledger">
          {_row("Ledger expects, gross", expected)}
          {_row("Customer paid, gross", gross)}
          {_row("Kept back", customer_gap, cls="tie")}
        </table>
        <p class="reads">{verdict(customer_gap, "The customer paid in full.",
                                  "The customer withheld part of the invoice.")}</p>
      </div>
    </div>
    <p class="note">Two comparisons, kept apart on purpose. The bank credit is net of the
    processor&rsquo;s fee; the invoice is gross. A single &ldquo;matches&rdquo; line would
    hide which side is short.</p>
    """


def _precedents(records) -> str:
    if not records:
        return ('<p class="note">Nothing in the corpus looked similar. The agent reasoned '
                'from the case alone.</p>')
    out = []
    for record in records:
        origin = ("written by hand at set-up" if not record.derived_from_resolution
                  else f"deposited from {esc(record.derived_from_resolution)}")
        out.append(f"""
        <li>
          <div class="cite-head">
            <span class="code">{esc(record.reason_code.replace("_", " "))}</span>
            <span class="prov">{origin} &middot; trusted at
            {record.confidence_at_deposit:.2f}</span>
          </div>
          <p><span class="lbl">Seen before.</span> {esc(record.situation)}</p>
          <p><span class="lbl">Done then.</span> {esc(record.resolution)}</p>
        </li>""")
    return '<ol class="citations">' + "".join(out) + "</ol>"


def _draft_is_trustworthy(resolution) -> bool:
    """Whether the proposal may be presented as something to confirm.

    A draft that failed verification twice, or that came from an outage or an unparseable
    response, is evidence of what the agent thought — not a recommendation (§3.2b). The gate
    reads this so the *prominent* action matches the warning: an earlier version told the
    reviewer to resolve the case themselves and then offered Confirm as the primary button,
    which is the interface arguing against itself.
    """
    reason = (resolution.rationale or "").lower()
    if "unavailable" in reason or "could not be reached" in reason or "parse" in reason:
        return False
    return bool(resolution.verified)


def _flag(resolution) -> str:
    """§3.2b — three different failures, presented differently."""
    reason = (resolution.rationale or "").lower()
    if "unavailable" in reason or "could not be reached" in reason:
        return ('<div class="flag stop"><h3>The model could not be reached</h3>'
                '<p>No proposal exists for this case. This is an outage, not a judgement — '
                'nothing below should be read as the system declining to answer. Retry, or '
                'resolve it yourself.</p></div>')
    if "parse" in reason:
        return ('<div class="flag stop"><h3>The model returned unusable output</h3>'
                '<p>A response came back and could not be read as a resolution. Treat this '
                'case as unexamined.</p></div>')
    if not resolution.verified:
        return ('<div class="flag stop"><h3>The arithmetic did not close, twice</h3>'
                '<p>The draft below was checked, revised, and failed again. It is evidence '
                'of what the agent thought, not a recommendation. Resolve this one '
                'yourself.</p></div>')
    if resolution.confidence < DEFAULT_AUTO_RESOLVE_THRESHOLD:
        return (f'<div class="flag warn"><h3>Below the bar for resolving on its own</h3>'
                f'<p>The agent proposed this at {resolution.confidence:.2f}, under the '
                f'{DEFAULT_AUTO_RESOLVE_THRESHOLD:.2f} threshold set from measured outcomes, '
                f'so it came here instead. The reasoning is complete; the confidence is '
                f'what fell short.</p></div>')
    return ""


@router.get("/", response_class=HTMLResponse)
def queue(conn=Depends(get_connection)):
    rows = conn.execute(
        """
        SELECT e.exception_id, e.kind, e.detected_at, e.member_refs,
               r.resolution_id, r.confidence, r.verified
        FROM exceptions e JOIN resolutions r ON r.exception_id = e.exception_id
        WHERE r.human_action IS NULL
        ORDER BY e.detected_at ASC
        """
    ).fetchall()
    decided = conn.execute(
        "SELECT COUNT(*) n FROM resolutions WHERE human_action IS NOT NULL"
    ).fetchone()["n"]

    if not rows:
        return page("Queue", f"""
        <h1>Nothing to review</h1>
        <p class="standfirst">Every exception the agent could not settle on its own has been
        decided.</p>
        <p class="empty">{decided} resolution{"" if decided == 1 else "s"} reviewed so far.
        New exceptions appear here as reconciliation runs.</p>""", waiting=0)

    import json as _json
    items = []
    for row in rows:
        try:
            refs = _json.loads(row["member_refs"])
        except (ValueError, TypeError):
            refs = []
        case = _case_for(conn, refs)
        who = next((e.customer_name for e in case.ledger_entries if e.customer_name), "—")
        at_stake = case.expected_paise() or sum(p.amount_paise for p in case.payments)
        items.append(
            f'<tr><td><a href="/exceptions/{esc(row["resolution_id"])}">'
            f'{esc(row["kind"].replace("_", " "))}</a><br>'
            f'<span class="who">{esc(who)}</span></td>'
            f'<td class="fig">{rupees(at_stake)}</td>'
            f'<td class="fig">{row["confidence"]:.2f}</td>'
            f'<td>{"checked" if row["verified"] else "did not close"}</td></tr>'
        )

    return page("Queue", f"""
    <h1>{len(rows)} to review</h1>
    <p class="standfirst">Exceptions the agent would not settle on its own. Oldest first —
    working newest-first starves the hard ones, and those are the cases worth recording.</p>
    <table class="queue">
      <thead><tr><th>Case</th><th class="fig">At stake</th>
      <th class="fig">Confidence</th><th>Arithmetic</th></tr></thead>
      <tbody>{"".join(items)}</tbody>
    </table>
    <p class="note">{decided} already decided.</p>""", waiting=len(rows))


@router.get("/exceptions/{resolution_id}", response_class=HTMLResponse)
def detail(resolution_id: str, conn=Depends(get_connection)):
    resolution = ResolutionsRepository(conn).get(resolution_id)
    if resolution is None:
        return page("Not found", f"""
        <h1>No such case</h1>
        <p class="standfirst">Nothing here matches {esc(resolution_id)}.</p>
        <p><a class="back" href="/">Back to the queue</a></p>""")

    exception = ExceptionsRepository(conn).get(resolution.exception_id)
    precedents = PrecedentsRepository(conn)
    cited = [r for r in (precedents.get(p) for p in resolution.cited_precedents) if r]
    missing = [p for p in resolution.cited_precedents if precedents.get(p) is None]
    case = _case_for(conn, exception.member_refs if exception else [])

    who = next((e.customer_name for e in case.ledger_entries if e.customer_name), None)
    invoice = next((e.invoice_no for e in case.ledger_entries if e.invoice_no), None)
    detail_line = " · ".join(
        p for p in (
            f"invoice {esc(invoice)}" if invoice else None,
            f"seen {esc(exception.detected_at[:10])}" if exception else None,
        ) if p
    )

    decided = ""
    if resolution.human_action:
        deposited = resolution.human_action in {"confirmed", "corrected"}
        decided = f"""
        <div class="flag done">
          <h3>You {esc(resolution.human_action)} this</h3>
          <p>{"A precedent was written from it and is now in the corpus."
              if deposited else "Nothing was added to the corpus."}
          Recorded as {esc(resolution.resolution_id)}.</p>
        </div>"""

    trustworthy = _draft_is_trustworthy(resolution)
    options = "".join(
        f'<option value="{esc(c)}">{esc(c.replace("_", " "))}</option>'
        for c in REASON_CHOICES
    )
    stakes = (
        "<strong>Confirming writes this into the corpus.</strong> It will be found and cited "
        "on future cases that look like this one, so a confirmation that is wrong does not "
        "cost one record — it becomes something the system reasons from. Decide on the "
        "figures and the precedents above, not on the verdict."
        if trustworthy else
        "<strong>This draft has not earned a confirmation.</strong> Whatever you record here "
        "is written into the corpus and cited on future cases, so give it the answer you "
        "would want a colleague to inherit — not the one already on the screen."
    )
    # When the draft is untrustworthy the correction form is open and primary, and Confirm is
    # demoted. Telling the reviewer to resolve it themselves while making Confirm the easiest
    # click is the interface arguing against itself.
    correction = f"""
        <details class="correct"{" open" if not trustworthy else ""}>
          <summary>{"Record the right answer" if not trustworthy
                    else "Correct it instead"}</summary>
          <p class="why">A correction is worth more than a confirmation. It records a case the
          agent got wrong, which is the precedent most likely to stop the same mistake
          happening again.</p>
          <div class="field">
            <select name="corrected_reason_code" aria-label="What it should have been">
              <option value="">What it should have been</option>
              {options}
            </select>
            <input type="text" name="correction_note" placeholder="What the agent missed"
                   aria-label="What the agent missed">
            <button class="{"confirm" if not trustworthy else ""}"
                    name="human_action" value="corrected">Correct and deposit</button>
          </div>
        </details>"""
    confirm_button = (
        '<button class="confirm" name="human_action" value="confirmed">Confirm and '
        'deposit</button>' if trustworthy else
        '<button name="human_action" value="confirmed">Confirm anyway</button>'
    )

    gate = "" if resolution.human_action else f"""
    <div class="gate">
      <p class="stakes">{stakes}</p>
      <form method="post" action="/exceptions/{esc(resolution_id)}/decide">
        <div class="actions">
          {confirm_button}
          <button class="reject" name="human_action" value="rejected">Reject</button>
        </div>
        {correction}
      </form>
    </div>"""

    return page("Case", f"""
    <h1>{esc(who) if who else esc(exception.kind if exception else "Exception")}</h1>
    <p class="standfirst">{detail_line or esc(resolution.exception_id)}</p>
    {decided}

    <h2>Where the money went</h2>
    {_tieout(case)}

    <h2>What the system had seen before</h2>
    <p class="lede">Shown ahead of the proposal so you can judge whether these cases really
    are alike, before knowing what was concluded from them.</p>
    {_precedents(cited)}
    {f'<p class="note">Cited but no longer in the corpus: {esc(", ".join(missing))}.</p>'
     if missing else ""}

    <h2>What it proposes</h2>
    {_flag(resolution)}
    <div class="proposal">
      <p class="verdict">{esc(exception.kind.replace("_", " ") if exception else "—")}</p>
      <p class="conf">Confidence {resolution.confidence:.2f} ·
      arithmetic {"checked" if resolution.verified else "did not close"}</p>
      <p class="rationale">{esc(resolution.rationale)}</p>
    </div>
    {gate}
    <a class="back" href="/">Back to the queue</a>""")


@router.post("/exceptions/{resolution_id}/decide")
def decide(
    resolution_id: str,
    human_action: str = Form(...),
    corrected_reason_code: str = Form(""),
    correction_note: str = Form(""),
    conn=Depends(get_connection),
):
    """Record the decision, then redirect so a refresh cannot re-submit it.

    Validation is repeated from `api/approvals.py` rather than delegated, because on a screen
    a form post that silently does nothing is worse than an error — and a correction with no
    corrected code would deposit the agent's own answer labelled as a human correction.
    """
    if human_action not in {"confirmed", "corrected", "rejected"}:
        return page("Not recorded", f"""
        <h1>That action was not recorded</h1>
        <p class="standfirst">Only confirm, correct and reject are available.</p>
        <p><a class="back" href="/exceptions/{esc(resolution_id)}">Back to the case</a></p>""")
    if human_action == "corrected" and not corrected_reason_code:
        return page("Correction incomplete", f"""
        <h1>Choose what it should have been</h1>
        <p class="standfirst">A correction needs the right answer attached.</p>
        <p class="note">Without one, the agent&rsquo;s own answer would be written into the
        corpus labelled as your correction — the worst of both.</p>
        <p><a class="back" href="/exceptions/{esc(resolution_id)}">Back to the case</a></p>""")

    ResolutionsRepository(conn).record_human_action(
        resolution_id=resolution_id,
        human_action=human_action,
        corrected_payload=(
            {"reason_code": corrected_reason_code, "note": correction_note}
            if human_action == "corrected" else None
        ),
        resolved_at=_now(),
    )
    return RedirectResponse(f"/exceptions/{resolution_id}", status_code=303)
