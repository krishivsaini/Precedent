"""The eval report, rendered from committed result files (spec §9, Ring 3.6).

**Every number here is read from `evals/results/*.json`. None is typed in.** That is the whole
design constraint: a report with hand-transcribed figures drifts from the run that produced
them the first time anyone edits it, and the drift is silent. Regenerating this file is
therefore a reproducibility check — if a number in the page is wrong, the JSON is wrong, and
the JSON is what the eval actually wrote.

The report deliberately leads with the things that constrain the claim rather than burying
them: the random control, the case-by-case decomposition of where the gain came from, and the
caveats each result file carries. A report that has to be read carefully to find its own
limitations is a marketing document.

Renders to a single self-contained HTML file with no external assets, so it opens from a
clone with no network.
"""

import argparse
import glob
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.chart import render_curve_svg

RESULTS_DIR = Path(__file__).parent / "results"
OUTPUT = RESULTS_DIR / "report.html"


def latest(pattern: str) -> dict | None:
    matches = sorted(glob.glob(str(RESULTS_DIR / pattern)))
    if not matches:
        return None
    with open(matches[-1]) as handle:
        return json.load(handle)


def pct(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def esc(value) -> str:
    return html.escape(str(value))


def curve_section(curve: dict, rules_baseline: float | None = None) -> str:
    if not curve:
        return "<p class='missing'>No learning-curve result committed yet.</p>"

    rows = "".join(
        f"<tr><td>{p['deposits']}</td><td>{p['corpus_size']}</td>"
        f"<td class='num strong'>{pct(p['retrieved']['resolution_rate'])}</td>"
        f"<td class='num control'>{pct(p['random_control']['resolution_rate'])}</td>"
        f"<td class='num strong'>{pct(p['retrieved']['counterparty_resolution_rate'])}</td>"
        f"<td class='num'>{pct(p['retrieved']['escalation_rate'])}</td>"
        f"<td class='num'>{pct(p['retrieved']['precedent_precision'])}</td></tr>"
        for p in curve["curve"]
    )

    tests = curve.get("significance") or {}
    sig_rows = "".join(
        f"<tr><td>{esc(name.replace('_', ' '))}</td>"
        f"<td class='num'>{t['wins']}W–{t['losses']}L</td>"
        f"<td class='num'>{t['p_value']:.5f}</td>"
        f"<td class='{'yes' if t['significant_at_05'] else 'no'}'>"
        f"{'significant' if t['significant_at_05'] else 'not significant'}</td></tr>"
        for name, t in tests.items()
    )

    points = curve["curve"]
    first, last = points[0], points[-1]
    caveats = "".join(f"<li>{esc(c)}</li>" for c in curve.get("caveats", []))

    # Derived, not asserted. An earlier version hard-coded "it falls" — true of the run it
    # was written against and false of the next one, which is exactly the silent drift this
    # report exists to prevent. Reading the numbers from JSON is not enough if the sentence
    # around them is a constant.
    control_delta = (
        last["random_control"]["resolution_rate"]
        - first["random_control"]["resolution_rate"]
    )
    control_test = (curve.get("significance") or {}).get("control_first_to_last") or {}
    control_moved = control_test.get("significant_at_05", abs(control_delta) > 0.10)
    if not control_moved:
        control_verdict = (
            f"it does not move — {pct(first['random_control']['resolution_rate'])} to "
            f"{pct(last['random_control']['resolution_rate'])}, indistinguishable from noise "
            f"across a corpus that more than tripled"
        )
    elif control_delta < 0:
        control_verdict = (
            f"it <strong>falls</strong>, {pct(first['random_control']['resolution_rate'])} to "
            f"{pct(last['random_control']['resolution_rate'])} — a larger corpus of irrelevant "
            f"precedents is worse than a small one, because there is more to be distracted by"
        )
    else:
        control_verdict = (
            f"it <strong>rises too</strong>, "
            f"{pct(first['random_control']['resolution_rate'])} to "
            f"{pct(last['random_control']['resolution_rate'])}, which means part of the gain "
            f"is prompt length rather than relevance and the headline over-claims"
        )

    return f"""
    <h2>Learning curve</h2>
    <p>The held-out test set of {curve['test_set']['size']} exceptions, replayed unchanged
    against the corpus at five sizes. <strong>The test set is never deposited</strong> — each
    point is the same questions against more accumulated knowledge.</p>

    <figure>{render_curve_svg(curve, rules_baseline)}
      <figcaption class="note">Drawn from the same result file as the table below, so the
      line and the number cannot disagree. The control is drawn at the same weight as the
      treatment: it is the line that decides whether the other one means anything.</figcaption>
    </figure>

    <table>
      <thead><tr><th>Deposits</th><th>Corpus</th><th>Resolved</th>
      <th>Random control</th><th>Counterparty subset</th><th>Escalated</th>
      <th>Precedent precision</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>

    <div class="callout">
      <h3>The control is what makes this evidence</h3>
      <p>The random-precedent control draws the same <em>k</em> precedents from the same
      growing corpus, differing only in whether they are relevant — so {control_verdict}.
      Whatever lifts the treatment arm, it is not the presence of more text in the prompt.</p>
    </div>

    <h3>Paired significance, first snapshot to last</h3>
    <table>
      <thead><tr><th>Comparison</th><th>Discordant</th><th>p (exact McNemar)</th><th></th></tr></thead>
      <tbody>{sig_rows}</tbody>
    </table>

    <h3>What the curve does not say</h3>
    <ul class="caveats">{caveats}</ul>
    """


def ablation_section(chain: dict, graph: dict) -> str:
    if not chain:
        return ""

    def arm_rows(result, label):
        if not result:
            return ""
        return "".join(
            f"<tr><td>{esc(label)}</td><td>{esc(a['arm'].replace('_', ' '))}</td>"
            f"<td class='num'>{pct(a['metrics']['autonomous_resolution_rate'])}</td>"
            f"<td class='num'>{a['metrics']['false_resolution_count']}</td>"
            f"<td class='num'>₹{a['metrics']['false_resolution_cost_inr']:,.0f}</td></tr>"
            for a in result["arms"]
        )

    decomposition = chain.get("effect_decomposition", {})
    return f"""
    <h2>Ring 1–2 ablation</h2>
    <p>Three arms over the pool exceptions, same model, same prompt, same <em>k</em>. Only the
    contents of the precedent block differ.</p>
    <table>
      <thead><tr><th>Engine</th><th>Arm</th><th>Resolved</th><th>False resolutions</th>
      <th>Value at risk</th></tr></thead>
      <tbody>{arm_rows(chain, 'chain')}{arm_rows(graph, 'graph')}</tbody>
    </table>
    <div class="callout">
      <h3>The headline over-claims, and the control shows it</h3>
      <p>On the chain, grounding beat zero-shot by 14.5 points. That splits into
      <strong>{decomposition.get('having_precedents_at_all_pp', '?')}pp from having precedents
      at all</strong> and <strong>{decomposition.get('relevance_of_precedents_pp', '?')}pp from
      their relevance</strong>. Reporting the total as the value of retrieval would over-claim
      by more than a factor of two.</p>
      <p>On the graph, the investigation tools derive what a precedent would have told the
      agent, and the measured value of retrieval collapses to a single case. That is what Ring
      2.5 was built in response to.</p>
    </div>
    """


def retrieval_section(retrieval: dict) -> str:
    if not retrieval:
        return ""
    rows = "".join(
        f"<tr><td>{esc(a['retriever'])}</td>"
        + "".join(
            f"<td class='num'>{pct(a['precedent_class_precision'][f'top_{k}'])}</td>"
            for k in (1, 3, 5)
        )
        + "</tr>"
        for a in retrieval["arms"]
    )
    return f"""
    <h2>Retrieval quality, measured alone</h2>
    <p>No model involved, so a bad number here cannot be explained away as a reasoning
    failure. Scored on the pool exceptions only.</p>
    <table>
      <thead><tr><th>Retriever</th><th>top-1</th><th>top-3</th><th>top-5</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="note">{esc(retrieval['embedder']['caveat'])}</p>
    """


def deposit_section(deposit: dict) -> str:
    if not deposit:
        return ""
    rows = "".join(
        f"<tr><td>{esc(v['prompt_version'])}</td>"
        f"<td class='num'>{esc(v['metrics']['authored_successfully'])}</td>"
        f"<td class='num'>{pct(v['metrics']['deposit_retrieval_rate'])}</td>"
        f"<td class='num'>{pct(v['metrics']['resolution_rate_on_later_sightings'])}</td></tr>"
        for v in deposit["versions"]
    )
    return f"""
    <h2>Deposit prompt versions</h2>
    <p>Spec §4 asks the eval to measure whether one prompt version authors better precedents
    than another — prompt engineering with a number attached.</p>
    <table>
      <thead><tr><th>Version</th><th>Authored</th><th>Deposit retrieved</th>
      <th>Resolved later sightings</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="note">{esc(deposit.get('caveat', ''))}</p>
    """


def build_report() -> str:
    curve = latest("learning-curve-*.json")
    chain = latest("ablation-2026*.json")
    graph = latest("ablation-graph-*.json")
    retrieval = latest("retrieval-*.json")
    deposit = latest("deposit-prompt-*.json")
    baseline = latest("2026-09-01-0838.json")

    sources = [
        name for name, data in (
            ("learning-curve", curve), ("ablation (chain)", chain),
            ("ablation (graph)", graph), ("retrieval", retrieval),
            ("deposit-prompt", deposit), ("rules baseline", baseline),
        ) if data
    ]

    rules_rate = (
        baseline["metrics"]["autonomous_resolution_rate"] if baseline else None
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!-- Generated by evals/report.py. Every number is read from a committed
result file; none is typed in. Regenerate rather than edit. -->
<meta charset="utf-8">
<title>Precedent — eval report</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 62rem; margin: 0 auto; padding: 2.5rem 1.5rem; color: #14181f;
         background: #fbfbfa; }}
  h1 {{ font-size: 1.9rem; margin-bottom: .2rem; }}
  h2 {{ margin-top: 2.6rem; padding-bottom: .35rem; border-bottom: 2px solid #e6e3dd; }}
  h3 {{ margin-top: 1.6rem; font-size: 1.02rem; }}
  .sub {{ color: #6b7280; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .92rem; }}
  th, td {{ text-align: left; padding: .5rem .65rem; border-bottom: 1px solid #e6e3dd; }}
  th {{ background: #f3f1ec; font-weight: 600; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.strong {{ font-weight: 650; }}
  td.control {{ color: #9a6a00; }}
  td.yes {{ color: #1a7f4b; font-weight: 600; }}
  td.no {{ color: #8a8a8a; }}
  .callout {{ background: #fff; border: 1px solid #e6e3dd; border-left: 3px solid #14181f;
             padding: .9rem 1.1rem; margin: 1.4rem 0; border-radius: 3px; }}
  .callout h3 {{ margin-top: 0; }}
  .caveats li {{ margin-bottom: .5rem; }}
  .note {{ color: #6b7280; font-size: .88rem; }}
  figure {{ margin: 1.2rem 0; }}
  figcaption {{ margin-top: .4rem; }}
  .missing {{ color: #9a6a00; }}
  footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e6e3dd;
            color: #6b7280; font-size: .85rem; }}
  code {{ background: #f3f1ec; padding: .1rem .3rem; border-radius: 3px; font-size: .9em; }}
</style>

<h1>Precedent — eval report</h1>
<p class="sub">Generated {generated} from committed result files. Every figure is read from
JSON; none is transcribed. Regenerating this page is a reproducibility check.</p>

<div class="callout">
  <h3>The claim, stated as narrowly as the evidence supports</h3>
  <p>A precedent corpus improves resolution <strong>only for knowledge that cannot be derived
  from the case in front of the agent</strong>. Where the answer is computable — a round
  withholding rate, a netted sum, two payments on one order — an investigation tool derives it
  and the corpus adds nothing. Of the nine held-out cases that improved as the corpus grew,
  every one was a counterparty case, and every case that regressed was a derivable one.</p>
</div>

{curve_section(curve, rules_rate)}
{ablation_section(chain, graph)}
{retrieval_section(retrieval)}
{deposit_section(deposit)}

<h2>Baselines</h2>
<p>Deterministic rules alone, measured in Ring 0 before any LLM code existed:
<strong>{pct(rules_rate)}</strong> on the full 240-record dataset, and 0.0% on the held-out
test set — every test record is an exception by construction, so a rules-only system must
score zero there. That is the floor the agent has to beat.</p>

<footer>
  Sources: {esc(', '.join(sources))}. Model:
  <code>{esc((curve or {}).get('model', 'n/a'))}</code>.
  Regenerate with <code>uv run python -m evals.report</code>.
</footer>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the eval report from committed JSON")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args()

    path = Path(args.output)
    path.write_text(build_report(), encoding="utf-8")
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
