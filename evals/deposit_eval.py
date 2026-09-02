"""Does one deposit prompt author *better precedents* than another?

Spec §4: "Prompt versions for precedent authoring live in `prompts/deposit/v1.md`, `v2.md`,
… and the eval measures whether v3 precedents retrieve better than v2. **Prompt engineering
with a number attached.**" This is that number.

**Why it needs its own eval rather than riding on the ablation.** A precedent can fail in two
unrelated ways and the end-to-end resolution rate cannot tell them apart: it can be
*unfindable* (retrieval never surfaces it) or *unhelpful* (retrieved, and the agent still gets
the case wrong). Those have opposite fixes — the first is about what goes in the `situation`,
the second about what goes in the `resolution` — so a single number that blends them cannot
guide the prompt. Both are reported separately here.

**The protocol.** For each of the nine counterparty customers:

1. Author a precedent from that customer's **first sighting** — the case a human resolves
   because nobody could resolve it — using the prompt version under test.
2. Build a corpus of the seed precedents plus exactly that authored precedent.
3. Run that customer's **other sightings** through the graph against that corpus.

The counterparty classes are the only ones this can be measured on, and that is the point:
every other class is derivable from the evidence, so a precedent about it is redundant and
its quality unmeasurable. Here the deposited precedent is the only thing that can possibly
resolve the case.

Each customer is scored against a corpus containing **only their own** authored precedent, so
one customer's good deposit cannot mask another's bad one.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.cache import CachingLLM
from evals.dataset.loader import load_dataset
from evals.retrieval_eval import scenario_to_case
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import run_investigation
from precedent.usecases.deposit import author_precedent

RESULTS_DIR = Path(__file__).parent / "results"

#: The authored precedent is stamped at version 1 — one deposit past the seeds.
AUTHORED_VERSION = 1
AUTHORED_AT = "2026-09-02T00:00:00+00:00"


def counterparty_groups() -> dict[str, dict]:
    """Each customer's first sighting (the deposit source) and the rest (the measurement)."""
    scenarios = [s for s in load_dataset() if s.counterparty]
    groups: dict[str, dict] = {}
    for scenario in sorted(scenarios, key=lambda s: (s.counterparty, s.occurrence_index)):
        entry = groups.setdefault(
            scenario.counterparty, {"first": None, "later": [], "kind": scenario.kind}
        )
        if entry["first"] is None:
            entry["first"] = scenario
        else:
            entry["later"].append(scenario)
    return groups


def evaluate_prompt(llm, prompt_version: str, k: int = 5) -> dict:
    seeds = seed_precedent_records()
    per_customer = []
    authored_ok = 0
    retrieved_hits = retrieved_total = 0
    resolved_hits = resolved_total = 0

    for customer, group in counterparty_groups().items():
        first, later = group["first"], group["later"]
        if not later:
            continue

        case = scenario_to_case(first)
        record = None
        author_error = None
        try:
            precedent = author_precedent(
                llm, case, ReasonCode(first.expected_reason_code),
                f"Confirmed by the reconciliation lead: {first.notes}",
                "confirmed", prompt_version=prompt_version,
            )
            record = precedent.to_record("prec_0001", AUTHORED_AT, AUTHORED_VERSION)
            authored_ok += 1
        except Exception as error:  # noqa: BLE001 - an unusable precedent is a result
            author_error = f"{type(error).__name__}: {error}"

        # Seeds plus this customer's own precedent, and nobody else's.
        corpus = seeds + ([record] if record else [])
        retriever = BM25Retriever(corpus)

        sightings = []
        for scenario in later:
            later_case = scenario_to_case(scenario)
            hits = retriever.retrieve(later_case.retrieval_query(), k)
            found = record is not None and any(
                h.record.precedent_id == record.precedent_id for h in hits
            )
            retrieved_total += 1
            retrieved_hits += found

            outcome, _trace = run_investigation(later_case, llm, retriever=retriever, k=k)
            correct = outcome.reason_code.value == scenario.expected_reason_code
            resolved_total += 1
            resolved_hits += correct
            sightings.append({
                "scenario_id": scenario.scenario_id,
                "occurrence": scenario.occurrence_index,
                "split": scenario.pool_or_test,
                "deposit_retrieved": found,
                "resolved_correctly": correct,
                "said": outcome.reason_code.value,
                "cited_the_deposit": record is not None
                and record.precedent_id in outcome.cited_precedent_ids,
            })

        per_customer.append({
            "customer": customer,
            "kind": group["kind"],
            "authored": record is not None,
            "author_error": author_error,
            "situation": record.situation if record else None,
            # Recorded because a v2 regression was nearly misdiagnosed as a resolution
            # problem when the resolution text was fine and the situation had stopped
            # carrying any claim. Both halves have to be inspectable.
            "resolution": record.resolution if record else None,
            "amount_signature": record.amount_signature if record else None,
            "names_the_customer": bool(record and customer in record.situation),
            "situation_chars": len(record.situation) if record else 0,
            "sightings": sightings,
        })

    customers = [c for c in per_customer if c["authored"]]
    return {
        "prompt_version": prompt_version,
        "customers_scored": len(per_customer),
        "metrics": {
            # Separated deliberately: unfindable and unhelpful have opposite fixes.
            "authored_successfully": f"{authored_ok}/{len(per_customer)}",
            "deposit_retrieval_rate": round(retrieved_hits / retrieved_total, 4)
            if retrieved_total else None,
            "resolution_rate_on_later_sightings": round(resolved_hits / resolved_total, 4)
            if resolved_total else None,
            "names_the_customer_rate": round(
                sum(c["names_the_customer"] for c in customers) / len(customers), 4
            ) if customers else None,
            "mean_situation_chars": int(
                sum(c["situation_chars"] for c in customers) / len(customers)
            ) if customers else None,
        },
        "per_customer": per_customer,
    }


