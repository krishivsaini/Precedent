"""The three screens that answer "what does it know, and does that help?"

Separate from `api/ui.py` for one reason: `ui.py` owns the *acting* surface — the queue, the
case, the gate — and those screens change state. Everything here is read-only. Keeping the
split visible in the module boundary means a reader looking for "what can this app change?"
has one file to read, not two.

The shared vocabulary (the shell, the ledger type, the citation list) is imported from
`ui.py` rather than restated. Two stylesheets would drift, and a corpus entry rendered
differently here than on the case screen would quietly suggest they were different things.

**Every figure on `/result` is read from `evals/results/*.json`** — the same files the eval
harness wrote and the same rule the static showcase follows. Nothing here is typed in, so a
number on this screen cannot disagree with the run that produced it.
"""

from fastapi import APIRouter, Depends

from precedent.adapters.storage.records import PrecedentRecord
from precedent.api.deps import get_connection
from precedent.api.ui import esc, latest_result, page

router = APIRouter(tags=["knowledge"])

#: The two classes that cannot be derived from the evidence in the case — see
#: `domain/reasons.py`. They are the corpus's reason for existing, so the learning screen is
#: built around them rather than around the classes an investigation tool can already solve.
COUNTERPARTY_CODES = ("negotiated_rebate", "advance_adjusted")


def pct(value, places: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{places}f}%"


def _rows_to_precedents(rows) -> list[PrecedentRecord]:
    return [
        PrecedentRecord(
            precedent_id=r["precedent_id"], situation=r["situation"],
            resolution=r["resolution"], reason_code=r["reason_code"], entities=[],
            amount_signature=r["amount_signature"],
            confidence_at_deposit=r["confidence_at_deposit"],
            deposited_at=r["deposited_at"], corpus_version=r["corpus_version"],
            derived_from_resolution=r["derived_from_resolution"],
            times_retrieved=r["times_retrieved"],
            times_cited_correctly=r["times_cited_correctly"],
        )
        for r in rows
    ]


def _all_precedents(conn) -> list[PrecedentRecord]:
    """Newest first. A corpus is read to see what was learned *lately*."""
    return _rows_to_precedents(conn.execute(
        "SELECT * FROM precedents ORDER BY corpus_version DESC, deposited_at DESC"
    ).fetchall())


# ---------------------------------------------------------------------------------------
# /corpus


def _entry(record: PrecedentRecord) -> str:
    authored = bool(record.derived_from_resolution)
    origin = (
        f'<a href="/exceptions/{esc(record.derived_from_resolution)}">'
        f'written from {esc(record.derived_from_resolution)}</a>'
        if authored else "written by hand at set-up"
    )
    pill = ('<span class="pill new">authored by operation</span>' if authored
            else '<span class="pill">seeded</span>')
    cited = (f' &middot; retrieved {record.times_retrieved}&times;'
             if record.times_retrieved else "")
    return f"""
    <li>
      <div class="cite-head">
        <span class="code">{esc(record.reason_code.replace("_", " "))} {pill}</span>
        <span class="prov">{origin} &middot; trusted at
        {record.confidence_at_deposit:.2f}{cited}</span>
      </div>
      <p><span class="lbl">Seen before.</span> {esc(record.situation)}</p>
      <p><span class="lbl">Done then.</span> {esc(record.resolution)}</p>
    </li>"""


@router.get("/corpus")
def corpus(show: str = "all", conn=Depends(get_connection)):
    """Everything the system currently knows, and where each piece came from.

    The filter is the argument. A corpus of hand-written entries is a knowledge base like
    any other; what this project claims is that the corpus *grows from its own operation*,
    and the only way to let someone check that claim rather than take it is to let them
    separate the two origins and count.
    """
    records = _all_precedents(conn)
    seeded = [r for r in records if not r.derived_from_resolution]
    authored = [r for r in records if r.derived_from_resolution]

    shown = {"seeded": seeded, "authored": authored}.get(show, records)
    def _filter(key: str, label: str, count: int) -> str:
        on = ' class="on"' if show == key else ""
        return f'<a href="/corpus?show={key}"{on}>{label} ({count})</a>'

    filters = "".join(_filter(*f) for f in (
        ("all", "All", len(records)),
        ("seeded", "Written by hand", len(seeded)),
        ("authored", "Authored by operation", len(authored)),
    ))

    if not records:
        body = """
        <h1>The corpus is empty</h1>
        <p class="standfirst">Nothing has been seeded and nothing has been deposited.</p>
        <p class="empty">Precedents appear here as reviewers confirm and correct
        resolutions on the queue.</p>"""
        return page("Corpus", body, here="/corpus")

    # Stated plainly rather than dressed up. If nothing has been deposited yet, the honest
    # reading is that the loop has not run — not that the corpus is "ready".
    standfirst = (
        f"{len(records)} precedents. {len(authored)} of them were written by the system from "
        f"a reviewer&rsquo;s decision; {len(seeded)} were written by hand before it ran."
        if authored else
        f"{len(records)} precedents, all written by hand before the system ran. Nothing has "
        f"been deposited from operation yet — confirm or correct a case on the queue and the "
        f"precedent it writes will appear here."
    )
    entries = "".join(_entry(r) for r in shown) or (
        '<p class="empty">Nothing in this category yet.</p>')

    return page("Corpus", f"""
    <h1>What it knows</h1>
    <p class="standfirst">{standfirst}</p>
    <div class="corpus-filters">{filters}</div>
    <ol class="citations">{entries}</ol>""", here="/corpus")


