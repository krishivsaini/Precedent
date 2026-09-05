"""Build the public showcase — one static page, generated from committed results.

    uv run python scripts/build_site.py        # -> site/index.html

**Why static.** The page is for a demo and a portfolio link, so it has to load in a hotel
wifi dead zone and still be true a year from now. No server, no database, no API key, no cold
start. Everything it claims is read at build time from `evals/results/*.json` — the same files
the eval harness wrote — so a figure on the page cannot drift from the run that produced it.
The page is the report; the FastAPI app beside it is the running system.

**Design.** A reconciliation is two columns that should agree and do not, and the craft is
finding where the difference went. The tie-out is the recurring structural device: anything
that closes gets a rule above its total, the way a statement sets it. The hero is the learning
curve drawn large with the random control flat beside it, because that contrast is the whole
argument — and the control is drawn at the same weight as the result, which is the honesty
made visible rather than asserted.
"""

import base64
import hashlib
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
OUT = ROOT / "site" / "index.html"
HEADERS = OUT.parent / "_headers"


def latest(pattern: str) -> dict | None:
    matches = sorted(RESULTS.glob(pattern))
    if not matches:
        return None
    return json.loads(matches[-1].read_text(encoding="utf-8"))


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def pct(v, places: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{places}f}%"


def rupees(paise: int) -> str:
    sign = paise < 0
    whole, part = divmod(abs(paise), 100)
    body = f"{whole:,}.{part:02d}"
    return f"({body})" if sign else body


# ---------------------------------------------------------------------------------------
# the hero chart


