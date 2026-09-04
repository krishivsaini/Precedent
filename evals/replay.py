"""The learning curve: the held-out test set replayed at growing corpus sizes (spec §6).

> Same questions, growing knowledge, measured every time.

This is the headline artifact, and it is the one number the whole project exists to produce.
It is also the one most easily faked, so three things are structural rather than optional:

* **The test set is never deposited.** The 60 held-out exceptions are replayed unchanged at
  every snapshot. Depositing a resolution from one of them would let the system answer from
  its own memory of that exact case, and the curve would measure recall.
* **The random-precedent control runs at every snapshot**, automatically, at the same *k*
  (spec §6, control #2). If a corpus of random precedents lifts the curve as much as the
  retrieved one, the curve is a prompt-length artifact. Running it automatically is the point
  — a control that has to be remembered is a control that gets skipped under time pressure.
* **Snapshots cut on `corpus_version`, not wall-clock time.** Version *n* means "after
  exactly n deposits", so a replay is reproducible rather than approximately repeatable.

**Snapshot scale.** Spec §6 asks for 0/50/100/150/200 deposited precedents. The corpus cannot
exceed 176 — 42 hand-written seeds plus 134 depositable pool exceptions, one precedent per
resolution — so 200 is unreachable and 150 sits past the last useful point. That is a contradiction in the
source spec rather than in this build: even at the spec's own ~70-pool/~40-seed sizing the
ceiling was ~110. Rescaled to quarters of the depositable pool, with the last point being
everything available rather than a round number that happens to fit.

**What the curve can and cannot show on this dataset.** Seven of the nine exception classes are
derivable from the evidence in front of the agent, and Ring 2 measured all three arms at
98-100% on them: no growing corpus can move a number that is already at the ceiling. The curve
is therefore driven by the **counterparty classes**, where the seed corpus reaches 0/15 and
0/12 at k=5 and only a deposited precedent can help. Both are reported — the headline over all
60, and the counterparty subset that is the only part with headroom — because a flat headline
concealing a real effect in a subset would be as misleading as the reverse.
"""

import argparse
import json
import math
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.cache import CachingLLM
from evals.dataset.loader import load_dataset
from evals.human import REALISTIC_REJECTION_RATE, SimulatedReviewer
from evals.retrieval_eval import RANDOM_CONTROL_SEED, scenario_to_case
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.random_control import RandomRetriever
from evals.ablation import MAX_UNAVAILABLE_SHARE, MIN_UNAVAILABLE_TO_ABORT
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import run_investigation
from precedent.usecases.deposit import author_precedent

RESULTS_DIR = Path(__file__).parent / "results"

#: Quarters of the depositable pool. See the module docstring for why not 0/50/100/150/200.
#: Rescaled when Ring 3's counterparty classes doubled — the pool went 98 -> 134, and a
#: snapshot scale that no longer reaches the end of the pool would truncate the curve.
SNAPSHOTS = (0, 33, 67, 100, 134)

#: Fixes the order pool cases are worked in. Cases arrive in arbitrary order in reality, so
#: a seeded shuffle models that honestly — and fixing the seed makes every snapshot
#: reproducible rather than a different corpus each run.
DEPOSIT_ORDER_SEED = 20260902


class ReplayAborted(RuntimeError):
    """A snapshot was too degraded to mean anything, so no curve was written.

    The same guard the ablation has, carried here late and at cost. A replay that loses the
    model partway through produces a *shape* — a curve that rises and then collapses — and
    that shape is far more convincing than a single bad number. The first realistic run
    reported 85.0% at 67 deposits falling to 3.3% at 134, with the random control at exactly
    0.0%; it read like corpus poisoning and was 58 of 60 cases failing to reach the provider.
    A control pinned at 0.0% is the tell, but nothing should depend on someone noticing it.
    """


