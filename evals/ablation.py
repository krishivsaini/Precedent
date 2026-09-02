"""Ring 1.3 — the kill criterion.

> **KILL CRITERION** — if grounded does not beat zero-shot, the thesis is dead. Fall back to
> plain adjudication. — spec §9, Ring 1

Three arms over the same 62 pool exceptions, the same model, the same prompt, the same *k*.
The only thing that varies is which precedents go into the prompt:

| arm | precedent block contains |
|---|---|
| `zero_shot` | nothing — the empty-corpus sentence |
| `grounded` | the top-*k* **retrieved** precedents |
| `random_control` | *k* precedents sampled at random (spec §6, control #2) |

The random control is what separates "retrieval works" from "more text in the prompt works".
Without it, a grounded win over zero-shot is unattributable, because the grounded arm's
prompt is also simply longer. It runs here unprompted, as spec §6 demands.

**Read the result honestly.** If `grounded` does not beat `zero_shot`, that is the finding.
It gets written into `docs/ARCHITECTURE.md` and the project falls back to plain adjudication.
Building Ring 2 on top of a failed kill criterion would be building on a premise the data has
already refused.

Scored against gold reason codes. The test set is not touched — it is replayed at corpus
snapshots from Ring 3.4, and spending it here would leave nothing held out.
"""

import argparse
import itertools
import math
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from evals.cache import CachingLLM
from evals.dataset.loader import load_dataset
from evals.dataset.scenario import Scenario
from evals.retrieval_eval import RANDOM_CONTROL_SEED, scenario_to_case
from precedent.adapters.llm.base import LLMClient
from precedent.adapters.retrieval.base import Retriever
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.adapters.retrieval.dense import DenseRetriever, HashingEmbedder
from precedent.adapters.retrieval.hybrid import HybridRetriever
from precedent.adapters.retrieval.random_control import RandomRetriever
from precedent.corpus.seed import seed_precedent_records
from precedent.domain.confidence import DEFAULT_AUTO_RESOLVE_THRESHOLD
from precedent.domain.reasons import ReasonCode
from precedent.graph.investigation import run_investigation
from precedent.usecases.resolve import ResolutionOutcome, resolve_case

RESULTS_DIR = Path(__file__).parent / "results"

#: Justified by `evals/retrieval_eval.py`: at k=5 lexical retrieval surfaces a same-class
#: precedent for every pool exception, and below that some classes are never reachable.
DEFAULT_K = 5

#: Above this share of unreachable-model escalations, the run is measuring the provider
#: rather than the system and is aborted instead of written. Set low deliberately: a handful
#: of transient failures is tolerable noise, a third of the batch is not a result.
MAX_UNAVAILABLE_SHARE = 0.15

#: A share alone cannot police a small batch: one transient blip in a six-case smoke run is
#: 16.7% and would abort it. The breaker needs both a proportion and an absolute floor. On
#: the real 62-case run 15% is 9 cases, so this floor never weakens it where it matters.
MIN_UNAVAILABLE_TO_ABORT = 3


def mcnemar_exact(a_correct: dict, b_correct: dict) -> dict:
    """Exact McNemar test on two arms scored over the same cases.

    Paired, because the arms answer the *same* 62 cases — comparing two independent
    proportions would throw away that pairing and understate the evidence. Only the
    discordant pairs carry information: cases both arms get right, or both get wrong, say
    nothing about which is better.

    Exact binomial rather than the chi-squared approximation, because the discordant counts
    here are single digits and the approximation is unreliable below about 25.

    This exists because the headline numbers invite over-claiming. A 6.5-point gap on 62
    cases is four cases; reporting it as a demonstrated improvement without saying whether
    four cases could be noise is the same category of error as the rate limit that
    masqueraded as a kill-criterion failure (FAILURES.md) — a plausible number nobody
    checked.
    """
    ids = a_correct.keys()
    a_wins = sum(1 for i in ids if a_correct[i] and not b_correct[i])
    b_wins = sum(1 for i in ids if not a_correct[i] and b_correct[i])
    discordant = a_wins + b_wins
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, k) for k in range(min(a_wins, b_wins) + 1))
        p_value = min(1.0, 2 * tail / 2**discordant)
    return {
        "a_wins": a_wins,
        "b_wins": b_wins,
        "discordant_pairs": discordant,
        "p_value": round(p_value, 4),
        "significant_at_05": p_value < 0.05,
    }


