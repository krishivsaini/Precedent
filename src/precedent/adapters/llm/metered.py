"""An `LLMClient` wrapper that totals what a run cost.

The Ring 1 chain makes exactly one model call per case, so its cost telemetry could ride on
the single `LLMResponse`. The Ring 2 graph makes between two and eight — a tool loop, a
proposal, and up to two revisions — and `run_investigation` returns one `ResolutionOutcome`
with nowhere for that to accumulate. The result was that the graph, the configuration where
cost actually varies, reported no tokens at all.

Spec §6 lists tokens per exception as a cost-discipline metric. A number that silently
becomes zero when the system gets more expensive is worse than not reporting it.

Kept separate from `CachingLLM` so the two compose: metering wraps caching, and a replayed
response is counted as the tokens it originally cost while being excluded from latency.
"""

import threading

from precedent.adapters.llm.base import LLMClient, LLMResponse


class MeteredLLM:
    """Counts calls, tokens and latency. One instance per case, or per batch — whatever is
    being measured. Thread-safe, since the ablation runs several cases concurrently."""

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self._lock = threading.Lock()
        self.calls = 0
        self.cached_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.thinking_tokens = 0
        self.latency_ms = 0
        self.attempts = 0

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return getattr(self._inner, "provider", "")

    def complete(self, system: str, user: str, *, temperature: float = 0.0) -> LLMResponse:
        response = self._inner.complete(system, user, temperature=temperature)
        with self._lock:
            self.calls += 1
            self.attempts += response.attempts
            self.prompt_tokens += response.prompt_tokens or 0
            self.completion_tokens += response.completion_tokens or 0
            self.thinking_tokens += response.thinking_tokens or 0
            if response.cached:
                self.cached_calls += 1
            else:
                # Cached replays would collapse the latency distribution toward zero.
                self.latency_ms += response.latency_ms
        return response

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens + self.thinking_tokens

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "cached_calls": self.cached_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "thinking_tokens": self.thinking_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
        }