def deposit_order(scenarios: list) -> list:
    pool = [s for s in scenarios if s.is_exception and s.pool_or_test == "pool"]
    shuffled = pool[:]
    random.Random(DEPOSIT_ORDER_SEED).shuffle(shuffled)
    return shuffled


def build_deposit_sequence(
    llm, scenarios: list, limit: int | None = None, workers: int = 4,
    reviewer: SimulatedReviewer | None = None, retriever_for_pool=None, k: int = 5,
) -> list[dict]:
    """Work each pool exception the way the system actually would, and deposit what a
    reviewer signs off.

    With `reviewer` set, this runs the **full loop**: the agent proposes, a simulated
    reviewer confirms, corrects or rejects, and only confirmations and corrections deposit.
    That matters for two reasons beyond realism. Rejections mean the corpus grows more slowly
    than one precedent per case, which is what makes the curve an estimate rather than a
    ceiling. And corrections — which spec §7 calls the higher-value deposit, because they
    encode a case the system got wrong — are otherwise never exercised end to end.

    With `reviewer` unset the old behaviour remains: every case confirmed at the gold answer,
    which is the optimistic bound and is labelled as such in the result.
    """
    ordered = deposit_order(scenarios)[: limit or None]

    def author(indexed):
        index, scenario = indexed
        case = scenario_to_case(scenario)

        review = None
        if reviewer is not None:
            # Run the agent first: there has to be a proposal for a reviewer to judge.
            outcome, _trace = run_investigation(
                case, llm, retriever=retriever_for_pool, k=k
            )
            review = reviewer.review(
                scenario.scenario_id, scenario.expected_reason_code,
                None if outcome.escalated else outcome.reason_code.value,
                outcome.escalated,
            )
            if not review.deposits:
                return {"record": None, "scenario_id": scenario.scenario_id,
                        "kind": scenario.kind, "ok": False,
                        "human_action": review.human_action,
                        "error": "reviewer rejected; nothing deposited"}

        action = review.human_action if review else "confirmed"
        note = review.correction_note if review else ""
        try:
            precedent = author_precedent(
                llm, case, ReasonCode(scenario.expected_reason_code),
                f"{action.capitalize()} by the reconciliation lead: {scenario.notes}",
                action, correction_note=note,
            )
            record = precedent.to_record(
                precedent_id=f"prec_{index:04d}",
                deposited_at="2026-09-02T00:00:00+00:00",
                corpus_version=index,
            )
            return {"record": record, "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind, "ok": True,
                    "human_action": action}
        except Exception as error:  # noqa: BLE001 - a failed authoring is a real outcome
            # The version still advances: a deposit that could not be authored is a gap in
            # the corpus, and pretending it did not happen would overstate corpus growth.
            return {"record": None, "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind, "ok": False,
                    "human_action": action, "error": str(error)[:200]}

    # Concurrent, but the sequence order is the deposit order — corpus_version is assigned
    # from position, so the snapshots stay reproducible regardless of completion order.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        sequence = list(pool.map(author, enumerate(ordered, start=1)))
    actions = Counter(e.get("human_action", "confirmed") for e in sequence)
    print(f"  authored {sum(1 for e in sequence if e['ok'])}/{len(sequence)}"
          f"  ({dict(actions)})", flush=True)
    return sequence


def corpus_at(sequence: list[dict], deposits: int) -> list:
    """The corpus as it stood after exactly `deposits` deposits: seeds plus the first N."""
    grown = [entry["record"] for entry in sequence[:deposits] if entry["record"] is not None]
    return seed_precedent_records() + grown


def deposit_provenance(sequence: list[dict]) -> dict[str, str]:
    """Precedent id to the human action that produced it.

    Spec §7 asserts that a corrected resolution is the *higher-value* precedent, because it
    encodes a case the system got wrong. That was untestable while every deposit was a
    confirmation; with a reviewer producing both, it becomes a measurement — so citations are
    attributed back to how the cited precedent came to exist.
    """
    return {
        entry["record"].precedent_id: entry.get("human_action", "confirmed")
        for entry in sequence
        if entry["record"] is not None
    }