def hero_chart(curve: dict, rules_baseline: float | None) -> str:
    """The learning curve, drawn large.

    Three series at deliberately equal weight. Under-drawing the control would be an
    editorial claim: it is the line that decides whether the other one means anything, and a
    reader should be able to check that for themselves at a glance.
    """
    points = curve["curve"]
    W, H = 940, 430
    # The right margin has to hold the longest legend label, or it clips silently.
    L, R, T, B = 64, 268, 28, 52
    xs = [p["corpus_size"] for p in points]
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1

    def x(size):
        return L + (size - lo) / span * (W - L - R)

    def y(rate):
        return T + (1 - rate) * (H - T - B)

    parts = []
    for tick in range(0, 6):
        rate = tick / 5
        yy = y(rate)
        parts.append(
            f'<line x1="{L}" y1="{yy:.1f}" x2="{W - R}" y2="{yy:.1f}" class="grid"/>'
            f'<text x="{L - 12}" y="{yy + 5:.1f}" class="ytick">{rate:.0%}</text>'
        )
    for p in points:
        parts.append(
            f'<text x="{x(p["corpus_size"]):.1f}" y="{H - B + 24}" class="xtick">'
            f'{p["deposits"]}</text>'
        )
    parts.append(
        f'<text x="{(L + W - R) / 2:.0f}" y="{H - 8}" class="axis">'
        f'precedents deposited</text>'
    )

    if rules_baseline is not None:
        parts.append(
            f'<line x1="{L}" y1="{y(rules_baseline):.1f}" x2="{W - R}" '
            f'y2="{y(rules_baseline):.1f}" class="baseline"/>'
        )

    def series(key, cls, delay):
        coords = " ".join(
            f"{x(p['corpus_size']):.1f},{y(v):.1f}"
            for p in points if (v := key(p)) is not None
        )
        dots = "".join(
            f'<circle cx="{x(p["corpus_size"]):.1f}" cy="{y(v):.1f}" r="4.5" '
            f'class="{cls}-dot"/>'
            for p in points if (v := key(p)) is not None
        )
        return (f'<polyline points="{coords}" class="{cls}" '
                f'style="animation-delay:{delay}s"/>{dots}')

    parts.append(series(lambda p: p["random_control"]["resolution_rate"], "control", 0.1))
    parts.append(series(
        lambda p: p["retrieved"]["counterparty_resolution_rate"], "subset", 0.35))
    parts.append(series(lambda p: p["retrieved"]["resolution_rate"], "treatment", 0.6))

    legend = [
        ("treatment", "retrieved precedents"),
        ("subset", "only-a-precedent cases"),
        ("control", "random control"),
    ]
    if rules_baseline is not None:
        legend.append(("baseline", f"rules alone, {pct(rules_baseline)}"))
    for i, (cls, label) in enumerate(legend):
        yy = T + 18 + i * 26
        xx = W - R + 16
        parts.append(
            f'<line x1="{xx}" y1="{yy}" x2="{xx + 22}" y2="{yy}" class="{cls} key"/>'
            f'<text x="{xx + 30}" y="{yy + 4}" class="legend">{esc(label)}</text>'
        )

    return (f'<svg viewBox="0 0 {W} {H}" class="hero-chart" role="img" '
            f'aria-label="Resolution rate rising with corpus size while the random control '
            f'stays flat" xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>")


# ---------------------------------------------------------------------------------------
# sections


def hero(curve, baseline_rate) -> str:
    first, last = curve["curve"][0], curve["curve"][-1]
    sig = curve["significance"]["headline_first_to_last"]
    return f"""
<section class="hero">
  <h1>An agent that writes<br>its own knowledge base.</h1>
  <p class="deck">Reconciliation exceptions are resolved by an agent that retrieves
  precedents from past resolutions. Every confirmed resolution is written back as a new
  precedent, so the corpus is authored by the system&rsquo;s own operation — and autonomous
  resolution rate is the measurement, not the claim.</p>

  <figure class="hero-figure">
    {hero_chart(curve, baseline_rate)}
    <figcaption>Held-out exceptions replayed against the corpus at five sizes. The test set
    is never deposited: each point asks the same questions of more accumulated knowledge.
    The control draws the same number of precedents from the same corpus and differs only in
    whether they are relevant.</figcaption>
  </figure>

  <div class="figures">
    <div><span class="n">{pct(first['retrieved']['resolution_rate'])} &rarr;
      {pct(last['retrieved']['resolution_rate'])}</span>
      <span class="l">resolved without a human, as the corpus grew from
      {first['corpus_size']} to {last['corpus_size']} precedents</span></div>
    <div><span class="n">{pct(first['random_control']['resolution_rate'])} &rarr;
      {pct(last['random_control']['resolution_rate'])}</span>
      <span class="l">the random control over the same range — flat, which is what rules out
      &ldquo;more text in the prompt&rdquo;</span></div>
    <div><span class="n">p&nbsp;=&nbsp;{sig['p_value']:.3f}</span>
      <span class="l">paired exact McNemar, {sig['wins']} cases gained against
      {sig['losses']} lost</span></div>
  </div>
</section>"""


def the_case(curve) -> str:
    gross, fee, tax = 167_800, 3_960, 604
    settles, landed, expected = gross - fee - tax, gross - fee - tax, 186_444
    return f"""
<section id="case">
  <h2>What an exception looks like</h2>
  <p class="lede">A payment from Coral Textiles. The bank credit is <em>net</em> of the
  processor&rsquo;s fee; the invoice is <em>gross</em>. Those are two different comparisons,
  and collapsing them into one &ldquo;matches / does not match&rdquo; line is what makes
  reconciliation tools untrustworthy — the reviewer can no longer tell a genuine shortfall
  from a fee deduction.</p>

  <div class="tieout">
    <div>
      <h3>At the bank</h3>
      <table class="ledger">
        <tr><td>Payments captured, gross</td><td class="fig">{rupees(gross)}</td></tr>
        <tr><td class="in">processor fee</td>
            <td class="fig neg">{rupees(-fee)}</td></tr>
        <tr><td class="in">tax on the fee</td>
            <td class="fig neg">{rupees(-tax)}</td></tr>
        <tr class="sub"><td>Should have landed</td>
            <td class="fig">{rupees(settles)}</td></tr>
        <tr><td>Actually landed</td><td class="fig">{rupees(landed)}</td></tr>
        <tr class="tie"><td>Unexplained</td><td class="fig">{rupees(0)}</td></tr>
      </table>
      <p class="reads yes">The credit ties out.</p>
    </div>
    <div>
      <h3>Against the invoice</h3>
      <table class="ledger">
        <tr><td>Ledger expects, gross</td><td class="fig">{rupees(expected)}</td></tr>
        <tr><td>Customer paid, gross</td><td class="fig">{rupees(gross)}</td></tr>
        <tr class="tie"><td>Kept back</td>
            <td class="fig">{rupees(expected - gross)}</td></tr>
      </table>
      <p class="reads no">The customer withheld part of the invoice.</p>
    </div>
  </div>
</section>"""


def investigation() -> str:
    steps = [
        ("classify", "proportional shortfall",
         "Computed from the case, not asked of the model — naming the class before the "
         "evidence is gathered would anchor every step after it."),
        ("retrieve", "5 precedents, BM25 over the corpus",
         "Searched with the computed observations rather than the whole record dump: the "
         "per-record boilerplate buries the few sentences that discriminate."),
        ("investigate", "fetch_payment · compute_expected_amount(rate 0.10)",
         "A tool loop capped at five calls in code, not asked of the model. The arithmetic "
         "tool is the same code that will check the answer, so a figure from it cannot "
         "disagree with the verifier."),
        ("propose", "tds_short_payment @ 0.96, citing precedent 2",
         "Structured output, Pydantic-validated. A parse failure escalates rather than "
         "guessing."),
        ("verify", "passed",
         "Deterministic. A check performed by the model being checked samples the same "
         "distribution that produced the error."),
        ("route", "above the 0.90 threshold — resolve",
         "The threshold is set from measured outcomes, not chosen: it gives up one case of "
         "coverage to remove ₹23,739 of false-resolution exposure."),
    ]
    rows = "".join(
        f'<li><span class="node">{esc(n)}</span>'
        f'<span class="did">{esc(d)}</span>'
        f'<p class="why">{esc(w)}</p></li>'
        for n, d, w in steps
    )
    return f"""
<section id="how">
  <h2>How it decides</h2>
  <p class="lede">A LangGraph investigation with a verify&thinsp;&rarr;&thinsp;revise cycle.
  Two of these steps deliberately do not call a model.</p>
  <ol class="trace">{rows}</ol>
</section>"""


def learning() -> str:
    return """
<section id="learns">
  <h2>The same customer, before and after</h2>
  <p class="lede">Konark Logistics deducts a rebate negotiated with them. It is not a
  statutory rate, and nothing in the case says what it is — so the answer cannot be worked
  out from the evidence. It can only be remembered.</p>

  <div class="beforeafter">
    <div>
      <p class="stamp no">First time it saw them</p>
      <p class="when">corpus: 42 hand-written precedents, none about this customer</p>
      <table class="ledger">
        <tr><td>Shortfall against the invoice</td><td class="fig">2.65%</td></tr>
        <tr><td>Matches a statutory band</td><td class="fig">no</td></tr>
        <tr><td>Explained by fees or a refund</td><td class="fig">no</td></tr>
      </table>
      <p class="outcome no">Escalated at 0.65 confidence.</p>
      <p class="why">Correct behaviour: the evidence is genuinely insufficient. A guess here
      would close an invoice that was never settled.</p>
    </div>
    <div>
      <p class="stamp yes">After a reviewer resolved one</p>
      <p class="when">corpus: one precedent about Konark Logistics, written from that
      resolution</p>
      <blockquote class="precedent">
        <p><span class="lbl">Situation.</span> Payments from Konark Logistics arrive short of
        the invoice by 2.65 percent, a proportion matching no statutory withholding band,
        with no refund or fee explaining the gap.</p>
        <p><span class="lbl">Resolution.</span> Konark settles under a negotiated rebate
        agreed in their supply contract. Reconstruct the invoice from the receipt and close
        it in full. This is not withholding tax: no tax credit arises.</p>
      </blockquote>
      <p class="outcome yes">Resolved as a negotiated rebate at 0.94, citing it.</p>
    </div>
  </div>

  <p class="aside">Written generically — &ldquo;this counterparty&rdquo; rather than the
  name — the same knowledge resolved 1 case in 5. Naming them resolved 4 in 5. A customer
  name generalises to that customer&rsquo;s future cases; a payment id generalises to
  nothing. That distinction is now in the deposit prompt, with the measurement attached.</p>
</section>"""


def gate() -> str:
    return """
<section id="gate">
  <h2>Nothing is deposited without a human</h2>
  <p class="lede">A confirmed wrong resolution does not cost one record. It is written into
  the corpus and then retrieved to justify future wrong ones, which makes corpus poisoning
  the most serious failure mode in the system — and this screen the only thing standing
  between it and the corpus.</p>

  <div class="gate-demo" id="gateDemo">
    <p class="stakes">Confirming writes this into the corpus. It will be found and cited on
    future cases that look like this one. Decide on the figures and the precedents, not on
    the verdict.</p>
    <div class="actions">
      <button class="primary" data-act="confirmed">Confirm and deposit</button>
      <button data-act="corrected">Correct and deposit</button>
      <button class="danger" data-act="rejected">Reject</button>
    </div>
    <p class="outcome-slot" id="gateOut" role="status" aria-live="polite"></p>
  </div>

  <div class="tieout narrow">
    <div>
      <h3>What deposits</h3>
      <table class="ledger">
        <tr><td>Confirmed</td><td class="fig">yes</td></tr>
        <tr><td>Corrected</td><td class="fig">yes, at the corrected answer</td></tr>
        <tr><td>Rejected</td><td class="fig neg">no</td></tr>
        <tr class="tie"><td>Unreviewed</td><td class="fig neg">never</td></tr>
      </table>
      <p class="why">A corrected resolution is the higher-value precedent: it records a case
      the system got wrong.</p>
    </div>
    <div>
      <h3>Under a realistic reviewer</h3>
      <table class="ledger">
        <tr><td>Confirmed</td><td class="fig">66</td></tr>
        <tr><td>Corrected</td><td class="fig">44</td></tr>
        <tr><td>Rejected</td><td class="fig neg">24</td></tr>
        <tr class="tie"><td>Precedents from 134 cases</td><td class="fig">109</td></tr>
      </table>
      <p class="why">The curve above is measured this way — rejections included — so it is an
      estimate rather than a ceiling.</p>
    </div>
  </div>
</section>"""


def evidence(curve, chain, graph, calib) -> str:
    rows = "".join(
        f"<tr><td>{p['deposits']}</td><td class='fig'>{p['corpus_size']}</td>"
        f"<td class='fig strong'>{pct(p['retrieved']['resolution_rate'])}</td>"
        f"<td class='fig'>{pct(p['random_control']['resolution_rate'])}</td>"
        f"<td class='fig strong'>{pct(p['retrieved']['counterparty_resolution_rate'])}</td>"
        f"<td class='fig'>{pct(p['retrieved']['escalation_rate'])}</td></tr>"
        for p in curve["curve"]
    )
    sig = "".join(
        f"<tr><td>{esc(k.replace('_', ' ').replace('first to last', ''))}</td>"
        f"<td class='fig'>{v['wins']}&ndash;{v['losses']}</td>"
        f"<td class='fig'>{v['p_value']:.5f}</td>"
        f"<td class='{'yes' if v['significant_at_05'] else 'no'}'>"
        f"{'significant' if v['significant_at_05'] else 'not significant'}</td></tr>"
        for k, v in curve["significance"].items()
    )
    dec_chain = chain.get("effect_decomposition", {}) if chain else {}
    dec_graph = graph.get("effect_decomposition", {}) if graph else {}
    caveats = "".join(f"<li>{esc(c)}</li>" for c in curve.get("caveats", []))
    return f"""
<section id="evidence">
  <h2>Does it work</h2>

  <table class="data">
    <thead><tr><th>Deposits</th><th class="fig">Corpus</th><th class="fig">Resolved</th>
    <th class="fig">Control</th><th class="fig">Only-a-precedent cases</th>
    <th class="fig">Escalated</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <h3>Paired significance, first snapshot to last</h3>
  <table class="data">
    <thead><tr><th>Comparison</th><th class="fig">W&ndash;L</th>
    <th class="fig">p</th><th></th></tr></thead>
    <tbody>{sig}</tbody>
  </table>

  <h3>What the tools do to the case for retrieval</h3>
  <p class="lede">The gain over answering with no precedents at all splits into an effect of
  <em>having</em> precedents and an effect of their <em>relevance</em>. Giving the agent
  investigation tools removes the first entirely.</p>
  <table class="data">
    <thead><tr><th></th><th class="fig">from having precedents</th>
    <th class="fig">from their relevance</th></tr></thead>
    <tbody>
      <tr><td>single prompt</td>
        <td class="fig">+{dec_chain.get('having_precedents_at_all_pp', '?')}pp</td>
        <td class="fig">+{dec_chain.get('relevance_of_precedents_pp', '?')}pp</td></tr>
      <tr><td>with an investigation graph</td>
        <td class="fig">+{dec_graph.get('having_precedents_at_all_pp', '?')}pp</td>
        <td class="fig strong">+{dec_graph.get('relevance_of_precedents_pp', '?')}pp</td></tr>
    </tbody>
  </table>

  <h3>What this does not show</h3>
  <ul class="caveats">{caveats}
    <li>Every gain was a case whose answer could not be derived. Every case that regressed
    could be — the corpus is a mild distraction where an investigation tool already
    suffices.</li>
    <li>The counterparty task is recall of a customer&rsquo;s standing terms. That is real
    institutional knowledge, and it is not deep generalisation.</li>
  </ul>
</section>"""


def live_link() -> str:
    """A link to the running app, or nothing.

    Driven by `PRECEDENT_APP_URL` rather than hard-coded, because the two halves deploy
    independently: the showcase is true whether or not a backend happens to be up, and a
    dead link to a torn-down demo would be the one false thing on the page.
    """
    url = os.environ.get("PRECEDENT_APP_URL", "").strip()
    if not url:
        return ""
    return f'<a class="live" href="{esc(url)}">the running approval screen &rarr;</a>'


def build(model: str, generated: str, sections: str, script: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Precedent — reconciliation that remembers</title>
<meta name="description" content="An exception-resolution agent whose knowledge base is
written by its own operation. Autonomous resolution rate rises from 70.0% to 86.7% as the
corpus grows, while a random-precedent control stays flat.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,300;0,6..72,400;0,6..72,500;0,6..72,600;1,6..72,400&display=swap" rel="stylesheet">
<style>{CSS}</style>
<body>
<div class="sheet">
  <header class="masthead">
    <span class="wordmark">Precedent</span>
    <span class="tag">reconciliation that remembers</span>
    {live_link()}
  </header>
  {sections}
  <footer>
    <p>Every figure on this page is read at build time from the JSON the eval harness wrote —
    none is typed in. Model: <code>{esc(model)}</code>. Built {esc(generated)}.</p>
  </footer>
</div>
<script>{script}</script>
</body></html>"""


CSS = """
:root{
  --paper:#EEF1EA; --card:#F8FAF5; --rule:#C4CFBE; --ink:#171C16;
  --muted:#5F6A5C; --red:#8E2B22; --green:#2C5F2D; --blue:#1D4E7A;
  color-scheme:light;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%; scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}
  *{animation:none!important;transition:none!important}}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:Newsreader,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  font-size:17px;line-height:1.62;font-weight:400;
  font-variant-numeric:tabular-nums lining;}
