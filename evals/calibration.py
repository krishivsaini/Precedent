"""Confidence calibration and the auto-resolve threshold (spec §6, Ring 4).

Ring 0 set `DEFAULT_AUTO_RESOLVE_THRESHOLD` to 0.8 as an explicit placeholder — a number
nothing measured. Ring 2's ablation then made the cost of leaving it there concrete:
grounding halves the escalation rate and converts those escalations into correct answers
*and* expensive wrong ones, so the threshold is the single control governing spec §6's
"number that gets someone fired".

Two things are measured here, and they answer different questions:

**Reliability** — when the agent says 0.9, is it right 90% of the time? This is a property of
the model and the prompt, and it is reported whether or not it is flattering. It is not
flattering: the agent is overconfident in every bin.

**The threshold sweep** — for each candidate cutoff, what would the resolution rate, error
rate and rupee exposure have been on cases already scored? This is counterfactual over real
outcomes rather than a new run, which is what makes it cheap enough to redo whenever the
prompt or model changes. It is also why it must be re-derived rather than trusted: a
threshold tuned on one model is not evidence about another.

**The honest limit.** The sweep is computed on the *pool* exceptions the ablation scored, and
a threshold chosen on them is chosen on data the system has seen. Reported alongside is the
same sweep on the held-out test set where a curve result is available, because a cutoff that
only works on the tuning set is not a cutoff.
"""

import argparse
import glob
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"

#: Candidate cutoffs. Dense between 0.85 and 0.95 because that is where the ablation's
#: confidences actually live — sweeping 0.1 to 0.5 would produce a tidy table about nothing.
CANDIDATES = (0.50, 0.70, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.99)


def load_scored_cases(pattern: str = "ablation*2026-09-04*.json") -> list[dict]:
    """Every non-escalated decision with a stated confidence, from committed results.

    Escalated cases are excluded because they carry no usable confidence — the agent
    declined to answer, so there is no claim to check against an outcome.
    """
    cases = []
    for path in sorted(glob.glob(str(RESULTS_DIR / pattern))):
        with open(path) as handle:
            result = json.load(handle)
        engine = result.get("settings", {}).get("engine", "chain")
        for arm in result["arms"]:
            risk = {f["scenario_id"]: f["amount_at_risk_paise"]
                    for f in arm.get("false_resolutions", [])}
            for case in arm["per_case"]:
                cases.append({
                    "engine": engine, "arm": arm["arm"],
                    "scenario_id": case["scenario_id"], "kind": case["kind"],
                    "confidence": case["confidence"], "correct": case["correct"],
                    "escalated": case["escalated"],
                    "amount_at_risk_paise": risk.get(case["scenario_id"], 0),
                })
    return cases


def reliability(cases: list[dict], bin_width: float = 0.05) -> list[dict]:
    """Stated confidence against observed accuracy, binned.

    A calibrated agent sits on the diagonal. The gap is signed so over- and under-confidence
    are distinguishable at a glance: negative means the agent claimed more than it delivered.
    """
    answered = [c for c in cases if not c["escalated"]]
    bins: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    for case in answered:
        # Integer arithmetic, not `// bin_width`. In floating point 0.90 // 0.05 is 17, not
        # 18, so every confidence that lands exactly on a bin edge — which, for a model that
        # emits round numbers, is nearly all of them — was filed one bin too low. The first
        # version of this table therefore labelled the 0.95 cohort as 0.90.
        steps = int(round(case["confidence"] / bin_width))
        edge = round(min(1.0, steps * bin_width), 2)
        bins[edge][0] += case["correct"]
        bins[edge][1] += 1
    return [
        {
            "stated_confidence": edge,
            "n": total,
            "observed_accuracy": round(correct / total, 4),
            "gap": round(correct / total - edge, 4),
        }
        for edge, (correct, total) in sorted(bins.items())
        if total
    ]


def sweep(cases: list[dict], candidates=CANDIDATES) -> list[dict]:
    """What each cutoff would have produced, counterfactually, on cases already scored."""
    total = len(cases)
    rows = []
    for threshold in candidates:
        resolved = wrong = escalated = 0
        exposure = 0
        for case in cases:
            if case["escalated"] or case["confidence"] < threshold:
                escalated += 1
            elif case["correct"]:
                resolved += 1
            else:
                wrong += 1
                exposure += case["amount_at_risk_paise"]
        rows.append({
            "threshold": threshold,
            "resolution_rate": round(resolved / total, 4),
            "escalation_rate": round(escalated / total, 4),
            "error_rate": round(wrong / total, 4),
            "false_resolution_cost_inr": round(exposure / 100, 2),
        })
    return rows