def replay_test_set(
    llm, scenarios: list, corpus: list, k: int, control: bool, workers: int = 4,
    provenance: dict[str, str] | None = None,
) -> dict:
    """Run the held-out test set against one corpus snapshot.

    Concurrent, with order restored from the input list afterwards — the full curve is 600
    graph runs across five snapshots and two arms, and sequentially that is several hours of
    wall clock for a result the cache then makes free to reproduce.
    """
    test_set = [s for s in scenarios if s.is_exception and s.pool_or_test == "test"]
    retriever = (
        RandomRetriever(corpus, seed=RANDOM_CONTROL_SEED) if control
        else BM25Retriever(corpus)
    )

    def attempt(scenario):
        outcome, _trace = run_investigation(
            scenario_to_case(scenario), llm, retriever=retriever, k=k
        )
        return scenario, outcome

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = list(pool.map(attempt, test_set))

    unreachable = [
        o for _s, o in pairs
        if o.reason_code is ReasonCode.ESCALATED_MODEL_UNAVAILABLE
    ]
    if unreachable:
        print(f"    {len(unreachable)}/{len(pairs)} could not reach the model", flush=True)
    if len(unreachable) > max(MIN_UNAVAILABLE_TO_ABORT,
                              len(pairs) * MAX_UNAVAILABLE_SHARE):
        raise ReplayAborted(
            f"{len(unreachable)}/{len(pairs)} cases could not reach the model at this "
            f"snapshot. That measures the provider, not the corpus — no curve was written."
        )

    correct = escalated = 0
    counterparty_correct = counterparty_total = 0
    cited_total = cited_applied = 0
    # Citations attributed to how the cited precedent came to exist, so spec §7's claim
    # about corrections can be checked rather than repeated.
    by_origin: dict[str, dict[str, int]] = {
        "seed": {"cited": 0, "applied": 0},
        "confirmed": {"cited": 0, "applied": 0},
        "corrected": {"cited": 0, "applied": 0},
    }
    per_case = []
    for scenario, outcome in pairs:
        hit = outcome.reason_code.value == scenario.expected_reason_code
        correct += hit
        escalated += outcome.escalated
        if scenario.counterparty:
            counterparty_total += 1
            counterparty_correct += hit

        # Precedent precision (spec §6): of the precedents cited, the fraction that actually
        # applied. Approximated by agreement with the gold reason code — a proxy, and named
        # as one, because a precedent of the right class can still be the wrong precedent.
        by_id = {r.precedent_id: r for r in corpus}
        for pid in outcome.cited_precedent_ids:
            cited_total += 1
            record = by_id.get(pid)
            applied = bool(record and record.reason_code == scenario.expected_reason_code)
            cited_applied += applied
            origin = (provenance or {}).get(pid, "seed")
            bucket = by_origin.setdefault(origin, {"cited": 0, "applied": 0})
            bucket["cited"] += 1
            bucket["applied"] += applied

        per_case.append({
            "scenario_id": scenario.scenario_id, "kind": scenario.kind,
            "counterparty": scenario.counterparty,
            "gold": scenario.expected_reason_code, "said": outcome.reason_code.value,
            "correct": hit, "escalated": outcome.escalated,
        })

    total = len(test_set)
    return {
        "resolution_rate": round(correct / total, 4),
        "escalation_rate": round(escalated / total, 4),
        "counterparty_resolution_rate": (
            round(counterparty_correct / counterparty_total, 4)
            if counterparty_total else None
        ),
        "counterparty_cases": counterparty_total,
        "precedent_precision": (
            round(cited_applied / cited_total, 4) if cited_total else None
        ),
        "citations_made": cited_total,
        "precision_by_deposit_origin": {
            origin: {
                "cited": counts["cited"],
                "applied": counts["applied"],
                "precision": (
                    round(counts["applied"] / counts["cited"], 4) if counts["cited"] else None
                ),
            }
            for origin, counts in by_origin.items()
        },
        "per_case": per_case,
    }