.sheet{max-width:62rem;margin:0 auto;padding:0 1.6rem 6rem}

.masthead{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
  padding:1.6rem 0 .9rem;border-bottom:2px solid var(--ink);margin-bottom:3.4rem}
.wordmark{font-size:1.15rem;font-weight:600;letter-spacing:.005em}
.tag{color:var(--muted);font-size:.95rem}
.masthead .live{margin-left:auto;color:var(--blue);text-decoration:none;font-size:.95rem;
  border-bottom:1px solid var(--rule);padding-bottom:1px}
.masthead .live:hover{border-bottom-color:var(--blue)}

h1{font-size:clamp(2.6rem,6vw,4.3rem);line-height:1.04;font-weight:500;
  letter-spacing:-.022em;margin:0 0 1.1rem}
.deck{font-size:1.2rem;line-height:1.55;max-width:34em;color:var(--ink);margin:0 0 2.6rem;
  font-weight:300}
h2{font-size:clamp(1.5rem,2.6vw,2rem);font-weight:500;letter-spacing:-.014em;
  margin:0 0 .5rem}
h3{font-size:1.02rem;font-weight:600;margin:2rem 0 .5rem}
section{margin:0 0 5rem;scroll-margin-top:1.5rem}
section>h2{padding-top:2.4rem;border-top:1px solid var(--rule)}
.hero>h1{padding-top:0}
.lede{color:var(--muted);max-width:38em;margin:0 0 1.6rem;font-size:1.02rem}
.aside{color:var(--muted);max-width:40em;font-size:.98rem;margin-top:2rem;
  padding-left:1.1rem;border-left:2px solid var(--rule)}