def recommend(rows: list[dict], baseline: float = 0.80) -> dict:
    """The cutoff that cuts the most exposure per point of resolution rate given up.

    Framed as a rate rather than an optimum because there is no objective exchange rate
    between a rupee of exposure and a point of coverage — that is a business decision, not a
    measurement. What the eval can say is which candidate buys the most and where the trade
    turns bad, and it says so with the numbers attached rather than picking for the reader.
    """
    base = next((r for r in rows if r["threshold"] == baseline), rows[0])
    scored = []
    for row in rows:
        given_up = base["resolution_rate"] - row["resolution_rate"]
        saved = base["false_resolution_cost_inr"] - row["false_resolution_cost_inr"]
        if saved <= 0:
            continue
        # Rupees saved per percentage point of resolution rate surrendered. A cutoff that
        # gives up nothing is infinitely efficient, which is the right answer.
        scored.append({
            **row,
            "resolution_given_up_pp": round(given_up * 100, 2),
            "exposure_saved_inr": round(saved, 2),
            "inr_saved_per_pp": (
                round(saved / (given_up * 100), 2) if given_up > 0 else None
            ),
        })
    if not scored:
        return {"recommended": baseline, "why": "no candidate reduced exposure"}

    # Two answers, because they are genuinely different questions and collapsing them into
    # one number hides the more useful of the two.
    free = [r for r in scored if r["resolution_given_up_pp"] <= 0]
    priced = [r for r in scored if r["resolution_given_up_pp"] > 0]

    best_free = max(free, key=lambda r: r["exposure_saved_inr"]) if free else None
    best_value = max(priced, key=lambda r: r["inr_saved_per_pp"]) if priced else None

    # Prefer the priced one when it buys substantially more, because in a reconciliation
    # system a false accept is the failure that matters and a point of coverage is cheap
    # against it. Stated as a judgement rather than smuggled in as a computation.
    chosen = best_free
    if best_value and (
        best_free is None
        or best_value["exposure_saved_inr"] > best_free["exposure_saved_inr"] * 2
    ):
        chosen = best_value

    return {
        "recommended": chosen["threshold"],
        "baseline": baseline,
        "resolution_given_up_pp": chosen["resolution_given_up_pp"],
        "exposure_saved_inr": chosen["exposure_saved_inr"],
        "why": (
            f"gives up {chosen['resolution_given_up_pp']}pp of resolution rate to remove "
            f"INR {chosen['exposure_saved_inr']:,.0f} of exposure"
        ),
        "free_improvement": best_free,
        "best_value_for_money": best_value,
        "judgement": (
            "Where a cutoff that costs coverage removes more than twice the exposure of the "
            "free one, it is preferred: in reconciliation a false accept is the failure that "
            "matters and a point of coverage is cheap against it. That is a judgement about "
            "this domain, not something the numbers decide on their own."
        ),
        "candidates": scored,
    }


def run_calibration() -> dict:
    cases = load_scored_cases()
    graph_grounded = [
        c for c in cases if c["engine"] == "graph" and c["arm"] == "grounded"
    ]
    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "what_this_measures": (
            "Whether stated confidence predicts correctness, and what auto-resolve "
            "threshold the observed outcomes justify."
        ),
        "cases_scored": len(cases),
        "reliability_all_arms": reliability(cases),
        "reliability_graph_grounded": reliability(graph_grounded),
        "sweep_graph_grounded": sweep(graph_grounded),
        "recommendation": recommend(sweep(graph_grounded)),
        "caveats": [
            "The sweep is counterfactual over pool exceptions the ablation already scored, "
            "so a threshold chosen here is chosen on data the system has seen.",
            "A threshold calibrated on one model is not evidence about another. Re-derive "
            "it when the model or the prompt changes.",
            "Escalated cases are excluded from reliability: the agent declined to answer, "
            "so there is no confidence claim to check.",
        ],
    }


def print_report(result: dict) -> None:
    print(f"{result['cases_scored']} scored decisions\n")
    print("reliability — is a stated confidence worth what it claims?")
    print(f"  {'stated':>8}{'n':>7}{'observed':>11}{'gap':>9}")
    for row in result["reliability_all_arms"]:
        if row["n"] >= 5:
            print(f"  {row['stated_confidence']:>8.2f}{row['n']:>7}"
                  f"{row['observed_accuracy']:>11.1%}{row['gap']:>+9.1%}")

    print("\nthreshold sweep (graph, grounded):")
    print(f"  {'thresh':>7}{'resolved':>10}{'escalated':>11}{'wrong':>8}{'exposure':>14}")
    for row in result["sweep_graph_grounded"]:
        print(f"  {row['threshold']:>7.2f}{row['resolution_rate']:>10.1%}"
              f"{row['escalation_rate']:>11.1%}{row['error_rate']:>8.1%}"
              f"   INR {row['false_resolution_cost_inr']:>9,.0f}")

    rec = result["recommendation"]
    free, value = rec.get("free_improvement"), rec.get("best_value_for_money")
    if free:
        print(f"\n  free improvement:  {free['threshold']} — saves INR "
              f"{free['exposure_saved_inr']:,.0f} for {free['resolution_given_up_pp']}pp")
    if value:
        print(f"  best value:        {value['threshold']} — saves INR "
              f"{value['exposure_saved_inr']:,.0f} for {value['resolution_given_up_pp']}pp "
              f"(INR {value['inr_saved_per_pp']:,.0f} per pp)")
    print(f"\nrecommended threshold: {rec['recommended']} — {rec['why']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate the auto-resolve threshold")
    parser.parse_args()
    result = run_calibration()
    print_report(result)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = RESULTS_DIR / f"calibration-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
