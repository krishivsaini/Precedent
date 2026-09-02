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
exceed 140 — 42 hand-written seeds plus 98 depositable pool exceptions, one precedent per
resolution — so 150 and 200 are arithmetically unreachable. That is a contradiction in the
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
from concurrent.futures import ThreadPoolExecutor
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.cache import CachingLLM
from evals.dataset.loader import load_dataset
from evals.retrieval_eval import RANDOM_CONTROL_SEED, scenario_to_case
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.random_control import RandomRetriever
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import run_investigation
from precedent.usecases.deposit import author_precedent

RESULTS_DIR = Path(__file__).parent / "results"

#: Quarters of the depositable pool. See the module docstring for why not 0/50/100/150/200.
SNAPSHOTS = (0, 25, 50, 75, 98)

#: Fixes the order pool cases are worked in. Cases arrive in arbitrary order in reality, so
#: a seeded shuffle models that honestly — and fixing the seed makes every snapshot
#: reproducible rather than a different corpus each run.
DEPOSIT_ORDER_SEED = 20260902


def deposit_order(scenarios: list) -> list:
    pool = [s for s in scenarios if s.is_exception and s.pool_or_test == "pool"]
    shuffled = pool[:]
    random.Random(DEPOSIT_ORDER_SEED).shuffle(shuffled)
    return shuffled


def build_deposit_sequence(
    llm, scenarios: list, limit: int | None = None, workers: int = 4
) -> list[dict]:
    """Author one precedent per pool exception, in a fixed order.

    The human is simulated by the gold label: every resolution is treated as **confirmed at
    the gold reason code**. That is deliberately the *optimistic* case for the corpus — a
    real operator would reject some, and every rejection is a precedent that never exists.
    So the curve this produces is an upper bound on what deposits can achieve, and it is
    reported as one.
    """
    ordered = deposit_order(scenarios)[: limit or None]

    def author(indexed):
        index, scenario = indexed
        case = scenario_to_case(scenario)
        try:
            precedent = author_precedent(
                llm, case, ReasonCode(scenario.expected_reason_code),
                f"Confirmed by the reconciliation lead: {scenario.notes}", "confirmed",
            )
            record = precedent.to_record(
                precedent_id=f"prec_{index:04d}",
                deposited_at="2026-09-02T00:00:00+00:00",
                corpus_version=index,
            )
            return {"record": record, "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind, "ok": True}
        except Exception as error:  # noqa: BLE001 - a failed authoring is a real outcome
            # The version still advances: a deposit that could not be authored is a gap in
            # the corpus, and pretending it did not happen would overstate corpus growth.
            return {"record": None, "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind, "ok": False, "error": str(error)[:200]}

    # Concurrent, but the sequence order is the deposit order — corpus_version is assigned
    # from position, so the snapshots stay reproducible regardless of completion order.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        sequence = list(pool.map(author, enumerate(ordered, start=1)))
    print(f"  authored {sum(1 for e in sequence if e['ok'])}/{len(sequence)}", flush=True)
    return sequence


def corpus_at(sequence: list[dict], deposits: int) -> list:
    """The corpus as it stood after exactly `deposits` deposits: seeds plus the first N."""
    grown = [entry["record"] for entry in sequence[:deposits] if entry["record"] is not None]
    return seed_precedent_records() + grown


def replay_test_set(
    llm, scenarios: list, corpus: list, k: int, control: bool, workers: int = 4
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

    correct = escalated = 0
    counterparty_correct = counterparty_total = 0
    cited_total = cited_applied = 0
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
            if record and record.reason_code == scenario.expected_reason_code:
                cited_applied += 1

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
        "per_case": per_case,
    }


def run_replay(llm, k: int = 5, snapshots=SNAPSHOTS, deposit_limit: int | None = None,
               workers: int = 4) -> dict:
    scenarios = load_dataset()
    print("building the deposit sequence...", flush=True)
    sequence = build_deposit_sequence(llm, scenarios, deposit_limit, workers)
    authored = sum(1 for e in sequence if e["ok"])

    points = []
    for deposits in snapshots:
        if deposits > len(sequence):
            continue
        corpus = corpus_at(sequence, deposits)
        print(f"replaying at {deposits} deposits (corpus {len(corpus)})...", flush=True)
        real = replay_test_set(llm, scenarios, corpus, k, False, workers)
        control = replay_test_set(llm, scenarios, corpus, k, True, workers)
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
        },
        "test_set": {
            "size": len([s for s in scenarios if s.is_exception and s.pool_or_test == "test"]),
            "never_deposited": True,
        },
        "caveats": [
            "The human is simulated by the gold label: every pool resolution is treated as "
            "confirmed at the correct reason code. A real operator would reject some, and "
            "each rejection is a precedent that never exists — so this curve is an upper "
            "bound on what deposits can achieve.",
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
    args = parser.parse_args()

    from scripts.trace_investigation import build_llm

    llm = CachingLLM(build_llm(args.provider, None))
    result = run_replay(llm, args.k, tuple(args.snapshots), args.deposit_limit, args.workers)
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