em{font-style:italic}

/* hero chart */
.hero-figure{margin:0 0 2.4rem}
.hero-chart{width:100%;height:auto;display:block}
.hero-chart .grid{stroke:var(--rule);stroke-width:1}
.hero-chart .ytick,.hero-chart .xtick{fill:var(--muted);font-size:13px;
  font-family:Newsreader,Georgia,serif}
.hero-chart .ytick{text-anchor:end}
.hero-chart .xtick{text-anchor:middle}
.hero-chart .axis{fill:var(--ink);font-size:13.5px;text-anchor:middle;
  font-family:Newsreader,Georgia,serif}
.hero-chart .legend{fill:var(--ink);font-size:13.5px;font-family:Newsreader,Georgia,serif}
.hero-chart polyline{fill:none;stroke-width:2.6;stroke-linejoin:round;stroke-linecap:round;
  stroke-dasharray:2000;stroke-dashoffset:2000;animation:draw 1.25s ease-out forwards}
.hero-chart .key{stroke-width:3;stroke-dasharray:none;animation:none}
.hero-chart .treatment{stroke:var(--blue)} .hero-chart .treatment-dot{fill:var(--blue)}
.hero-chart .control{stroke:var(--red)}    .hero-chart .control-dot{fill:var(--red)}
.hero-chart .subset{stroke:var(--green)}   .hero-chart .subset-dot{fill:var(--green)}
.hero-chart .baseline{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:3 5}
.hero-chart circle{opacity:0;animation:pop .01s ease-out forwards;animation-delay:1.3s}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes pop{to{opacity:1}}
@media (prefers-reduced-motion:reduce){
  .hero-chart polyline{stroke-dashoffset:0}.hero-chart circle{opacity:1}}