def run_replay(llm, k: int = 5, snapshots=SNAPSHOTS, deposit_limit: int | None = None,
               workers: int = 4, rejection_rate: float | None = None) -> dict:
    """`rejection_rate=None` keeps the optimistic bound; a number runs the full review loop."""
    scenarios = load_dataset()
    reviewer = (
        SimulatedReviewer(rejection_rate) if rejection_rate is not None else None
    )
    print("building the deposit sequence...", flush=True)
    sequence = build_deposit_sequence(
        llm, scenarios, deposit_limit, workers,
        reviewer=reviewer,
        # The pool is worked against the seed corpus, as it would be on day one.
        retriever_for_pool=BM25Retriever(seed_precedent_records()) if reviewer else None,
        k=k,
    )
    authored = sum(1 for e in sequence if e["ok"])

    points = []
    for deposits in snapshots:
        if deposits > len(sequence):
            continue
        corpus = corpus_at(sequence, deposits)
        print(f"replaying at {deposits} deposits (corpus {len(corpus)})...", flush=True)
        provenance = deposit_provenance(sequence[:deposits])
        real = replay_test_set(llm, scenarios, corpus, k, False, workers, provenance)
        control = replay_test_set(llm, scenarios, corpus, k, True, workers, provenance)
        points.append({
            "deposits": deposits,
            "corpus_size": len(corpus),
            "retrieved": real,
            "random_control": control,
        })

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": llm.model,
        "settings": {"k": k, "snapshots": list(snapshots),
                     "deposit_order_seed": DEPOSIT_ORDER_SEED},
        "deposit_sequence": {
            "attempted": len(sequence),
            "authored": authored,
            "failed": len(sequence) - authored,
            "human_actions": dict(
                Counter(e.get("human_action", "confirmed") for e in sequence)
            ),
            "reviewer": (
                {"simulated": True, "rejection_rate": reviewer.rejection_rate}
                if reviewer else
                {"simulated": False,
                 "note": "every resolution confirmed at the gold answer — optimistic bound"}
            ),
        },
        "test_set": {
            "size": len([s for s in scenarios if s.is_exception and s.pool_or_test == "test"]),
            "never_deposited": True,
        },
        "caveats": [
            (
                "Every pool resolution is confirmed at the gold answer, so no deposit is "
                "ever rejected and the corpus grows one precedent per case. This is the "
                "optimistic bound; pass --rejection-rate to run the full review loop."
                if reviewer is None else
                f"A simulated reviewer confirms the agent when it is right, corrects it "
                f"when it is wrong, and rejects {reviewer.rejection_rate:.0%} of cases "
                f"outright. The rejection rate is a stated assumption, not a measurement."
            ),
            "Seven of nine exception classes are derivable from the evidence and already sit "
            "at 98-100% with no corpus at all, so the headline rate has little room to move. "
            "The counterparty subset is the part with headroom and is reported separately.",
            "Precedent precision is approximated by reason-code agreement. A precedent of "
            "the right class can still be the wrong precedent.",
        ],
        "curve": points,
        "significance": curve_significance(points),
    }


