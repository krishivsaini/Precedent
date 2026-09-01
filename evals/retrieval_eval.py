"""Retrieval quality, isolated from end-to-end accuracy (spec §6).

Spec §6 requires two numbers this module produces and one discipline it enforces:

* **Precedent precision** — retrieval quality measured on its own, "not hidden inside
  end-to-end accuracy". Here: how often the corpus surfaces a precedent of the class the
  case actually belongs to.
* **BM25-only vs dense-only vs hybrid** — "justifies the hybrid choice with data". Note the
  wording: it justifies the choice, whatever the data says. If hybrid loses, hybrid loses.
* **The random-precedent control on every run**, unprompted.

No LLM is involved. This measures the retriever alone, so a bad number here cannot be
explained away as a reasoning failure later.

The proxy and its limit: a hit means a retrieved precedent shares the case's gold reason
code. That is a proxy for "actually applied" — a precedent of the right class can still be
the wrong precedent. It is measurable without a judge, which end-to-end precedent precision
(Ring 3) is not, so it is reported as what it is rather than as the final word.

Run against the **62 pool exceptions only**. The 60 test exceptions are never touched here;
they are replayed at each corpus snapshot from Ring 3.4, and looking at them now would
contaminate the held-out split for the sake of a number nobody needs yet.
"""

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from evals.dataset.loader import load_dataset
from evals.dataset.scenario import Scenario
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.dense import DenseRetriever, HashingEmbedder
from precedent.adapters.retrieval.hybrid import HybridRetriever
from precedent.adapters.retrieval.random_control import RandomRetriever
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.case import ReconciliationCase

RESULTS_DIR = Path(__file__).parent / "results"

#: Fixed so the control is reproducible across runs and machines.
RANDOM_CONTROL_SEED = 20260901

K_VALUES = (1, 3, 5)
MAX_K = max(K_VALUES)


def scenario_to_case(scenario: Scenario) -> ReconciliationCase:
    return ReconciliationCase(
        case_id=scenario.scenario_id,
        payments=list(scenario.payments),
        bank_lines=list(scenario.bank_lines),
        ledger_entries=list(scenario.ledger_entries),
    )


def _score_retriever(name: str, retriever, scenarios: list[Scenario]) -> dict:
    hits_at = {k: 0 for k in K_VALUES}
    per_class: dict[str, dict] = defaultdict(lambda: {k: 0 for k in K_VALUES} | {"total": 0})
    misses: list[dict] = []

    for scenario in scenarios:
        query = scenario_to_case(scenario).retrieval_query()
        codes = [hit.record.reason_code for hit in retriever.retrieve(query, MAX_K)]
        want = scenario.expected_reason_code
        per_class[scenario.kind]["total"] += 1
        for k in K_VALUES:
            if want in codes[:k]:
                hits_at[k] += 1
                per_class[scenario.kind][k] += 1
        if want not in codes:
            misses.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind,
                    "expected_reason_code": want,
                    "retrieved_reason_codes": codes,
                }
            )

    total = len(scenarios)
    return {
        "retriever": name,
        "precedent_class_precision": {
            f"top_{k}": round(hits_at[k] / total, 4) if total else 0.0 for k in K_VALUES
        },
        "per_class": {
            kind: {
                **{f"top_{k}": f"{counts[k]}/{counts['total']}" for k in K_VALUES},
                "top_3_rate": round(counts[3] / counts["total"], 4),
            }
            for kind, counts in sorted(per_class.items())
        },
        "complete_misses": misses,
    }


def run_retrieval_eval() -> dict:
    records = seed_precedent_records()
    scenarios = [
        s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"
    ]

    dense = DenseRetriever(records, embedder=HashingEmbedder())
    hybrid = HybridRetriever(records, embedder=HashingEmbedder())
    try:
        arms = [
            _score_retriever("bm25", BM25Retriever(records), scenarios),
            _score_retriever("dense", dense, scenarios),
            _score_retriever("hybrid", hybrid, scenarios),
            _score_retriever(
                "random_control", RandomRetriever(records, seed=RANDOM_CONTROL_SEED), scenarios
            ),
        ]
    finally:
        dense.close()
        hybrid.close()

    by_name = {arm["retriever"]: arm for arm in arms}
    best = max(
        (a for a in arms if a["retriever"] != "random_control"),
        key=lambda a: a["precedent_class_precision"]["top_3"],
    )
    control_top3 = by_name["random_control"]["precedent_class_precision"]["top_3"]

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "what_this_measures": (
            "Whether the corpus surfaces a precedent of the case's own class. Proxy for "
            "precedent precision; no LLM involved."
        ),
        "corpus": {
            "size": len(records),
            "corpus_version": 0,
            "composition": "hand-written seed corpus only — no deposited precedents yet",
        },
        "dataset": {
            "scenarios_scored": len(scenarios),
            "split": "pool exceptions only; the 60 test exceptions are untouched",
        },
        "embedder": {
            "name": "HashingEmbedder",
            "semantic": False,
            "caveat": (
                "Feature hashing over surface tokens. Cannot relate 'withholding' to 'TDS'. "
                "The dense and hybrid arms below are therefore lower bounds; a real "
                "embedding model is required before any claim that hybrid beats lexical."
            ),
        },
        "arms": arms,
        "verdict": {
            "best_retriever": best["retriever"],
            "best_top_3": best["precedent_class_precision"]["top_3"],
            "random_control_top_3": control_top3,
            "beats_random_control": (
                best["precedent_class_precision"]["top_3"] > control_top3
            ),
        },
    }


def write_result(result: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = RESULTS_DIR / f"retrieval-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> None:
    result = run_retrieval_eval()
    path = write_result(result)

    print(f"corpus: {result['corpus']['size']} seed precedents, "
          f"{result['dataset']['scenarios_scored']} pool exceptions\n")
    header = f"{'retriever':16}" + "".join(f"top-{k:<6}" for k in K_VALUES)
    print(header)
    print("-" * len(header))
    for arm in result["arms"]:
        precision = arm["precedent_class_precision"]
        row = "".join(f"{precision[f'top_{k}']:<10.1%}" for k in K_VALUES)
        print(f"{arm['retriever']:16}{row}")

    best = next(a for a in result["arms"] if a["retriever"] == result["verdict"]["best_retriever"])
    print(f"\nper class ({result['verdict']['best_retriever']}):")
    print(f"  {'':28}" + "".join(f"top-{k:<7}" for k in K_VALUES))
    for kind, counts in best["per_class"].items():
        row = "".join(f"{counts[f'top_{k}']:<11}" for k in K_VALUES)
        print(f"  {kind:28}{row}")
    if best["complete_misses"]:
        print(f"\n  {len(best['complete_misses'])} scenario(s) retrieved no same-class "
              f"precedent at k={MAX_K}")

    verdict = result["verdict"]
    print(
        f"\nbest {verdict['best_top_3']:.1%} vs random control "
        f"{verdict['random_control_top_3']:.1%} — "
        f"{'beats control' if verdict['beats_random_control'] else 'DOES NOT BEAT CONTROL'}"
    )
    print(f"\nwritten to {path}")


if __name__ == "__main__":
    main()
