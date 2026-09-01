"""Ring 0.6 eval runner — Baseline #1: deterministic rules alone (spec §6).

Measured here, before any LLM code exists in the repo (spec §9, Ring 0 gate). Loads the
committed dataset, runs `match_batch` once per scenario (isolated — see
`dataset/generate.py`'s module docstring for why not pooled), scores against
`gold.jsonl`, and writes a timestamped, committed result to `evals/results/`.

    uv run python -m evals.runner
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evals.dataset.convert import to_domain_bank_line, to_domain_ledger_entry, to_domain_payment
from evals.dataset.loader import LoadedScenario, load_dataset
from precedent.domain.matching import MatchBatchResult, match_batch

_RESULTS_DIR = Path(__file__).parent / "results"


def _run_matcher(scenario: LoadedScenario) -> MatchBatchResult:
    payments = [to_domain_payment(p) for p in scenario.payments]
    bank_lines = [to_domain_bank_line(b) for b in scenario.bank_lines]
    ledger_entries = [to_domain_ledger_entry(l) for l in scenario.ledger_entries]
    return match_batch(payments, bank_lines, ledger_entries)


def _scenario_resolved_by_rules(scenario: LoadedScenario, result: MatchBatchResult) -> bool:
    """A scenario counts as auto-resolved only if every one of its payments is accounted
    for by a successful match AND nothing about it was flagged as an exception —
    "one match happened" isn't enough: `duplicate_payment` produces one legitimate match
    *and* one rejection, and that rejection still needs a human/agent decision, so the
    scenario as a whole is not rules-resolved."""
    if result.exceptions:
        return False
    matched_payment_ids = {pid for m in result.matches for pid in m.payment_ids}
    return matched_payment_ids == {p.payment_id for p in scenario.payments}


def run_baseline_rules_alone() -> dict:
    scenarios = load_dataset()

    per_class: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "resolved": 0})
    exception_list = []
    gold_mismatches = []
    false_accepts = []
    resolved_count = 0
    test_set_resolved = test_set_total = 0

    for scenario in scenarios:
        result = _run_matcher(scenario)
        resolved = _scenario_resolved_by_rules(scenario, result)

        per_class[scenario.kind]["total"] += 1
        if resolved:
            per_class[scenario.kind]["resolved"] += 1
            resolved_count += 1

        # Correct behavior is resolved == (not is_exception); flag anything else —
        # either the matcher wrongly cleared something meant to resist it, or it failed
        # on something meant to be clean.
        if resolved == scenario.is_exception:
            gold_mismatches.append(
                {
                    "scenario_id": scenario.scenario_id, "kind": scenario.kind,
                    "gold_is_exception": scenario.is_exception, "rules_resolved": resolved,
                }
            )

        if scenario.kind == "duplicate_payment" and len(result.matches) > 1:
            false_accepts.append(scenario.scenario_id)

        if not resolved:
            exception_list.append(
                {
                    "scenario_id": scenario.scenario_id, "kind": scenario.kind,
                    "expected_reason_code": scenario.expected_reason_code,
                    "pool_or_test": scenario.pool_or_test,
                }
            )

        if scenario.pool_or_test == "test":
            test_set_total += 1
            if resolved:
                test_set_resolved += 1

    total = len(scenarios)
    per_class_out = {
        kind: {**counts, "resolution_rate": counts["resolved"] / counts["total"]}
        for kind, counts in sorted(per_class.items())
    }

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M"),
        "baseline": "deterministic_rules_alone",
        "corpus_size": 0,
        "dataset": {
            "total_records": total,
            "resolved_by_rules": resolved_count,
            "exceptions": total - resolved_count,
        },
        "metrics": {
            "autonomous_resolution_rate": resolved_count / total,
            "autonomous_resolution_rate_on_test_set": (
                test_set_resolved / test_set_total if test_set_total else None
            ),
            "escalation_rate": (total - resolved_count) / total,
            "false_accept_count": len(false_accepts),
        },
        "per_class_breakdown": per_class_out,
        "gold_matcher_agreement": {
            "mismatches": len(gold_mismatches),
            "total_checked": total,
            "mismatch_detail": gold_mismatches,
        },
        "false_accepts": false_accepts,
        "exception_list": exception_list,
    }


def main() -> None:
    result = run_baseline_rules_alone()
    _RESULTS_DIR.mkdir(exist_ok=True)
    out_path = _RESULTS_DIR / f"{result['run_id']}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote {out_path}")
    print(f"Autonomous resolution rate (rules alone): {result['metrics']['autonomous_resolution_rate']:.1%}")
    print(f"Escalation rate: {result['metrics']['escalation_rate']:.1%}")
    print(f"Gold/matcher agreement mismatches: {result['gold_matcher_agreement']['mismatches']}")
    print(f"Duplicate-payment false accepts: {len(result['false_accepts'])}")


if __name__ == "__main__":
    main()