class AblationAborted(RuntimeError):
    """The run was too degraded to mean anything. Raised instead of returning numbers."""


def build_retriever(name: str, records: list) -> Retriever | None:
    if name == "none":
        return None
    if name == "bm25":
        return BM25Retriever(records)
    if name == "dense":
        return DenseRetriever(records, embedder=HashingEmbedder())
    if name == "hybrid":
        return HybridRetriever(records, embedder=HashingEmbedder())
    if name == "random":
        return RandomRetriever(records, seed=RANDOM_CONTROL_SEED)
    raise ValueError(f"unknown retriever {name!r}")


def _run_arm(
    arm: str,
    retriever: Retriever | None,
    scenarios: list[Scenario],
    llm: LLMClient,
    k: int,
    threshold: float,
    workers: int,
    engine: str = "chain",
) -> dict:
    records_by_id = {r.precedent_id: r for r in seed_precedent_records()}

    done = itertools.count(1)

    def attempt(scenario: Scenario) -> tuple[Scenario, ResolutionOutcome]:
        case = scenario_to_case(scenario)
        if engine == "graph":
            # The graph retrieves in its own node, so it is handed the retriever rather
            # than a precomputed hit list. Both engines return the same ResolutionOutcome,
            # so scoring is identical and a difference between them is a difference in the
            # system rather than in how it was measured.
            outcome, _trace = run_investigation(
                case, llm, retriever=retriever, k=k, threshold=threshold
            )
        else:
            hits = retriever.retrieve(case.retrieval_query(), k) if retriever else []
            outcome = resolve_case(case, llm, precedents=hits, confidence_threshold=threshold)
        index = next(done)
        if index % 10 == 0 or index == len(scenarios):
            # A run of a few hundred calls with no output is unobservable: there is no way
            # to tell steady progress from a process wedged in retry backoff.
            print(f"  {arm}: {index}/{len(scenarios)}", flush=True)
        return scenario, outcome

    # Order is restored from the input list, so concurrency cannot change the result.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pairs = list(pool.map(attempt, scenarios))

    unreachable = [
        outcome for _, outcome in pairs
        if outcome.reason_code is ReasonCode.ESCALATED_MODEL_UNAVAILABLE
    ]
    unavailable = len(unreachable)
    if unavailable:
        # Printed even when the run continues: a handful of transient failures is tolerable
        # noise, but it is noise the reader should know was in the sample.
        reasons = {outcome.rationale[:400] for outcome in unreachable}
        print(f"  {arm}: {unavailable} case(s) could not reach the model:", flush=True)
        for reason in sorted(reasons):
            print(f"    - {reason}", flush=True)

    if unavailable > max(MIN_UNAVAILABLE_TO_ABORT, len(pairs) * MAX_UNAVAILABLE_SHARE):
        # Refusing to return is the point. A result file where most cases escalated because
        # the provider was down scores exactly like a system that cannot resolve anything,
        # and the first smoke run of this ablation printed a kill-criterion FAIL for exactly
        # that reason (see FAILURES.md). A measurement this degraded is not a finding.
        raise AblationAborted(
            f"arm {arm!r}: {unavailable}/{len(pairs)} cases could not reach the model. "
            "This measures the provider, not the system — no result was written."
        )

    resolved_correct = 0
    resolved_wrong: list[dict] = []
    escalated = 0
    false_resolution_paise = 0
    cited_total = 0
    cited_same_class = 0
    hallucinated = 0
    latencies: list[int] = []
    tokens: list[int] = []
    model_calls: list[int] = []
    tool_calls: list[int] = []
    retry_attempts = 0
    per_case: list[dict] = []

    for scenario, outcome in pairs:
        gold = scenario.expected_reason_code
        correct = (not outcome.escalated) and outcome.reason_code.value == gold
        if outcome.escalated:
            escalated += 1
        elif correct:
            resolved_correct += 1
        else:
            at_risk = scenario_to_case(scenario).expected_paise() or sum(
                p.amount_paise for p in scenario.payments
            )
            false_resolution_paise += at_risk
            resolved_wrong.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "kind": scenario.kind,
                    "gold": gold,
                    "said": outcome.reason_code.value,
                    "confidence": outcome.confidence,
                    "amount_at_risk_paise": at_risk,
                }
            )

        for pid in outcome.cited_precedent_ids:
            cited_total += 1
            record = records_by_id.get(pid)
            if record is None:
                hallucinated += 1
            elif record.reason_code == gold:
                cited_same_class += 1

        # Cached replays would collapse the latency distribution toward zero.
        if outcome.latency_ms is not None and not outcome.cached:
            latencies.append(outcome.latency_ms)
        if outcome.total_tokens is not None:
            tokens.append(outcome.total_tokens)
        retry_attempts += outcome.attempts - 1
        model_calls.append(outcome.model_calls)
        tool_calls.append(outcome.tool_calls_made)

        per_case.append(
            {
                "scenario_id": scenario.scenario_id,
                "kind": scenario.kind,
                "gold": gold,
                "said": outcome.reason_code.value,
                "confidence": outcome.confidence,
                "escalated": outcome.escalated,
                "correct": correct,
                "cited": outcome.cited_precedent_ids,
                "retrieved": outcome.retrieved_precedent_ids,
            }
        )

    total_cases = len(scenarios)
    return {
        "arm": arm,
        "retriever": arm if retriever else "none",
        "k": k if retriever else 0,
        "metrics": {
            "autonomous_resolution_rate": round(resolved_correct / total_cases, 4),
            "escalation_rate": round(escalated / total_cases, 4),
            "false_resolution_count": len(resolved_wrong),
            "false_resolution_cost_inr": round(false_resolution_paise / 100, 2),
            "precedent_precision": (
                round(cited_same_class / cited_total, 4) if cited_total else None
            ),
            "citations_made": cited_total,
            "hallucinated_citations": hallucinated,
            "latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
            "latency_p95_ms": (
                int(statistics.quantiles(latencies, n=20)[18]) if len(latencies) >= 20 else None
            ),
            "mean_tokens_per_exception": int(statistics.mean(tokens)) if tokens else None,
            # Reported beside accuracy because once every arm is near the ceiling, cost is
            # the only axis left on which the corpus can show an effect: reaching the same
            # answer with less investigation is a real benefit accuracy cannot express.
            "mean_model_calls_per_exception": round(statistics.mean(model_calls), 2),
            "mean_tool_calls_per_exception": round(statistics.mean(tool_calls), 2),
            "retry_attempts": retry_attempts,
        },
        "false_resolutions": resolved_wrong,
        "per_case": per_case,
    }