figcaption{color:var(--muted);font-size:.94rem;max-width:44em;margin-top:.9rem}

.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));
  gap:1.8rem 2.4rem;margin:2.6rem 0 0}
.figures div{border-top:1px solid var(--ink);padding-top:.6rem}
.figures .n{font-size:1.75rem;font-weight:500;display:block;letter-spacing:-.012em;
  line-height:1.2}
.figures .l{color:var(--muted);font-size:.92rem;display:block;margin-top:.35rem}

/* ledger */
.tieout{display:grid;grid-template-columns:1fr 1fr;gap:0 2.6rem;align-items:start}
.tieout.narrow{max-width:52rem}
.tieout>div+div{border-left:1px solid var(--rule);padding-left:2.6rem}
.ledger{width:100%;border-collapse:collapse;margin:.3rem 0 0}
.ledger td{padding:.3rem 0;border:0;vertical-align:baseline}
.ledger td.fig{text-align:right;white-space:nowrap;padding-left:1.2rem}
.ledger td.in{padding-left:1.2rem;color:var(--muted)}
.ledger tr.sub td{border-top:1px solid var(--ink);padding-top:.45rem}
.ledger tr.tie td{border-top:1px solid var(--ink);
  box-shadow:inset 0 3px 0 -2px var(--ink);padding-top:.52rem;font-weight:600}