def mcnemar_between(results: list[dict]) -> dict:
    """Paired significance between every pair of prompt versions.

    The same nine customers and the same later sightings are scored under each version, so
    the comparisons are paired. Reported because the headline is easy to over-read: v2's
    56% -> 63% looks like an improvement and is 9W-7L at p=0.80 — noise, while two customers
    that v1 resolved 3/3 collapsed to 0/3 underneath it.
    """
    def flat(version: dict) -> dict:
        return {
            s["scenario_id"]: s["resolved_correctly"]
            for c in version["per_customer"] for s in c["sightings"]
        }

    out = {}
    for i, a in enumerate(results):
        for b in results[i + 1:]:
            fa, fb = flat(a), flat(b)
            wins = sum(1 for k in fa if fb[k] and not fa[k])
            losses = sum(1 for k in fa if fa[k] and not fb[k])
            n = wins + losses
            p = (sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n * 2
                 if n else 1.0)
            out[f"{b['prompt_version']}_vs_{a['prompt_version']}"] = {
                "wins": wins, "losses": losses, "p_value": round(min(p, 1.0), 4),
                "significant_at_05": min(p, 1.0) < 0.05,
            }
    return out


def print_report(results: list[dict]) -> None:
    header = (f"{'prompt':10}{'authored':>11}{'names cust':>12}{'retrieved':>11}"
              f"{'resolved':>10}{'situation':>11}")
    print(header)
    print("-" * len(header))
    for result in results:
        m = result["metrics"]
        print(
            f"{result['prompt_version']:10}{m['authored_successfully']:>11}"
            f"{(m['names_the_customer_rate'] or 0):>12.0%}"
            f"{(m['deposit_retrieval_rate'] or 0):>11.0%}"
            f"{(m['resolution_rate_on_later_sightings'] or 0):>10.0%}"
            f"{str(m['mean_situation_chars'] or 0) + ' ch':>11}"
        )
    if len(results) > 1:
        print("\npaired significance on resolution (exact McNemar):")
        for name, test in mcnemar_between(results).items():
            verdict = "significant" if test["significant_at_05"] else "NOT significant"
            print(f"  {name:16} {test['wins']}W-{test['losses']}L  "
                  f"p={test['p_value']:.4f}  {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare deposit prompt versions")
    parser.add_argument("versions", nargs="*", default=["v1"],
                        help="prompt versions to compare, e.g. v1 v2")
    parser.add_argument("--provider", default="nvidia",
                        choices=("nvidia", "groq", "gemini", "ollama"))
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    from scripts.trace_investigation import build_llm

    llm = CachingLLM(build_llm(args.provider, None))
    results = [evaluate_prompt(llm, version, args.k) for version in args.versions]
    print_report(results)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = RESULTS_DIR / f"deposit-prompt-{stamp}.json"
    path.write_text(json.dumps({
        "run_id": stamp,
        "model": llm.model,
        "what_this_measures": (
            "Whether a deposit prompt authors precedents that can be found and that help. "
            "Measured only on the counterparty classes, where the deposited precedent is "
            "the only thing that can resolve the case."
        ),
        "versions": results,
        "significance": mcnemar_between(results),
        "caveat": (
            "Authoring success, the absence of rupee amounts, and retrieval are near-"
            "deterministic properties of the authored text and can be read directly. The "
            "resolution rate cannot: at 27 sightings no pairwise comparison reaches "
            "significance, and per-customer variance is high — the same prompt authors "
            "materially different quality for different cases."
        ),
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