def curve_significance(curve: list[dict]) -> dict:
    """Paired exact McNemar between the first and last snapshot.

    The same 60 held-out cases are replayed at every point, so the comparison is paired.
    Computed here rather than left to a reader, because "81.7% rose to 95.0%" over 60 cases
    is exactly the shape of claim that needs a p-value attached before it is believed — and
    the earlier prompt-version work in this project produced a 7-point rise that turned out
    to be noise (FAILURES.md).

    The control's own first-to-last comparison is included deliberately. A curve is only
    evidence of *learning* if the control does not rise with it; reporting the treatment
    alone would leave the prompt-length explanation open.
    """
    def flat(point, arm):
        return {c["scenario_id"]: c["correct"] for c in point[arm]["per_case"]}

    def mcnemar(a, b):
        wins = sum(1 for k in a if b[k] and not a[k])
        losses = sum(1 for k in a if a[k] and not b[k])
        n = wins + losses
        p = (sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n * 2
             if n else 1.0)
        return {"wins": wins, "losses": losses, "p_value": round(min(p, 1.0), 5),
                "significant_at_05": min(p, 1.0) < 0.05}

    if len(curve) < 2:
        return {}
    first, last = curve[0], curve[-1]

    def counterparty_only(point):
        return {c["scenario_id"]: c["correct"]
                for c in point["retrieved"]["per_case"] if c["counterparty"]}

    return {
        "headline_first_to_last": mcnemar(flat(first, "retrieved"), flat(last, "retrieved")),
        "control_first_to_last": mcnemar(
            flat(first, "random_control"), flat(last, "random_control")
        ),
        "counterparty_first_to_last": mcnemar(
            counterparty_only(first), counterparty_only(last)
        ),
    }


def print_report(result: dict) -> None:
    print(f"\nmodel: {result['model']}   k={result['settings']['k']}")
    seq = result["deposit_sequence"]
    print(f"deposits authored: {seq['authored']}/{seq['attempted']}"
          f"   test set: {result['test_set']['size']} (never deposited)\n")
    header = (f"{'deposits':>9}{'corpus':>8}{'resolved':>10}{'control':>9}"
              f"{'counterparty':>14}{'escalated':>11}{'prec.prec':>11}")
    print(header)
    print("-" * len(header))
    for point in result["curve"]:
        real, control = point["retrieved"], point["random_control"]
        cp = real["counterparty_resolution_rate"]
        precision = real["precedent_precision"]
        print(
            f"{point['deposits']:>9}{point['corpus_size']:>8}"
            f"{real['resolution_rate']:>10.1%}{control['resolution_rate']:>9.1%}"
            f"{(f'{cp:.1%}' if cp is not None else '-'):>14}"
            f"{real['escalation_rate']:>11.1%}"
            f"{(f'{precision:.1%}' if precision is not None else '-'):>11}"
        )

    last = result["curve"][-1]["retrieved"].get("precision_by_deposit_origin") or {}
    if any(v["cited"] for v in last.values()):
        print("\nprecedent precision by how the cited precedent came to exist (last snapshot):")
        for origin, counts in last.items():
            if counts["cited"]:
                print(f"  {origin:12} cited {counts['cited']:4}  "
                      f"applied {counts['applied']:4}  {counts['precision']:.1%}")

    tests = result.get("significance") or {}
    if tests:
        print("\npaired significance, first snapshot to last (exact McNemar):")
        for name, test in tests.items():
            verdict = "significant" if test["significant_at_05"] else "not significant"
            print(f"  {name:28} {test['wins']}W-{test['losses']}L  "
                  f"p={test['p_value']:.5f}  {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay the held-out test set at each snapshot")
    parser.add_argument("--provider", default="nvidia",
                        choices=("nvidia", "groq", "gemini", "ollama"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--snapshots", type=int, nargs="*", default=list(SNAPSHOTS))
    parser.add_argument("--deposit-limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--rejection-rate", type=float, default=None,
                        help="run the full review loop with this reviewer rejection rate "
                             f"(realistic default {REALISTIC_REJECTION_RATE}); omit for the "
                             "optimistic all-confirmed bound")
    args = parser.parse_args()

    from scripts.trace_investigation import build_llm

    llm = CachingLLM(build_llm(args.provider, None))
    result = run_replay(llm, args.k, tuple(args.snapshots), args.deposit_limit,
                        args.workers, args.rejection_rate)
    print_report(result)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    path = RESULTS_DIR / f"learning-curve-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\ncache: {llm.hits} replayed, {llm.misses} fetched")
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