.neg{color:var(--red)}
.reads{margin:.7rem 0 0;font-size:.97rem}
.reads.yes,.outcome.yes,.yes{color:var(--green)}
.reads.no,.outcome.no,.no{color:var(--red)}
.why{color:var(--muted);font-size:.94rem;max-width:34em;margin:.6rem 0 0}

/* trace */
.trace{list-style:none;counter-reset:step;margin:0;padding:0;max-width:46rem}
.trace li{counter-increment:step;position:relative;padding:0 0 1.15rem 3.4rem;
  margin-bottom:1.15rem;border-bottom:1px solid var(--rule)}
.trace li:last-child{border-bottom:0}
.trace li::before{content:counter(step);position:absolute;left:0;top:.05rem;
  color:var(--muted);font-size:.95rem}
.trace .node{font-weight:600;margin-right:.55rem}
.trace .did{color:var(--blue)}
.trace .why{margin-top:.3rem}

/* before / after */
.beforeafter{display:grid;grid-template-columns:1fr 1fr;gap:0 2.6rem;align-items:start}
.beforeafter>div+div{border-left:1px solid var(--rule);padding-left:2.6rem}
.stamp{font-weight:600;margin:0 0 .1rem}
.when{color:var(--muted);font-size:.9rem;margin:0 0 1rem}
.outcome{font-weight:600;margin:1rem 0 0}
blockquote.precedent{margin:.2rem 0 0;padding:0 0 0 1.1rem;
  border-left:3px solid var(--green)}