def run_ablation(
    llm: LLMClient,
    grounded_retriever: str = "bm25",
    k: int = DEFAULT_K,
    threshold: float = DEFAULT_AUTO_RESOLVE_THRESHOLD,
    workers: int = 8,
    limit: int | None = None,
    engine: str = "chain",
) -> dict:
    records = seed_precedent_records()
    scenarios = [s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"]
    if limit:
        scenarios = scenarios[:limit]

    arms_spec = [
        ("zero_shot", "none"),
        ("grounded", grounded_retriever),
        ("random_control", "random"),
    ]
    arms = []
    for arm_name, retriever_name in arms_spec:
        retriever = build_retriever(retriever_name, records)
        try:
            arms.append(
                _run_arm(arm_name, retriever, scenarios, llm, k, threshold, workers, engine)
            )
        finally:
            if hasattr(retriever, "close"):
                retriever.close()

    by_arm = {arm["arm"]: arm["metrics"]["autonomous_resolution_rate"] for arm in arms}
    correct_by_arm = {
        arm["arm"]: {c["scenario_id"]: c["correct"] for c in arm["per_case"]} for arm in arms
    }
    significance = {
        f"{a}_vs_{b}": mcnemar_exact(correct_by_arm[a], correct_by_arm[b])
        for a, b in [
            ("grounded", "zero_shot"),
            ("grounded", "random_control"),
            ("random_control", "zero_shot"),
        ]
    }
    grounded_beats_zero_shot = by_arm["grounded"] > by_arm["zero_shot"]
    grounded_beats_random = by_arm["grounded"] > by_arm["random_control"]

    return {
        "run_id": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "purpose": "Ring 1.3 kill criterion — does grounding beat zero-shot?",
        "model": llm.model,
        "provider": getattr(llm, "provider", None) or getattr(
            getattr(llm, "_inner", None), "provider", "unknown"
        ),
        "corpus": {"size": len(records), "corpus_version": 0, "composition": "seed only"},
        "dataset": {
            "scenarios_scored": len(scenarios),
            "split": "pool exceptions only; the 60 test exceptions are untouched",
        },
        "settings": {
            "k": k,
            "confidence_threshold": threshold,
            "grounded_retriever": grounded_retriever,
            "temperature": 0.0,
            "engine": engine,
        },
        "arms": arms,
        "significance": significance,
        "effect_decomposition": {
            "note": (
                "The random control shares the grounded arm's prompt shape and precedent "
                "count, differing only in whether the precedents are relevant. So the gain "
                "over zero-shot splits into an effect of having precedents at all and an "
                "effect of their relevance. Reporting the total as if it were all retrieval "
                "over-claims."
            ),
            "having_precedents_at_all_pp": round(
                (by_arm["random_control"] - by_arm["zero_shot"]) * 100, 1
            ),
            "relevance_of_precedents_pp": round(
                (by_arm["grounded"] - by_arm["random_control"]) * 100, 1
            ),
        },
        "kill_criterion": {
            "grounded_resolution_rate": by_arm["grounded"],
            "zero_shot_resolution_rate": by_arm["zero_shot"],
            "random_control_resolution_rate": by_arm["random_control"],
            "grounded_beats_zero_shot": grounded_beats_zero_shot,
            "grounded_beats_random_control": grounded_beats_random,
            "verdict": (
                "PASS — grounding beats both zero-shot and the random control"
                if grounded_beats_zero_shot and grounded_beats_random
                else "FAIL — see docs/ARCHITECTURE.md; the retrieval thesis is not supported"
            ),
            "caveat": (
                "The verdict compares point estimates. See `significance` for whether each "
                "comparison survives a paired test; a gap that does not is a direction, "
                "not yet a demonstrated effect."
            ),
        },
    }


def write_result(result: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    engine = (result.get("settings") or {}).get("engine", "chain")
    suffix = "" if engine == "chain" else f"-{engine}"
    path = RESULTS_DIR / f"ablation{suffix}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def print_report(result: dict) -> None:
    print(f"model: {result['model']}   k={result['settings']['k']}   "
          f"grounded retriever: {result['settings']['grounded_retriever']}   "
          f"engine: {result['settings']['engine']}")
    print(f"{result['dataset']['scenarios_scored']} pool exceptions, "
          f"corpus of {result['corpus']['size']} seed precedents\n")

    header = (f"{'arm':16}{'resolved':>10}{'escalated':>11}{'false':>7}{'cost INR':>12}"
              f"{'prec.prec':>11}{'calls':>7}{'tools':>7}{'tokens':>9}")
    print(header)
    print("-" * len(header))
    for arm in result["arms"]:
        m = arm["metrics"]
        precision = "-" if m["precedent_precision"] is None else f"{m['precedent_precision']:.1%}"
        print(
            f"{arm['arm']:16}{m['autonomous_resolution_rate']:>10.1%}"
            f"{m['escalation_rate']:>11.1%}{m['false_resolution_count']:>7}"
            f"{m['false_resolution_cost_inr']:>12,.0f}{precision:>11}"
            f"{m['mean_model_calls_per_exception']:>7.1f}"
            f"{m['mean_tool_calls_per_exception']:>7.1f}"
            f"{m['mean_tokens_per_exception'] or 0:>9,}"
        )

    kill = result["kill_criterion"]
    print(f"\n{kill['verdict']}")
    print(f"  grounded {kill['grounded_resolution_rate']:.1%} vs "
          f"zero-shot {kill['zero_shot_resolution_rate']:.1%} vs "
          f"random {kill['random_control_resolution_rate']:.1%}")

    decomposition = result["effect_decomposition"]
    print(f"\n  of the gain over zero-shot: "
          f"{decomposition['having_precedents_at_all_pp']:+.1f}pp from having precedents at "
          f"all, {decomposition['relevance_of_precedents_pp']:+.1f}pp from their relevance")

    print("\npaired significance (exact McNemar):")
    for name, test in result["significance"].items():
        verdict = "significant" if test["significant_at_05"] else "NOT significant"
        print(f"  {name:32} {test['a_wins']}W-{test['b_wins']}L  "
              f"p={test['p_value']:.4f}  {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ring 1.3 kill-criterion ablation")
    parser.add_argument("--provider",
                        choices=("nvidia", "groq", "gemini", "ollama"),
                        default="nvidia")
    parser.add_argument("--model", default=None)
    parser.add_argument("--retriever", default="bm25",
                        choices=("bm25", "dense", "hybrid"))
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None,
                        help="score only the first N scenarios — a smoke run, not a result")
    parser.add_argument("--engine", choices=("chain", "graph"), default="chain",
                        help="chain = Ring 1's single prompt; graph = Ring 2's LangGraph "
                             "investigation. The Ring 2 gate is that graph does not regress.")
    parser.add_argument("--no-cache", action="store_true",
                        help="bypass the response cache and re-ask the model everything")
    args = parser.parse_args()

    if args.provider == "nvidia":
        from precedent.adapters.llm.nvidia import DEFAULT_MODEL, NvidiaClient

        llm = NvidiaClient(model=args.model or DEFAULT_MODEL)
    elif args.provider == "groq":
        from precedent.adapters.llm.groq import DEFAULT_MODEL, GroqClient

        llm = GroqClient(model=args.model or DEFAULT_MODEL)
    elif args.provider == "gemini":
        from precedent.adapters.llm.gemini import DEFAULT_MODEL, GeminiClient

        llm = GeminiClient(model=args.model or DEFAULT_MODEL)
    else:
        from precedent.adapters.llm.ollama import DEFAULT_MODEL, OllamaClient

        llm = OllamaClient(model=args.model or DEFAULT_MODEL)

    # Wrapped so a run interrupted by a quota exhaustion resumes tomorrow instead of
    # discarding everything it already paid for. See evals/cache.py.
    cached_llm = CachingLLM(llm, enabled=not args.no_cache)
    result = run_ablation(
        cached_llm, grounded_retriever=args.retriever, k=args.k, workers=args.workers,
        limit=args.limit, engine=args.engine,
    )
    result["cache"] = cached_llm.stats()
    print_report(result)
    print(f"cache: {cached_llm.hits} replayed, {cached_llm.misses} fetched")

    if args.limit:
        print("\n--limit was set: this is a smoke run, not a result. Not written to disk.")
        return
    print(f"\nwritten to {write_result(result)}")


if __name__ == "__main__":
    main()