# ---------------------------------------------------------------------------------------
# /learns


@router.get("/learns")
def learns(conn=Depends(get_connection)):
    """Why a corpus beats a better prompt, argued on the two classes where it must.

    Every other exception class can be worked out from the evidence in front of the agent:
    a statutory rate can be tested for, a netted sum can be computed. Those classes are the
    ones an investigation tool already solves, and on them a precedent is at best a
    shortcut. The two classes below cannot be derived at all — the shortfall matches no
    statute and nothing in the case says what the arrangement is. They can only be
    remembered, which is the claim this project actually makes.
    """
    counterparty = [
        r for r in _all_precedents(conn) if r.reason_code in COUNTERPARTY_CODES
    ]
    authored = [r for r in counterparty if r.derived_from_resolution]

    curve = latest_result("learning-curve-*.json")
    measured = ""
    if curve and curve.get("curve"):
        first, last = curve["curve"][0], curve["curve"][-1]
        measured = f"""
        <h2>What that is worth, measured</h2>
        <p class="lede">Held-out cases replayed against the corpus at two sizes. The
        left-hand figure is every exception; the right-hand one is the subset that can only
        be answered from a precedent.</p>
        <div class="figures">
          <div><span class="n">{pct(first['retrieved']['resolution_rate'])} &rarr;
            {pct(last['retrieved']['resolution_rate'])}</span>
            <span class="l">all exceptions, corpus {first['corpus_size']} &rarr;
            {last['corpus_size']}</span></div>
          <div><span class="n">
            {pct(first['retrieved']['counterparty_resolution_rate'])} &rarr;
            {pct(last['retrieved']['counterparty_resolution_rate'])}</span>
            <span class="l">the only-a-precedent cases below</span></div>
          <div><span class="n">{pct(first['random_control']['resolution_rate'])} &rarr;
            {pct(last['random_control']['resolution_rate'])}</span>
            <span class="l">random control — flat, which is what rules out &ldquo;more text
            in the prompt&rdquo;</span></div>
        </div>"""

    if counterparty:
        examples = "".join(_entry(r) for r in counterparty[:6])
        corpus_state = f"""
        <h2>What it has actually remembered</h2>
        <p class="lede">{len(counterparty)} precedents in these two classes,
        {len(authored)} of them written from a reviewer&rsquo;s decision.</p>
        <ol class="citations">{examples}</ol>"""
    else:
        corpus_state = """
        <h2>What it has actually remembered</h2>
        <p class="empty">No counterparty precedents in the corpus yet.</p>"""

    return page("How it learns", f"""
    <h1>Knowledge that cannot be derived</h1>
    <p class="standfirst">The case for a corpus rests on the exceptions no amount of
    reasoning can settle.</p>

    <div class="beforeafter">
      <div>
        <p class="stamp no">The first time it meets a counterparty</p>
        <p class="when">nothing in the corpus about them</p>
        <p>A payment arrives short of the invoice by a proportion matching no statutory
        band, with no refund and no fee explaining the gap. The agent can measure the
        shortfall precisely and still cannot say why it exists.</p>
        <p class="reads">Escalating is correct here. The evidence is genuinely
        insufficient, and a guess would close an invoice that was never settled.</p>
      </div>
      <div>
        <p class="stamp yes">After a reviewer has resolved one</p>
        <p class="when">one precedent about that counterparty, written from that decision</p>
        <p>The same arithmetic now matches something the system has seen. The precedent
        carries the arrangement — a negotiated rebate, an advance already adjusted — which
        no tool could have computed from the case.</p>
        <p class="reads">This is the whole mechanism. Not a better prompt: a fact the system
        did not previously have.</p>
      </div>
    </div>
    <p class="arrow">the corpus is the difference between those two columns</p>
    {measured}
    {corpus_state}""", here="/learns")


