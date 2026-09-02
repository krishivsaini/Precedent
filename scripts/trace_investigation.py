"""Run one exception through the investigation graph and print the trace.

Ring 2's exit checklist requires an end-to-end run with a *visible* trace. This is that, as
a committed script rather than an ad-hoc invocation, because "you can see what it did" is a
claim someone should be able to check rather than take on trust.

    uv run python scripts/trace_investigation.py                     # one of each class
    uv run python scripts/trace_investigation.py tds_short_payment   # one class
    uv run python scripts/trace_investigation.py --all netted_settlement

Uses the response cache, so re-running a case already seen costs nothing.
"""

import argparse
import sys
from pathlib import Path

# `precedent` is installed (src layout, editable), but `evals` is a top-level package that
# is not — pytest puts it on the path via `pythonpath = ["."]`, and a script run by file
# path gets no such help. Added here rather than requiring `python -m` so the usage line
# above is the one that actually works.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.cache import CachingLLM  # noqa: E402
from evals.dataset.loader import load_dataset  # noqa: E402
from evals.retrieval_eval import scenario_to_case  # noqa: E402
from precedent.adapters.retrieval.bm25 import BM25Retriever
from precedent.corpus.seed import seed_precedent_records
from precedent.graph.investigation import format_trace, run_investigation

CLASSES = (
    "netted_settlement", "direct_neft_bypass", "tds_short_payment", "split_payment",
    "refund_netted", "duplicate_payment", "unmatchable",
)


def build_llm(provider: str, model: str | None):
    if provider == "groq":
        from precedent.adapters.llm.groq import DEFAULT_MODEL, GroqClient

        return GroqClient(model=model or DEFAULT_MODEL)
    if provider == "gemini":
        from precedent.adapters.llm.gemini import DEFAULT_MODEL, GeminiClient

        return GeminiClient(model=model or DEFAULT_MODEL)
    if provider == "ollama":
        from precedent.adapters.llm.ollama import DEFAULT_MODEL, OllamaClient

        return OllamaClient(model=model or DEFAULT_MODEL)
    from precedent.adapters.llm.nvidia import DEFAULT_MODEL, NvidiaClient

    return NvidiaClient(model=model or DEFAULT_MODEL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace one case through the graph")
    parser.add_argument("kinds", nargs="*", default=None,
                        help="exception classes to trace (default: one of each)")
    parser.add_argument("--all", action="store_true",
                        help="every pool case of the named class, not just the first")
    parser.add_argument("--provider", default="nvidia",
                        choices=("nvidia", "groq", "gemini", "ollama"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-precedents", action="store_true",
                        help="run without retrieval, to see what the corpus contributes")
    args = parser.parse_args()

    pool = [s for s in load_dataset() if s.is_exception and s.pool_or_test == "pool"]
    wanted = args.kinds or list(CLASSES)

    chosen = []
    for kind in wanted:
        matching = [s for s in pool if s.kind == kind]
        if not matching:
            print(f"no pool scenario of kind {kind!r}; known kinds: {', '.join(CLASSES)}")
            return 2
        chosen.extend(matching if args.all else matching[:1])

    llm = CachingLLM(build_llm(args.provider, args.model))
    retriever = None if args.no_precedents else BM25Retriever(seed_precedent_records())

    correct = 0
    for scenario in chosen:
        # No prebuilt graph: that would bypass metering and zero the cost numbers.
        outcome, trace = run_investigation(
            scenario_to_case(scenario), llm, retriever=retriever
        )
        hit = outcome.reason_code.value == scenario.expected_reason_code
        correct += hit
        print(f"\n=== {scenario.scenario_id}  ({scenario.kind})")
        print(f"    gold: {scenario.expected_reason_code}")
        print(format_trace(trace))
        print(f"    -> {outcome.reason_code.value} @ {outcome.confidence:.2f}  "
              f"{'CORRECT' if hit else 'WRONG'}  "
              f"[{outcome.model_calls} model call(s), {outcome.tool_calls_made} tool call(s), "
              f"{outcome.total_tokens or 0:,} tokens]")
        if outcome.rationale:
            print(f"    rationale: {outcome.rationale[:300]}")

    print(f"\n{correct}/{len(chosen)} correct   "
          f"(cache: {llm.hits} replayed, {llm.misses} fetched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