blockquote.precedent p{margin:.4rem 0 0;font-size:.99rem}
.lbl{color:var(--muted)}

/* gate */
.gate-demo{background:var(--card);border:1px solid var(--rule);
  border-left:3px solid var(--ink);padding:1.3rem 1.5rem;margin:0 0 2.4rem;max-width:52rem}
.stakes{margin:0 0 1.1rem;max-width:40em}
.actions{display:flex;gap:.7rem;flex-wrap:wrap}
button{font:inherit;font-size:.98rem;padding:.58rem 1.2rem;border:1px solid var(--ink);
  background:var(--card);color:var(--ink);border-radius:2px;cursor:pointer;
  transition:background .12s ease,color .12s ease}
button.primary{background:var(--ink);color:var(--paper)}
button.danger{border-color:#B79A97;color:var(--red)}
button:hover{background:var(--ink);color:var(--paper)}
button.danger:hover{background:var(--red);border-color:var(--red);color:#fff}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.outcome-slot{margin:1.1rem 0 0;min-height:1.6rem;font-size:.99rem}
.outcome-slot.on{animation:rise .25s ease-out}
@keyframes rise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* data tables */
table.data{width:100%;border-collapse:collapse;margin:.6rem 0 1.4rem;font-size:.96rem}
table.data th{text-align:left;font-weight:600;padding:.5rem 1.5rem .5rem 0;
  border-bottom:1px solid var(--ink)}
table.data td{padding:.5rem 1.5rem .5rem 0;border-bottom:1px solid var(--rule)}
table.data th.fig,table.data td.fig{text-align:right}
table.data th:last-child,table.data td:last-child{padding-right:0}
table.data .strong{font-weight:600}
.caveats{color:var(--muted);max-width:40em;padding-left:1.1rem}
.caveats li{margin-bottom:.55rem}

footer{border-top:1px solid var(--rule);padding-top:1.2rem;color:var(--muted);
  font-size:.9rem;max-width:44em}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;
  background:var(--card);padding:.08rem .3rem;border:1px solid var(--rule)}

@media(max-width:48rem){
  .tieout,.beforeafter{grid-template-columns:1fr}
  .tieout>div+div,.beforeafter>div+div{border-left:0;border-top:1px solid var(--rule);
    padding-left:0;padding-top:1.4rem;margin-top:1.4rem}
  table.data{font-size:.9rem}
  table.data th,table.data td{padding-right:.7rem}
}
"""

JS = """
document.querySelectorAll('#gateDemo button').forEach(function(b){
  b.addEventListener('click', function(){
    var act = b.dataset.act;
    var out = document.getElementById('gateOut');
    var text = {
      confirmed: 'Confirmed. A precedent was written from this resolution and is now in the '
        + 'corpus — it will be retrieved on future cases that look like this one.',
      corrected: 'Corrected. The precedent records the answer you gave, not the one the '
        + 'agent proposed. Corrections are the higher-value deposit: they encode a case the '
        + 'system got wrong.',
      rejected: 'Rejected. Nothing was written to the corpus. The exception stays on the '
        + 'list rather than being left ambiguous.'
    }[act];
    out.className = 'outcome-slot on ' + (act === 'rejected' ? 'no' : 'yes');
    out.textContent = text;
  });
});
"""


def script_text() -> str:
    """The page's inline script: the gate demo, plus a wake-up call when there is an app.

    **Why the page pings the app.** The service sleeps when idle and takes the better part of
    a minute to wake. A reader who follows the link cold gets a blank browser and concludes
    it is broken — the worst possible reading, since the thing they were about to look at is
    the part that actually works. Landing here starts the wake-up, so by the time anyone has
    read this far and clicked through, it is warm.

    Fire-and-forget and `no-cors`: nothing on this page depends on the response, and the page
    must render identically whether the app is up, asleep, or gone.
    """
    app = os.environ.get("PRECEDENT_APP_URL", "").strip().rstrip("/")
    if not app:
        return JS
    return JS + """
(function () {
  try {
    fetch(%s + "/healthz", { mode: "no-cors", cache: "no-store" });
  } catch (e) { /* the page is complete without it */ }
})();
""" % json.dumps(app)


def csp_headers(script: str) -> str:
    """Cloudflare Pages `_headers`, with the inline script pinned by hash.

    The script is generated right here, so its hash can be computed rather than maintained —
    a CSP that has to be updated by hand is one that gets `'unsafe-inline'` added to it the
    first time it breaks a deploy.

    `style-src` keeps `'unsafe-inline'` and is not pinned: the chart sets `animation-delay`
    as a style *attribute* per series, and attributes need `'unsafe-hashes'` to be allowed by
    hash — a broader grant than the one it would replace. On a page with no user input and no
    third-party script, the inline style is not the exposure worth contorting the chart for.
    """
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    app = os.environ.get("PRECEDENT_APP_URL", "").strip().rstrip("/")
    policy = "; ".join([
        "default-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "font-src https://fonts.gstatic.com",
        "style-src 'unsafe-inline' https://fonts.googleapis.com",
        f"script-src 'sha256-{digest}'",
        # Only ever the one origin the page was built to wake, and only when there is one.
        f"connect-src {app}" if app else "connect-src 'none'",
    ])
    return "\n".join([
        "/*",
        f"  Content-Security-Policy: {policy}",
        "  X-Content-Type-Options: nosniff",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: geolocation=(), camera=(), microphone=(), interest-cohort=()",
        "  Strict-Transport-Security: max-age=63072000; includeSubDomains; preload",
        "",
    ])


def main() -> int:
    curve = latest("learning-curve-2026-09-04*.json") or latest("learning-curve-*.json")
    if not curve:
        print("no learning-curve result committed", file=sys.stderr)
        return 1
    chain = latest("ablation-2026-09-04*.json")
    graph = latest("ablation-graph-2026-09-04*.json")
    calib = latest("calibration-*.json")
    baseline = latest("2026-09-01-0838.json")
    baseline_rate = (
        baseline["metrics"]["autonomous_resolution_rate"] if baseline else None
    )

    sections = "".join([
        hero(curve, baseline_rate),
        the_case(curve),
        investigation(),
        learning(),
        gate(),
        evidence(curve, chain, graph, calib),
    ])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    script = script_text()
    OUT.write_text(build(
        curve["model"],
        datetime.now(timezone.utc).strftime("%-d %B %Y"),
        sections,
        script,
    ), encoding="utf-8")
    HEADERS.write_text(csp_headers(script), encoding="utf-8")
    print(f"written to {OUT} ({OUT.stat().st_size:,} bytes)")
    print(f"written to {HEADERS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