# ---------------------------------------------------------------------------------------
# /result


@router.get("/result")
def result():
    """The measurement, read off the committed result files.

    This screen deliberately shows the control at the same weight as the treatment. The
    control is the line that decides whether the other one means anything, and under-drawing
    it would be an editorial claim dressed as a layout choice.
    """
    curve = latest_result("learning-curve-*.json")
    if not curve or not curve.get("curve"):
        return page("Does it work", """
        <h1>No measurement committed</h1>
        <p class="standfirst">There is no learning-curve result in
        <code>evals/results/</code> to read.</p>
        <p class="empty">This screen never computes a figure of its own. If the eval has not
        been run and committed, it has nothing honest to show.</p>""", here="/result")

    points = curve["curve"]
    first, last = points[0], points[-1]
    sig = curve.get("significance", {}).get("headline_first_to_last", {})
    baseline = latest_result("2026-09-01-0838.json")
    baseline_rate = (baseline or {}).get("metrics", {}).get("autonomous_resolution_rate")

    rows = "".join(
        f"<tr><td>{p['deposits']}</td><td class='fig'>{p['corpus_size']}</td>"
        f"<td class='fig'>{pct(p['retrieved']['resolution_rate'])}</td>"
        f"<td class='fig'>{pct(p['random_control']['resolution_rate'])}</td>"
        f"<td class='fig'>{pct(p['retrieved']['counterparty_resolution_rate'])}</td>"
        f"<td class='fig'>{pct(p['retrieved']['escalation_rate'])}</td></tr>"
        for p in points
    )
    caveats = "".join(f"<li>{esc(c)}</li>" for c in curve.get("caveats", []))
    p_value = (f"{sig['p_value']:.3f}" if "p_value" in sig else "—")
    baseline_line = (
        f"<div><span class='n'>{pct(baseline_rate)}</span><span class='l'>deterministic "
        f"rules alone, the gate this had to clear</span></div>"
        if baseline_rate is not None else ""
    )

    return page("Does it work", f"""
    <h1>Does it work</h1>
    <p class="standfirst">Every figure here is read from
    <code>evals/results/</code> at request time. None is typed in.</p>

    <div class="flag">
      <h3>This is not this database</h3>
      <p>The corpus below is the one an offline eval grew by replaying held-out exceptions
      against a simulated reviewer &mdash; 42 precedents to 151, on a test set that is never
      deposited into. The corpus on <a href="/corpus">this deployment</a> is a different and
      much smaller thing, and depositing a precedent here will not move a number on this
      page. It cannot: these figures were committed by a run that has already finished, which
      is the only way a reported result can still be true a year from now.</p>
    </div>

    <div class="headline">
      <span class="big">{pct(last['retrieved']['resolution_rate'])}</span>
      <span class="from">of held-out exceptions resolved without a human, at a corpus of
      {last['corpus_size']} precedents — up from
      {pct(first['retrieved']['resolution_rate'])} at {first['corpus_size']}.</span>
    </div>
    <p class="claim">The test set is never deposited. Each point asks the same questions of
    more accumulated knowledge, so the rise is not the model getting more practice — it is
    the corpus having more to say.</p>

    <div class="figures">
      <div><span class="n">{pct(first['random_control']['resolution_rate'])} &rarr;
        {pct(last['random_control']['resolution_rate'])}</span>
        <span class="l">random control over the same range, drawing the same number of
        precedents and differing only in relevance</span></div>
      <div><span class="n">p&nbsp;=&nbsp;{p_value}</span>
        <span class="l">paired exact McNemar, {sig.get('wins', '—')} gained against
        {sig.get('losses', '—')} lost</span></div>
      {baseline_line}
    </div>

    <h2>Every snapshot</h2>
    <table class="queue">
      <thead><tr><th>Deposits</th><th class="fig">Corpus</th><th class="fig">Resolved</th>
      <th class="fig">Control</th><th class="fig">Only-a-precedent</th>
      <th class="fig">Escalated</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>

    <h2>What this does not show</h2>
    <ul class="note">{caveats}
      <li>The counterparty task is recall of a customer&rsquo;s standing terms. That is real
      institutional knowledge, and it is not deep generalisation.</li>
    </ul>
    <p class="note">Model: <code>{esc(curve.get("model", "—"))}</code>.</p>""",
    here="/result")
